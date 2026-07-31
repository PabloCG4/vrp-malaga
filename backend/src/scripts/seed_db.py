"""
Realistic database seeding script for the Rich VRP solver's Control Tower.

This script populates consecutive `WorkdayPlan`s for the Malaga demonstration
network with a small fleet of drivers and vehicles, a mix of standard
delivery `Order`s per day, and, on selected days, a genuine mid-day urgent
VRPPD order (a paired depot pickup and customer delivery), replaying the
exact same static-planning and dynamic-simulation pipeline already validated
by Phase 2 and Phase 3:

1. `solver.constructive.build_initial_state` followed by
   `solver.metaheuristic.run_tabu_search` produce the day's static plan.
2. On days with a dynamic disruption, `simulation.engine.DynamicSimulator`
   replays a traffic incident, an urgent order, or both, exactly as the
   Phase 3 demonstration does, yielding a final, re-optimized `VRPState`.
3. The resulting plan is persisted: `Order` rows for every standard and
   urgent stop, `RouteStop` rows for every vehicle's planned sequence
   (`evaluator.RouteEvaluation.stop_schedule`), and a `SimulationEvent` row
   for every disruption injected, giving the Control Tower dashboard a
   realistic, fully reproducible dataset to query against.

The script is idempotent and incremental:

- The static fleet (`drivers` / `vehicles`) is inserted only when those
  tables are empty; otherwise the existing rows are reused, so a second run
  never raises an integrity error on unique license plates or license numbers.
- Each run appends five new consecutive calendar days starting at
  `MAX(workday_date) + 1 day`, or at today's date when the table is empty,
  so re-running the script extends the dataset rather than colliding with
  already-seeded plans.

Only the persistence layer (`backend/src/db/`) and this script depend on the
database; the solver, heuristics and simulation engine are used exactly as
they are, unmodified, through their existing public APIs.

Completed days additionally get synthetic actual arrival telemetry, jittered
around their planned schedule, so that `RouteStop.actual_arrival_seconds`
demonstrates the planned-versus-actual comparison the schema was designed
for. Currently active or still-draft days are left without actual telemetry,
since that data would not exist yet in reality.
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import date, timedelta

import networkx as nx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.enums import RouteStopType, SimulationEventType, WorkdayStatus
from ..db.models import Driver, Order, RouteStop, SimulationEvent, Vehicle, WorkdayPlan
from ..db.session import create_all_tables, dispose_engine, get_session_factory
from ..domain.entities import Customer, TimeWindow, WorkdayInstance
from ..domain.entities import Vehicle as DomainVehicle
from ..simulation.clock import SimulationClock
from ..simulation.engine import DynamicSimulator
from ..simulation.events import TrafficIncidentEvent, UrgentOrderEvent
from ..solver.constructive import build_initial_state
from ..solver.evaluator import evaluate_state
from ..solver.metaheuristic import TabuSearchConfig, run_tabu_search
from ..topology.extractor import load_processed_graph
from ..topology.matrix import CostMatrix, build_cost_matrix, select_demonstration_nodes

# Reproducibility seed shared by every random draw this script makes, so two
# consecutive runs that start from the same calendar date produce the same
# customer draws and disruption choices for that day.
RANDOM_SEED: int = 2026

# Number of new consecutive calendar days appended on every seed run.
WORKDAY_COUNT: int = 5

FLEET_SIZE: int = 4
STANDARD_ORDERS_PER_DAY: int = 18
STANDARD_CUSTOMER_POOL_SIZE: int = 30
RESERVED_URGENT_NODE_COUNT: int = 5

DRIVER_FULL_NAMES: tuple[str, ...] = (
    "Antonio Garcia Ruiz",
    "Maria Fernandez Lopez",
    "Jose Martinez Sanchez",
    "Carmen Gonzalez Diaz",
    "Manuel Rodriguez Perez",
    "Lucia Sanchez Moreno",
)
VEHICLE_LICENSE_PLATE_SUFFIXES: tuple[str, ...] = ("BCD", "FGH", "JKL", "MNP", "RST", "VWX")
VEHICLE_CAPACITIES_KG: tuple[float, ...] = (350.0, 400.0, 320.0, 450.0, 380.0, 400.0)

# Stopping criteria for the static, once-per-day planning pass. Generous
# enough to converge well within a couple of seconds for a fleet and
# customer count of this size, while still bounded for a seeding script that
# plans a full week in one run.
STATIC_SEARCH_CONFIG = TabuSearchConfig(
    max_iterations=300, max_iterations_without_improvement=100, time_limit_seconds=5.0, tabu_tenure=12
)

# Rotating scenario templates: which dynamic disruptions are injected, and
# the lifecycle status the resulting plan is persisted with. Only COMPLETED
# days receive synthetic actual arrival telemetry, since ACTIVE and DRAFT
# plans represent a workday that has not finished (or not started) executing
# yet. Templates are applied cyclically across the rolling five-day window.
_DayScenario = dict[str, object]
DAY_SCENARIOS: tuple[_DayScenario, ...] = (
    {"status": WorkdayStatus.COMPLETED, "traffic_incident": False, "urgent_order": False},
    {"status": WorkdayStatus.COMPLETED, "traffic_incident": True, "urgent_order": False},
    {"status": WorkdayStatus.COMPLETED, "traffic_incident": False, "urgent_order": True},
    {"status": WorkdayStatus.ACTIVE, "traffic_incident": True, "urgent_order": True},
    {"status": WorkdayStatus.DRAFT, "traffic_incident": False, "urgent_order": False},
)

# Bounds, in seconds, of the jitter applied to a planned timestamp to derive
# a plausible "actual" arrival for an already COMPLETED workday.
ACTUAL_ARRIVAL_JITTER_SECONDS: tuple[int, int] = (-180, 300)


def _scenario_for_day(day_offset: int) -> _DayScenario:
    """Return the rotating scenario template for the given offset within a seed batch."""
    return DAY_SCENARIOS[day_offset % len(DAY_SCENARIOS)]


def _build_standard_customers_and_orders(
    street_network_graph: nx.MultiDiGraph,
    workday_plan: WorkdayPlan,
    candidate_nodes: list[int],
    day_random: random.Random,
) -> tuple[tuple[Customer, ...], dict[str, Order]]:
    """
    Draw a realistic subset of standard delivery customers for one workday.

    Returns both the immutable domain `Customer` tuple the solver operates
    on and the corresponding, not-yet-flushed `Order` ORM rows, keyed by
    `Customer.customer_id`, so that the caller can resolve every planned
    stop back to the database row that represents it.
    """
    chosen_nodes = day_random.sample(candidate_nodes, STANDARD_ORDERS_PER_DAY)

    customers: list[Customer] = []
    orders_by_customer_id: dict[str, Order] = {}

    for order_index, node_id in enumerate(chosen_nodes):
        node_attributes = street_network_graph.nodes[node_id]
        demand_kg = float(day_random.randint(10, 60))
        service_time_seconds = float(day_random.choice([180, 240, 300, 360]))
        window_start_seconds = float(day_random.randint(0, 5) * 1800)
        window_length_seconds = float(day_random.randint(2, 5) * 1800)

        customer = Customer(
            node_id=node_id,
            demand=demand_kg,
            service_time_seconds=service_time_seconds,
            time_window=TimeWindow(
                start_seconds=window_start_seconds, end_seconds=window_start_seconds + window_length_seconds
            ),
        )
        customers.append(customer)

        orders_by_customer_id[customer.customer_id] = Order(
            workday_plan_id=workday_plan.id,
            customer_name=f"Customer {workday_plan.workday_date.isoformat()}-{order_index + 1:02d}",
            node_id=node_id,
            latitude=float(node_attributes["y"]),
            longitude=float(node_attributes["x"]),
            demand_kg=demand_kg,
            service_time_seconds=int(service_time_seconds),
            time_window_start_seconds=int(window_start_seconds),
            time_window_end_seconds=int(window_start_seconds + window_length_seconds),
            is_urgent=False,
            is_pickup_stop=False,
        )

    return tuple(customers), orders_by_customer_id


async def _ensure_fleet(session: AsyncSession) -> tuple[list[Driver], list[Vehicle]]:
    """
    Return the static fleet, inserting the default drivers and vehicles only when empty.

    A second (or later) seed run reuses the existing fleet rows rather than
    attempting another insert that would violate the unique constraints on
    `drivers.license_number` and `vehicles.license_plate`.
    """
    existing_drivers = list((await session.execute(select(Driver).order_by(Driver.id))).scalars().all())
    existing_vehicles = list((await session.execute(select(Vehicle).order_by(Vehicle.id))).scalars().all())

    if existing_drivers and existing_vehicles:
        print(
            f"Reusing existing fleet: {len(existing_drivers)} driver(s), "
            f"{len(existing_vehicles)} vehicle(s)."
        )
        return existing_drivers, existing_vehicles

    drivers = [
        Driver(full_name=full_name, license_number=f"LIC-{index + 1:04d}")
        for index, full_name in enumerate(DRIVER_FULL_NAMES[:FLEET_SIZE])
    ]
    session.add_all(drivers)
    await session.flush()

    vehicles = [
        Vehicle(
            license_plate=f"{4821 + index * 173:04d} {VEHICLE_LICENSE_PLATE_SUFFIXES[index]}",
            capacity_kg=VEHICLE_CAPACITIES_KG[index],
            default_driver_id=drivers[index].id,
        )
        for index in range(FLEET_SIZE)
    ]
    session.add_all(vehicles)
    await session.flush()

    print(f"Seeded fleet: {len(drivers)} driver(s), {len(vehicles)} vehicle(s).")
    return drivers, vehicles


async def _resolve_rolling_start_date(session: AsyncSession) -> date:
    """
    Determine the first calendar date of the next five-day seeding window.

    Queries `SELECT MAX(workday_date) FROM workday_plans`. When the table is
    empty the window starts at today's date; otherwise it starts on the day
    immediately after the latest already-persisted plan, so each seed run
    appends new consecutive days without colliding with unique
    `workday_date` values.
    """
    maximum_workday_date = (
        await session.execute(select(func.max(WorkdayPlan.workday_date)))
    ).scalar_one_or_none()

    if maximum_workday_date is None:
        start_date = date.today()
        print(f"No existing workday plans found; starting the rolling window at {start_date.isoformat()}.")
        return start_date

    start_date = maximum_workday_date + timedelta(days=1)
    print(
        f"Latest workday plan is {maximum_workday_date.isoformat()}; "
        f"starting the rolling window at {start_date.isoformat()}."
    )
    return start_date


async def _persist_urgent_order_pair(
    session: AsyncSession,
    street_network_graph: nx.MultiDiGraph,
    workday_plan: WorkdayPlan,
    workday: WorkdayInstance,
    depot_node: int,
    urgent_event: UrgentOrderEvent,
    orders_by_customer_id: dict[str, Order],
) -> None:
    """
    Persist the depot pickup and customer delivery `Order` rows of a VRPPD pair.

    The delivery row is flushed first so that the pickup row can reference it
    through `paired_order_id`, and the delivery row is then updated to point
    back at the pickup row, mirroring the mutual pairing
    `domain.entities.WorkdayInstance` validates between the two `Customer`
    objects `DynamicSimulator.handle_urgent_order` created.
    """
    pickup_customer = workday.customers_by_id[urgent_event.pickup_customer_id]
    delivery_customer = workday.customers_by_id[urgent_event.delivery_customer_id]

    depot_attributes = street_network_graph.nodes[depot_node]
    delivery_attributes = street_network_graph.nodes[delivery_customer.node_id]

    delivery_order = Order(
        workday_plan_id=workday_plan.id,
        customer_name=f"Urgent order {urgent_event.order_id}",
        node_id=delivery_customer.node_id,
        latitude=float(delivery_attributes["y"]),
        longitude=float(delivery_attributes["x"]),
        demand_kg=delivery_customer.demand,
        service_time_seconds=int(delivery_customer.service_time_seconds),
        time_window_start_seconds=int(delivery_customer.time_window.start_seconds),
        time_window_end_seconds=int(min(delivery_customer.time_window.end_seconds, 10.0**9)),
        is_urgent=True,
        is_pickup_stop=False,
    )
    session.add(delivery_order)
    await session.flush()

    pickup_order = Order(
        workday_plan_id=workday_plan.id,
        customer_name=f"Urgent order {urgent_event.order_id} (depot pickup)",
        node_id=depot_node,
        latitude=float(depot_attributes["y"]),
        longitude=float(depot_attributes["x"]),
        demand_kg=0.0,
        service_time_seconds=int(pickup_customer.service_time_seconds),
        time_window_start_seconds=0,
        time_window_end_seconds=int(delivery_order.time_window_end_seconds),
        is_urgent=True,
        is_pickup_stop=True,
        paired_order_id=delivery_order.id,
    )
    session.add(pickup_order)
    await session.flush()

    delivery_order.paired_order_id = pickup_order.id
    await session.flush()

    orders_by_customer_id[pickup_customer.customer_id] = pickup_order
    orders_by_customer_id[delivery_customer.customer_id] = delivery_order


async def _seed_single_workday(
    session: AsyncSession,
    street_network_graph: nx.MultiDiGraph,
    cost_matrix: CostMatrix,
    domain_fleet: tuple[DomainVehicle, ...],
    vehicle_rows_by_domain_id: dict[str, Vehicle],
    depot_node: int,
    standard_node_pool: list[int],
    urgent_node_pool: list[int],
    workday_date: date,
    day_offset: int,
    scenario: _DayScenario,
) -> None:
    """Plan, optionally simulate, and persist a single day's `WorkdayPlan`."""
    weekday_label = workday_date.strftime("%A")
    print(f"\n=== {weekday_label} {workday_date.isoformat()} ===")
    # Seed from the calendar date itself so the same day always draws the
    # same customers and disruptions, even across separate rolling batches.
    day_random = random.Random(RANDOM_SEED + workday_date.toordinal())

    workday_plan = WorkdayPlan(workday_date=workday_date, status=WorkdayStatus.DRAFT)
    session.add(workday_plan)
    await session.flush()

    standard_customers, orders_by_customer_id = _build_standard_customers_and_orders(
        street_network_graph, workday_plan, standard_node_pool, day_random
    )
    session.add_all(orders_by_customer_id.values())
    await session.flush()

    workday = WorkdayInstance(customers=standard_customers, fleet=domain_fleet)

    print(f"Building the initial state and running the static Tabu Search pass for {len(standard_customers)} customers.")
    construction_started_at = time.perf_counter()
    initial_state = build_initial_state(workday, cost_matrix)
    search_result = run_tabu_search(initial_state, workday, cost_matrix, STATIC_SEARCH_CONFIG)
    state = search_result.best_state
    print(
        f"  Static plan ready: f(S) = {search_result.best_evaluation.total_cost:.2f}, "
        f"feasible={search_result.best_evaluation.is_feasible}."
    )

    simulation_events_to_persist: list[tuple[SimulationEventType, int, dict[str, object]]] = []

    if scenario["traffic_incident"] or scenario["urgent_order"]:
        simulator = DynamicSimulator(
            workday=workday,
            cost_matrix=cost_matrix,
            state=state,
            clock=SimulationClock(),
        )

        if scenario["traffic_incident"]:
            incident_edge = day_random.choice(sorted(cost_matrix.origin_indices_by_edge))
            traffic_event = TrafficIncidentEvent(
                trigger_minute=60 + day_offset * 5,
                first_node=incident_edge[0],
                second_node=incident_edge[1],
                reopen_minute=240,
                description=f"Traffic incident on {weekday_label}",
            )
            simulator.schedule_event(traffic_event)
            simulation_events_to_persist.append(
                (
                    SimulationEventType.TRAFFIC_INCIDENT,
                    traffic_event.trigger_minute,
                    {
                        "first_node": traffic_event.first_node,
                        "second_node": traffic_event.second_node,
                        "reopen_minute": traffic_event.reopen_minute,
                        "description": traffic_event.description,
                    },
                )
            )

        urgent_event: UrgentOrderEvent | None = None
        if scenario["urgent_order"]:
            urgent_event = UrgentOrderEvent(
                trigger_minute=120 + day_offset * 5,
                order_id=f"URG-{workday_date.isoformat()}",
                delivery_node=urgent_node_pool[day_offset % len(urgent_node_pool)],
                demand=float(day_random.randint(10, 30)),
                deadline_minutes_after_trigger=120.0,
                description=f"Urgent same-day order on {weekday_label}",
            )
            simulator.schedule_event(urgent_event)
            simulation_events_to_persist.append(
                (
                    SimulationEventType.URGENT_ORDER,
                    urgent_event.trigger_minute,
                    {
                        "order_id": urgent_event.order_id,
                        "delivery_node": urgent_event.delivery_node,
                        "demand": urgent_event.demand,
                        "pickup_service_time_seconds": urgent_event.pickup_service_time_seconds,
                        "delivery_service_time_seconds": urgent_event.delivery_service_time_seconds,
                        "deadline_minutes_after_trigger": urgent_event.deadline_minutes_after_trigger,
                        "description": urgent_event.description,
                    },
                )
            )

        simulator.run()

        workday = simulator.workday
        state = simulator.state

        if urgent_event is not None:
            await _persist_urgent_order_pair(
                session, street_network_graph, workday_plan, workday, depot_node, urgent_event, orders_by_customer_id
            )

    execution_time_ms = int((time.perf_counter() - construction_started_at) * 1000.0)
    final_evaluation = evaluate_state(state, workday, cost_matrix)

    workday_plan.status = scenario["status"]  # type: ignore[assignment]
    workday_plan.total_cost = final_evaluation.total_cost
    workday_plan.total_distance_km = final_evaluation.total_distance_meters / 1000.0
    workday_plan.execution_time_ms = execution_time_ms

    is_completed = scenario["status"] == WorkdayStatus.COMPLETED
    route_stop_count = 0
    for route, route_evaluation in zip(state.routes, final_evaluation.route_evaluations):
        if not route_evaluation.is_reachable:
            print(f"  Warning: route of vehicle '{route.vehicle_id}' is not fully reachable; skipping its stops.")
            continue

        vehicle_row = vehicle_rows_by_domain_id[route.vehicle_id]
        for position, stop_visit in enumerate(route_evaluation.stop_schedule):
            if stop_visit.customer_id is None:
                stop_type = RouteStopType.DEPOT_START if position == 0 else RouteStopType.DEPOT_END
                order_row = None
            else:
                customer = workday.customers_by_id[stop_visit.customer_id]
                stop_type = RouteStopType.DEPOT_PICKUP if customer.is_pickup_stop else RouteStopType.CUSTOMER_DELIVERY
                order_row = orders_by_customer_id[stop_visit.customer_id]

            planned_arrival_seconds = int(round(stop_visit.arrival_time_seconds))
            departure_seconds = int(round(stop_visit.departure_time_seconds))

            actual_arrival_seconds: int | None = None
            actual_departure_seconds: int | None = None
            if is_completed:
                jitter_seconds = day_random.randint(*ACTUAL_ARRIVAL_JITTER_SECONDS)
                actual_arrival_seconds = max(0, planned_arrival_seconds + jitter_seconds)
                actual_departure_seconds = max(actual_arrival_seconds, departure_seconds + jitter_seconds)

            session.add(
                RouteStop(
                    workday_plan_id=workday_plan.id,
                    vehicle_id=vehicle_row.id,
                    order_id=order_row.id if order_row is not None else None,
                    sequence_order=position,
                    stop_type=stop_type,
                    node_id=stop_visit.node_id,
                    planned_arrival_seconds=planned_arrival_seconds,
                    actual_arrival_seconds=actual_arrival_seconds,
                    departure_seconds=actual_departure_seconds if is_completed else departure_seconds,
                )
            )
            route_stop_count += 1

    for event_type, trigger_minute, payload in simulation_events_to_persist:
        session.add(
            SimulationEvent(
                workday_plan_id=workday_plan.id,
                event_type=event_type,
                trigger_minute=trigger_minute,
                payload_json=payload,
            )
        )

    await session.commit()
    print(
        f"  Persisted plan '{workday_plan.status.value}': {len(orders_by_customer_id)} order(s), "
        f"{route_stop_count} route stop(s), {len(simulation_events_to_persist)} simulation event(s)."
    )


async def seed_database() -> None:
    """Create every table and append the next five consecutive Malaga workdays."""
    print("Creating database tables (if they do not already exist).")
    await create_all_tables()

    print("Loading the preprocessed Malaga street network.")
    street_network_graph = load_processed_graph()

    depot_node, candidate_nodes = select_demonstration_nodes(
        street_network_graph, STANDARD_CUSTOMER_POOL_SIZE + RESERVED_URGENT_NODE_COUNT, random_seed=RANDOM_SEED
    )
    standard_node_pool = candidate_nodes[:STANDARD_CUSTOMER_POOL_SIZE]
    urgent_node_pool = candidate_nodes[STANDARD_CUSTOMER_POOL_SIZE:]

    print(f"Building the shared cost matrix for 1 depot and {len(candidate_nodes)} candidate node(s).")
    cost_matrix = build_cost_matrix(street_network_graph, depot_node, candidate_nodes)

    session_factory = get_session_factory()
    async with session_factory() as session:
        _, vehicle_rows = await _ensure_fleet(session)
        await session.commit()

        rolling_start_date = await _resolve_rolling_start_date(session)

        domain_fleet = tuple(
            DomainVehicle(vehicle_id=str(vehicle_row.id), max_capacity=vehicle_row.capacity_kg)
            for vehicle_row in vehicle_rows
        )
        vehicle_rows_by_domain_id = {str(vehicle_row.id): vehicle_row for vehicle_row in vehicle_rows}

        for day_offset in range(WORKDAY_COUNT):
            workday_date = rolling_start_date + timedelta(days=day_offset)
            scenario = _scenario_for_day(day_offset)
            await _seed_single_workday(
                session,
                street_network_graph,
                cost_matrix,
                domain_fleet,
                vehicle_rows_by_domain_id,
                depot_node,
                standard_node_pool,
                urgent_node_pool,
                workday_date,
                day_offset,
                scenario,
            )

    await dispose_engine()
    print(
        f"\nSeeding complete: {WORKDAY_COUNT} workday plan(s) from "
        f"{rolling_start_date.isoformat()} to "
        f"{(rolling_start_date + timedelta(days=WORKDAY_COUNT - 1)).isoformat()}."
    )


if __name__ == "__main__":
    # This module is only meant to be run as `python -m backend.src.scripts.seed_db`
    # from the repository root, for the same import resolution reason documented
    # in the solver and simulation modules' own __main__ blocks.
    asyncio.run(seed_database())
