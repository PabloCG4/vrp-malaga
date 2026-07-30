"""
Tests for the Phase 3 dynamic simulation engine (backend/src/simulation).

Two kinds of scenarios are used, matching the convention already established
in `test_metaheuristic.py`:

1. A small, synthetic, fully deterministic `CostMatrix` for `FleetTracker`,
   whose hand-computed timeline lets every locked-prefix and status boundary
   be verified by hand.
2. The real, preprocessed Malaga street network for the tests that need an
   actual street closure to patch (`TrafficIncidentEvent`) or a realistic
   fleet to insert a mid-day pickup-and-delivery pair into
   (`UrgentOrderEvent`), and for the full-day benchmark.
"""

from __future__ import annotations

import time

import networkx as nx
import numpy as np

from backend.src.domain.entities import Customer, Route, TimeWindow, Vehicle, WorkdayInstance
from backend.src.simulation.clock import SimulationClock
from backend.src.simulation.engine import DynamicSimulator
from backend.src.simulation.events import TrafficIncidentEvent, UrgentOrderEvent
from backend.src.simulation.fleet_tracker import FleetTracker, VehicleStatus
from backend.src.solver.constructive import build_initial_state
from backend.src.solver.evaluator import evaluate_route, evaluate_state
from backend.src.solver.metaheuristic import TabuSearchConfig, run_tabu_search
from backend.src.topology.matrix import CostMatrix, build_cost_matrix, select_demonstration_nodes


def _build_synthetic_cost_matrix(
    node_ids: tuple[int, ...], travel_time_seconds: float = 100.0, distance_meters: float = 1000.0
) -> CostMatrix:
    """Build a small, fully controlled `CostMatrix`, mirroring `test_metaheuristic.py`'s helper."""
    node_count = len(node_ids)
    travel_time_matrix = np.full((node_count, node_count), travel_time_seconds, dtype=np.float64)
    distance_matrix = np.full((node_count, node_count), distance_meters, dtype=np.float64)
    np.fill_diagonal(travel_time_matrix, 0.0)
    np.fill_diagonal(distance_matrix, 0.0)

    return CostMatrix(
        street_network_graph=nx.MultiDiGraph(),
        node_ids=node_ids,
        matrix_index_by_node={node_id: index for index, node_id in enumerate(node_ids)},
        travel_time_matrix=travel_time_matrix,
        distance_matrix=distance_matrix,
    )


def _build_demo_workday(customer_nodes: list[int], vehicle_count: int, vehicle_capacity: float = 400.0) -> WorkdayInstance:
    """Build a workday instance covering `customer_nodes`, mirroring `test_metaheuristic.py`'s helper."""
    customers = tuple(
        Customer(
            node_id=node_id,
            demand=float(15 + (index % 4) * 5),
            service_time_seconds=180.0 + 30.0 * (index % 6),
            time_window=TimeWindow(start_seconds=200.0 * index, end_seconds=200.0 * index + 5400.0),
        )
        for index, node_id in enumerate(customer_nodes)
    )
    fleet = tuple(
        Vehicle(vehicle_id=f"VAN-{vehicle_index + 1}", max_capacity=vehicle_capacity)
        for vehicle_index in range(vehicle_count)
    )
    return WorkdayInstance(customers=customers, fleet=fleet)


# ---------------------------------------------------------------------------
# FleetTracker (synthetic, hand-computed schedule)
# ---------------------------------------------------------------------------


def test_fleet_tracker_locked_prefix_and_status_match_hand_computed_schedule() -> None:
    """
    Confirm `FleetTracker` against a fully hand-computed timeline.

    Depot 0, customers 1 and 2, uniform 100 s / 1000 m legs, 50 s service,
    unrestricted time windows. The resulting schedule is, by construction:

        depot   arrival=0,   departure=0
        cust 1  arrival=100, departure=150   (100 s drive + 50 s service)
        cust 2  arrival=250, departure=300   (100 s drive + 50 s service)
        depot   arrival=400, departure=400   (final return leg)
    """
    depot_node, first_customer_node, second_customer_node = 0, 1, 2
    cost_matrix = _build_synthetic_cost_matrix((depot_node, first_customer_node, second_customer_node))
    customers = (
        Customer(node_id=first_customer_node, demand=10.0, service_time_seconds=50.0),
        Customer(node_id=second_customer_node, demand=10.0, service_time_seconds=50.0),
    )
    vehicle = Vehicle(vehicle_id="VAN-1", max_capacity=100.0)
    workday = WorkdayInstance(customers=customers, fleet=(vehicle,))

    route = Route(vehicle_id="VAN-1", customer_sequence=tuple(customer.customer_id for customer in customers))
    route_evaluation = evaluate_route(route, workday, cost_matrix)

    assert [stop.departure_time_seconds for stop in route_evaluation.stop_schedule] == [0.0, 150.0, 300.0, 400.0]

    # Locked prefix length: position 0 (first customer) locks as soon as the
    # vehicle departs the depot at t=0; position 1 (second customer) locks
    # only once the vehicle also departs the first customer, at t=150.
    assert FleetTracker.compute_locked_prefix_length(route_evaluation.stop_schedule, 2, current_time_seconds=0.0) == 1
    assert FleetTracker.compute_locked_prefix_length(route_evaluation.stop_schedule, 2, current_time_seconds=149.0) == 1
    assert FleetTracker.compute_locked_prefix_length(route_evaluation.stop_schedule, 2, current_time_seconds=150.0) == 2
    assert FleetTracker.compute_locked_prefix_length(route_evaluation.stop_schedule, 2, current_time_seconds=400.0) == 2

    # Vehicle status at representative instants along the same timeline.
    driving_to_first = FleetTracker.snapshot_vehicle(route, route_evaluation, current_time_seconds=50.0)
    assert driving_to_first.status is VehicleStatus.DRIVING
    assert driving_to_first.current_node == depot_node
    assert driving_to_first.next_node == first_customer_node

    serving_first = FleetTracker.snapshot_vehicle(route, route_evaluation, current_time_seconds=120.0)
    assert serving_first.status is VehicleStatus.SERVING
    assert serving_first.current_node == first_customer_node
    assert serving_first.active_customer_id == customers[0].customer_id

    driving_to_depot = FleetTracker.snapshot_vehicle(route, route_evaluation, current_time_seconds=350.0)
    assert driving_to_depot.status is VehicleStatus.DRIVING
    assert driving_to_depot.current_node == second_customer_node
    assert driving_to_depot.next_node == depot_node

    finished = FleetTracker.snapshot_vehicle(route, route_evaluation, current_time_seconds=400.0)
    assert finished.status is VehicleStatus.FINISHED


def test_fleet_tracker_reports_idle_at_depot_for_an_undispatched_route() -> None:
    """An undispatched (empty) route must be reported as idle at the depot, not finished."""
    depot_node = 0
    cost_matrix = _build_synthetic_cost_matrix((depot_node, 1))
    customer = Customer(node_id=1, demand=5.0)
    vehicle = Vehicle(vehicle_id="VAN-1", max_capacity=50.0)
    workday = WorkdayInstance(customers=(customer,), fleet=(vehicle,))
    empty_route = Route(vehicle_id="VAN-1", customer_sequence=())
    route_evaluation = evaluate_route(empty_route, workday, cost_matrix)

    snapshot = FleetTracker.snapshot_vehicle(empty_route, route_evaluation, current_time_seconds=1000.0)

    assert snapshot.status is VehicleStatus.IDLE_AT_DEPOT
    assert snapshot.locked_prefix_length == 0


# ---------------------------------------------------------------------------
# Traffic incident event (real street network)
# ---------------------------------------------------------------------------


def test_traffic_incident_patches_matrix_reopens_and_triggers_bounded_reoptimization(
    malaga_street_network: nx.MultiDiGraph,
) -> None:
    """
    A `TrafficIncidentEvent` must close the requested street, trigger a
    re-optimization that stays within its configured time budget and never
    touches a locked prefix, and, once its `reopen_minute` elapses, reopen
    the street and restore the matrix to a fully connected state again.
    """
    depot_node, customer_nodes = select_demonstration_nodes(malaga_street_network, customer_count=20)
    cost_matrix = build_cost_matrix(malaga_street_network, depot_node, customer_nodes)
    workday = _build_demo_workday(customer_nodes, vehicle_count=3)
    initial_state = build_initial_state(workday, cost_matrix)
    planning_result = run_tabu_search(
        initial_state, workday, cost_matrix, TabuSearchConfig(time_limit_seconds=3.0)
    )

    busiest_street_edge = max(
        cost_matrix.origin_indices_by_edge, key=lambda edge: len(cost_matrix.origin_indices_by_edge[edge])
    )
    reoptimization_time_limit_seconds = 1.5
    simulator = DynamicSimulator(
        workday=workday,
        cost_matrix=cost_matrix,
        state=planning_result.best_state,
        clock=SimulationClock(workday_duration_seconds=7200.0, tick_duration_seconds=60.0),
        reoptimization_config=TabuSearchConfig(
            max_iterations=300,
            max_iterations_without_improvement=100,
            time_limit_seconds=reoptimization_time_limit_seconds,
            tabu_tenure=10,
        ),
    )
    simulator.schedule_event(
        TrafficIncidentEvent(
            trigger_minute=10, first_node=busiest_street_edge[0], second_node=busiest_street_edge[1], reopen_minute=30
        )
    )

    simulator.run()

    assert len(simulator.telemetry_log) >= 1
    for telemetry_entry in simulator.telemetry_log:
        assert telemetry_entry.elapsed_seconds <= reoptimization_time_limit_seconds + 1.0
        assert telemetry_entry.locked_prefixes_respected

    # The street was reopened before the (short) simulated workday ended.
    assert not cost_matrix.closed_edges
    assert cost_matrix.find_unreachable_pairs() == ()


# ---------------------------------------------------------------------------
# Urgent order event (real street network)
# ---------------------------------------------------------------------------


def test_urgent_order_yields_full_coverage_no_precedence_violations_and_preserves_locks(
    malaga_street_network: nx.MultiDiGraph,
) -> None:
    """
    An `UrgentOrderEvent` must result in a state that still covers every
    customer exactly once (the two new pickup-and-delivery stops included),
    with zero precedence violations anywhere in the fleet, and without
    disturbing any stop that was already locked at the moment the order
    arrived.
    """
    depot_node, customer_nodes = select_demonstration_nodes(malaga_street_network, customer_count=15)
    reserved_delivery_node = customer_nodes.pop()
    cost_matrix = build_cost_matrix(malaga_street_network, depot_node, customer_nodes + [reserved_delivery_node])
    workday = _build_demo_workday(customer_nodes, vehicle_count=3)
    initial_state = build_initial_state(workday, cost_matrix)
    planning_result = run_tabu_search(
        initial_state, workday, cost_matrix, TabuSearchConfig(time_limit_seconds=3.0)
    )

    simulator = DynamicSimulator(
        workday=workday,
        cost_matrix=cost_matrix,
        state=planning_result.best_state,
        clock=SimulationClock(workday_duration_seconds=7200.0, tick_duration_seconds=60.0),
        reoptimization_config=TabuSearchConfig(time_limit_seconds=1.5, max_iterations=300),
    )

    # Advance partway through the simulated day so some stops are already
    # locked by the time the order arrives, which is what makes the
    # "locked prefixes preserved" assertion below meaningful.
    for _ in range(30):
        simulator.clock.advance()
    locks_before_order = FleetTracker.compute_locked_prefix_lengths(
        simulator.state.routes, simulator.route_evaluations_by_vehicle_id, simulator.clock.current_time_seconds
    )
    routes_before_order = {route.vehicle_id: route.customer_sequence for route in simulator.state.routes}

    order_event = UrgentOrderEvent(
        trigger_minute=simulator.clock.current_minute,
        order_id="URG-TEST",
        delivery_node=reserved_delivery_node,
        demand=20.0,
        deadline_minutes_after_trigger=90.0,
    )
    simulator.handle_urgent_order(order_event)

    # Full coverage, including the two newly created stops, and no unpaired
    # or out-of-order pickup-and-delivery precedence violations anywhere.
    simulator.state.validate_customer_coverage(simulator.workday)
    final_evaluation = evaluate_state(simulator.state, simulator.workday, simulator.cost_matrix)
    assert final_evaluation.total_precedence_violations == 0
    assert order_event.pickup_customer_id in simulator.state.visited_customer_ids()
    assert order_event.delivery_customer_id in simulator.state.visited_customer_ids()

    for route in simulator.state.routes:
        lock_length = locks_before_order[route.vehicle_id]
        assert route.customer_sequence[:lock_length] == routes_before_order[route.vehicle_id][:lock_length]


# ---------------------------------------------------------------------------
# Full-day benchmark (real street network)
# ---------------------------------------------------------------------------


def test_full_workday_simulation_wall_clock_time_is_dominated_by_reoptimization_budgets(
    malaga_street_network: nx.MultiDiGraph,
) -> None:
    """
    Run a complete simulated 480-minute (8 hour) workday with one traffic
    incident and one urgent order, and confirm the wall-clock time spent
    inside `DynamicSimulator.run` never exceeds the sum of the time budgets
    the triggered re-optimizations were configured with, plus a small,
    fixed allowance for the cost of the tick loop itself. This is what
    proves the minute-by-minute loop stays cheap regardless of workday
    length, with cost concentrated exclusively in the bounded
    re-optimization calls.
    """
    customer_count = 30
    depot_node, all_nodes = select_demonstration_nodes(malaga_street_network, customer_count=customer_count + 1)
    initial_customer_nodes = all_nodes[:customer_count]
    reserved_delivery_node = all_nodes[customer_count]
    cost_matrix = build_cost_matrix(malaga_street_network, depot_node, all_nodes)
    workday = _build_demo_workday(initial_customer_nodes, vehicle_count=4)
    initial_state = build_initial_state(workday, cost_matrix)
    planning_result = run_tabu_search(
        initial_state, workday, cost_matrix, TabuSearchConfig(time_limit_seconds=4.0)
    )

    reoptimization_time_limit_seconds = 1.5
    simulator = DynamicSimulator(
        workday=workday,
        cost_matrix=cost_matrix,
        state=planning_result.best_state,
        clock=SimulationClock(workday_duration_seconds=28800.0, tick_duration_seconds=60.0),
        reoptimization_config=TabuSearchConfig(
            max_iterations=300,
            max_iterations_without_improvement=100,
            time_limit_seconds=reoptimization_time_limit_seconds,
            tabu_tenure=10,
        ),
    )
    busiest_street_edge = max(
        cost_matrix.origin_indices_by_edge, key=lambda edge: len(cost_matrix.origin_indices_by_edge[edge])
    )
    simulator.schedule_event(
        TrafficIncidentEvent(
            trigger_minute=30, first_node=busiest_street_edge[0], second_node=busiest_street_edge[1]
        )
    )
    simulator.schedule_event(
        UrgentOrderEvent(
            trigger_minute=90, order_id="URG-BENCH", delivery_node=reserved_delivery_node, demand=15.0
        )
    )

    simulation_started_at = time.perf_counter()
    simulator.run()
    simulation_elapsed_seconds = time.perf_counter() - simulation_started_at

    reoptimization_time_budget_seconds = sum(entry.elapsed_seconds for entry in simulator.telemetry_log)
    tick_loop_overhead_allowance_seconds = 2.0

    print(
        f"\n[simulation benchmark] customers={customer_count} vehicles=4 "
        f"minutes={simulator.clock.workday_duration_seconds / 60.0:.0f} "
        f"reoptimizations={len(simulator.telemetry_log)} "
        f"total_elapsed={simulation_elapsed_seconds:.3f}s "
        f"reoptimization_budget={reoptimization_time_budget_seconds:.3f}s"
    )

    assert len(simulator.telemetry_log) >= 2
    assert simulation_elapsed_seconds <= reoptimization_time_budget_seconds + tick_loop_overhead_allowance_seconds

    final_evaluation = evaluate_state(simulator.state, simulator.workday, simulator.cost_matrix)
    assert final_evaluation.total_precedence_violations == 0
