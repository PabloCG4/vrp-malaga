"""
Live, minute-by-minute simulation orchestration for an 'ACTIVE' workday plan.

This module is the real-time counterpart of `services/workday_service.py`'s
static, one-shot `optimize_workday_plan`: instead of computing a plan once,
`LiveSimulationSession` replays it against `simulation.engine.DynamicSimulator`
on an accelerated clock, broadcasting telemetry to every connected WebSocket
client and accepting real-time disruption injections (traffic incidents,
urgent same-day orders) from the REST layer.

Design overview
----------------
- `LiveSimulationManager` is a process-wide registry holding at most one
  `LiveSimulationSession` per workday plan identifier, created lazily on
  first use (by either the WebSocket endpoint or a REST event-injection
  call, whichever happens first) and driven by its own `asyncio.Task`.
- `LiveSimulationSession` owns exactly one `DynamicSimulator` instance and
  advances it one simulated minute at a time, `await asyncio.sleep`-ing
  between ticks so the FastAPI event loop stays free for other requests.
  Every call into the CPU-bound solver (`run_tabu_search`, `evaluate_state`,
  and `DynamicSimulator.handle_traffic_incident`/`handle_urgent_order`,
  which themselves call `run_tabu_search`) is off-loaded to a worker thread
  via `asyncio.to_thread`, and every mutation of the simulator's state is
  guarded by an `asyncio.Lock`, so a concurrent REST injection can never
  race with an in-flight tick.
- Every dynamic event is dispatched through `DynamicSimulator`'s existing,
  unmodified public methods (`handle_traffic_incident`, `handle_urgent_order`),
  never through the pre-scheduling/`run()` mechanism that engine module
  offers for offline batch simulation (`scripts/seed_db.py`'s use case):
  a REST-injected disruption is a genuinely real-time event, so this session
  applies it immediately (the very next tick, or before, if a client's
  request lands mid-tick and the lock is free) rather than queuing it for a
  pre-determined trigger minute.
- Street reopening is the one exception `DynamicSimulator`'s public surface
  does not expose on its own (its `run()` loop is the only consumer of that
  bookkeeping, and it is private). This session therefore tracks scheduled
  reopenings itself, in `_pending_reopenings`, and processes them each tick
  using only `CostMatrix.reopen_streets`, `FleetTracker.compute_locked_prefix_lengths`,
  and `run_tabu_search`, the same public building blocks
  `DynamicSimulator._handle_street_reopen` itself is built from, deliberately
  always re-optimizing every route's locked-aware remainder on a reopening
  rather than replicating the engine's private "only re-optimize provably
  affected vehicles" cost-comparison shortcut, a reasonable simplification
  given how infrequently a reopening actually occurs.
- Persistence follows the same "delete and reinsert the whole sequence"
  strategy `services/workday_service.optimize_workday_plan` already uses for
  the static case, since a re-optimization can, in principle, touch any
  unlocked stop of any route: `_rewrite_route_stops` always rewrites every
  `RouteStop` row of the plan from the simulator's current state, and
  `_sync_actual_telemetry` fills in `actual_arrival_seconds`/`departure_seconds`
  for whatever the *locked* (already-committed, provably unchanged) prefix
  of each route has genuinely reached, every tick, independent of whether a
  disruption occurred. Because a rewrite always resets those two columns to
  `NULL` before `_sync_actual_telemetry` runs again immediately afterwards,
  already-realized telemetry for the locked prefix is naturally and safely
  restored without ever needing to be captured and copied across the
  rewrite by hand.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from uuid import uuid4

import networkx as nx
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.websockets import WebSocket

from ...db.enums import RouteStopType, SimulationEventType, WorkdayStatus
from ...db.models import Order, RouteStop, SimulationEvent, Vehicle, WorkdayPlan
from ...db.session import get_session_factory
from ...domain.entities import Route, VRPState
from ...simulation.clock import SimulationClock
from ...simulation.engine import DynamicSimulator, ReoptimizationTelemetry
from ...simulation.events import TrafficIncidentEvent, UrgentOrderEvent
from ...simulation.fleet_tracker import FleetTracker
from ...solver.evaluator import evaluate_route, evaluate_state
from ...solver.metaheuristic import run_tabu_search
from ...topology.matrix import CostMatrix, EdgeIdentifier, build_cost_matrix, find_street_edges, select_demonstration_nodes
from .network_provider import get_depot_node, get_street_network_graph
from .solver_bridge import build_workday_instance
from .workday_service import get_workday_plan, list_active_vehicles

# One real-world second per simulated minute by default: a full 8-hour (480
# minute) workday plays out in 8 real minutes, fast enough to feel "live"
# during a demonstration while still visibly progressing tick by tick,
# rather than resolving instantly like the offline batch simulation does.
DEFAULT_TICK_INTERVAL_SECONDS: float = 1.0

# `CostMatrix` is a fixed-size, precomputed structure (see
# `topology.matrix.CostMatrix`'s module docstring): it cannot grow a new
# row/column once a live session is running. A real-time `UrgentOrderEvent`
# can therefore only ever target a node that was already declared one of the
# matrix's "nodes of interest" at session-creation time. This mirrors the
# reserved-node pool `simulation.engine`'s own demonstration entry point
# draws for exactly this reason (see its `__main__` block), just resolved
# once per live session instead of once per demo script run.
RESERVED_URGENT_ORDER_NODE_COUNT: int = 40
_RESERVED_NODE_SAMPLING_SEED: int = 731_2026

# `db.session.get_session_factory` caches a single, process-wide engine
# resolved from `DATABASE_URL` the first time it is called, exactly like
# `scripts/seed_db.py` relies on for production use. Every background
# database access this module performs (it never receives a request-scoped
# `AsyncSession` from FastAPI's dependency injection, since it runs from a
# long-lived task rather than from within a single request) is routed
# through this thin, overridable indirection instead of calling
# `get_session_factory` directly, so integration tests can point live
# simulation sessions at a temporary engine via `set_session_factory_for_testing`.
_session_factory_override: async_sessionmaker[AsyncSession] | None = None


def _resolve_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the overridden session factory, if any, otherwise the production one."""
    return _session_factory_override or get_session_factory()


def set_session_factory_for_testing(session_factory: async_sessionmaker[AsyncSession] | None) -> None:
    """Override (or, passed `None`, reset) the session factory used by live simulation sessions."""
    global _session_factory_override
    _session_factory_override = session_factory


class LiveSimulationError(Exception):
    """Base class for every error raised by the live simulation layer."""


class WorkdayNotActiveError(LiveSimulationError):
    """Raised when a live simulation is requested for a plan that is not ACTIVE."""


class StreetNotAdjacentError(LiveSimulationError):
    """Raised when an injected traffic incident's two nodes share no street edge."""


class NoVehicleAvailableError(LiveSimulationError):
    """Raised when an injected urgent order cannot be assigned to any active vehicle."""


class DeliveryNodeNotInNetworkError(LiveSimulationError):
    """Raised when an urgent order's delivery node is not one of this session's reserved nodes."""


def get_reserved_urgent_order_nodes(
    street_network_graph: nx.MultiDiGraph, depot_node: int, orders: list[Order]
) -> tuple[int, ...]:
    """
    Return the deterministic pool of extra nodes a live session reserves for urgent orders.

    Sampled once, deterministically (fixed random seed), from every node of
    the street network, excluding the depot and every node already used by
    one of the plan's orders. Exposed as a public function so REST clients
    (and this module's own tests) can discover, ahead of time, which nodes a
    `POST .../events/urgent-order` call may legally target for a given plan.
    """
    order_nodes = {order.node_id for order in orders}
    _, sampled_nodes = select_demonstration_nodes(
        street_network_graph, RESERVED_URGENT_ORDER_NODE_COUNT, random_seed=_RESERVED_NODE_SAMPLING_SEED
    )
    return tuple(node_id for node_id in sampled_nodes if node_id != depot_node and node_id not in order_nodes)


def _build_live_cost_matrix(street_network_graph: nx.MultiDiGraph, depot_node: int, orders: list[Order]) -> CostMatrix:
    """Build the live session's cost matrix, covering every order node plus the reserved urgent-order pool."""
    reserved_nodes = get_reserved_urgent_order_nodes(street_network_graph, depot_node, orders)
    order_nodes = {order.node_id for order in orders}
    distinct_customer_nodes = sorted((order_nodes | set(reserved_nodes)) - {depot_node})
    return build_cost_matrix(street_network_graph, depot_node, distinct_customer_nodes)


def _build_state_from_route_stops(
    route_stops: list[RouteStop], vehicles: list[Vehicle], orders_by_customer_id: dict[str, Order]
) -> VRPState:
    """
    Reconstruct the `VRPState` a workday plan's persisted `RouteStop` rows encode.

    Groups the plan's route stops by vehicle, orders them by `sequence_order`,
    drops the synthetic `DEPOT_START`/`DEPOT_END` entries (which `Route` never
    stores explicitly), and resolves each remaining stop's `order_id` back to
    the `Customer.customer_id` (`str(order.id)`) `build_workday_instance`
    assigned it, giving `DynamicSimulator` the exact same starting point
    `optimize_workday_plan` last committed to the database.

    A vehicle with no persisted stops at all is represented by an empty,
    undispatched `Route`, which is a valid state `VRPState` accepts.
    """
    order_id_to_customer_id = {order.id: customer_id for customer_id, order in orders_by_customer_id.items()}

    stops_by_vehicle_row_id: dict[int, list[RouteStop]] = {}
    for stop in route_stops:
        stops_by_vehicle_row_id.setdefault(stop.vehicle_id, []).append(stop)

    routes: list[Route] = []
    for vehicle in vehicles:
        ordered_stops = sorted(stops_by_vehicle_row_id.get(vehicle.id, []), key=lambda stop: stop.sequence_order)
        customer_sequence = tuple(
            order_id_to_customer_id[stop.order_id]
            for stop in ordered_stops
            if stop.stop_type not in (RouteStopType.DEPOT_START, RouteStopType.DEPOT_END)
        )
        routes.append(Route(vehicle_id=str(vehicle.id), customer_sequence=customer_sequence))

    return VRPState(routes=tuple(routes))


def _reoptimization_payload(workday_plan_id: int, telemetry: ReoptimizationTelemetry) -> dict[str, Any]:
    """Build the WebSocket "reoptimization" message payload for one telemetry record."""
    return {
        "type": "reoptimization",
        "workday_plan_id": workday_plan_id,
        "trigger_description": telemetry.trigger_description,
        "triggered_at_minute": telemetry.triggered_at_minute,
        "iterations_completed": telemetry.iterations_completed,
        "elapsed_seconds": telemetry.elapsed_seconds,
        "cost_before": telemetry.cost_before,
        "cost_after": telemetry.cost_after,
        "feasible_before": telemetry.feasible_before,
        "feasible_after": telemetry.feasible_after,
        "locked_prefixes_respected": telemetry.locked_prefixes_respected,
    }


class LiveSimulationSession:
    """
    Drives one workday plan's `DynamicSimulator` on an accelerated, real-time clock.

    Every public method that mutates the simulator (`inject_traffic_incident`,
    `inject_urgent_order`, and the internal tick loop) acquires `self.lock`
    for its whole duration, which is what makes it safe for a REST request
    and the background tick task to touch the same `DynamicSimulator`
    instance concurrently.
    """

    def __init__(
        self,
        workday_plan_id: int,
        simulator: DynamicSimulator,
        orders_by_customer_id: dict[str, Order],
        vehicle_row_id_by_domain_id: dict[str, int],
        tick_interval_seconds: float,
    ) -> None:
        self.workday_plan_id = workday_plan_id
        self.simulator = simulator
        self.orders_by_customer_id = orders_by_customer_id
        self.vehicle_row_id_by_domain_id = vehicle_row_id_by_domain_id
        self.tick_interval_seconds = tick_interval_seconds

        self.lock = asyncio.Lock()
        self.subscribers: set[WebSocket] = set()
        self._pending_reopenings: list[tuple[int, tuple[EdgeIdentifier, ...]]] = []
        self._task: asyncio.Task[None] | None = None

    @classmethod
    async def create(
        cls, workday_plan_id: int, tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS
    ) -> "LiveSimulationSession":
        """
        Build a session for an ACTIVE workday plan, resuming from its persisted route stops.

        Raises
        ------
        WorkdayNotFoundError
            If no plan with `workday_plan_id` exists.
        WorkdayNotActiveError
            If the plan's status is not currently ACTIVE.
        """
        session_factory = _resolve_session_factory()
        async with session_factory() as session:
            workday_plan = await get_workday_plan(session, workday_plan_id)
            if workday_plan.status != WorkdayStatus.ACTIVE:
                message = f"Workday plan {workday_plan_id} is '{workday_plan.status.value}', not 'ACTIVE'."
                raise WorkdayNotActiveError(message)

            orders = list(workday_plan.orders)
            route_stops = list(workday_plan.route_stops)
            vehicles = await list_active_vehicles(session)

        street_network_graph = get_street_network_graph()
        depot_node = get_depot_node()
        workday, orders_by_customer_id = build_workday_instance(orders, vehicles)
        cost_matrix = _build_live_cost_matrix(street_network_graph, depot_node, orders)
        state = _build_state_from_route_stops(route_stops, vehicles, orders_by_customer_id)

        simulator = DynamicSimulator(
            workday=workday, cost_matrix=cost_matrix, state=state, clock=SimulationClock(tick_duration_seconds=60.0)
        )

        vehicle_row_id_by_domain_id = {str(vehicle.id): vehicle.id for vehicle in vehicles}

        return cls(
            workday_plan_id=workday_plan_id,
            simulator=simulator,
            orders_by_customer_id=orders_by_customer_id,
            vehicle_row_id_by_domain_id=vehicle_row_id_by_domain_id,
            tick_interval_seconds=tick_interval_seconds,
        )

    def start(self) -> None:
        """Start the background tick task, if it is not already running."""
        if self._task is None:
            self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Cancel the background tick task and wait for it to unwind."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # -- WebSocket subscriber management -----------------------------------

    async def add_subscriber(self, websocket: WebSocket) -> None:
        """Register a connected client and immediately send it the current snapshot."""
        self.subscribers.add(websocket)
        await websocket.send_json(self._build_state_payload("snapshot"))

    async def remove_subscriber(self, websocket: WebSocket) -> None:
        """Unregister a disconnected or closed client."""
        self.subscribers.discard(websocket)

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        """Send a JSON payload to every connected client, dropping any that fail to receive it."""
        dead_sockets: list[WebSocket] = []
        for websocket in list(self.subscribers):
            try:
                await websocket.send_json(payload)
            except Exception:  # noqa: BLE001  A broken connection must never abort the simulation loop.
                dead_sockets.append(websocket)
        for websocket in dead_sockets:
            self.subscribers.discard(websocket)

    def _build_state_payload(self, message_type: str) -> dict[str, Any]:
        """Build a full fleet/clock telemetry message of the given type."""
        clock = self.simulator.clock
        fleet_snapshot = self.simulator.fleet_snapshot()
        return {
            "type": message_type,
            "workday_plan_id": self.workday_plan_id,
            "clock": {
                "current_minute": clock.current_minute,
                "current_time_seconds": clock.current_time_seconds,
                "formatted_time": clock.formatted_timestamp(),
                "is_finished": clock.is_finished,
            },
            "vehicles": [
                {
                    "vehicle_id": snapshot.vehicle_id,
                    "status": snapshot.status.value,
                    "current_node": snapshot.current_node,
                    "next_node": snapshot.next_node,
                    "active_customer_id": snapshot.active_customer_id,
                    "locked_prefix_length": snapshot.locked_prefix_length,
                }
                for snapshot in fleet_snapshot.values()
            ],
        }

    # -- Persistence ----------------------------------------------------------

    async def _rewrite_route_stops(self, session: AsyncSession) -> None:
        """Replace every `RouteStop` of this plan with the simulator's current best state."""
        await session.execute(delete(RouteStop).where(RouteStop.workday_plan_id == self.workday_plan_id))

        for route in self.simulator.state.routes:
            route_evaluation = self.simulator.route_evaluations_by_vehicle_id.get(route.vehicle_id)
            if route_evaluation is None or not route_evaluation.is_reachable:
                continue

            vehicle_row_id = self.vehicle_row_id_by_domain_id[route.vehicle_id]
            for position, stop in enumerate(route_evaluation.stop_schedule):
                if stop.customer_id is None:
                    stop_type = RouteStopType.DEPOT_START if position == 0 else RouteStopType.DEPOT_END
                    order_id: int | None = None
                else:
                    order_row = self.orders_by_customer_id[stop.customer_id]
                    stop_type = (
                        RouteStopType.DEPOT_PICKUP if order_row.is_pickup_stop else RouteStopType.CUSTOMER_DELIVERY
                    )
                    order_id = order_row.id

                session.add(
                    RouteStop(
                        workday_plan_id=self.workday_plan_id,
                        vehicle_id=vehicle_row_id,
                        order_id=order_id,
                        sequence_order=position,
                        stop_type=stop_type,
                        node_id=stop.node_id,
                        planned_arrival_seconds=int(round(stop.arrival_time_seconds)),
                        departure_seconds=int(round(stop.departure_time_seconds)),
                    )
                )
        await session.flush()

    async def _sync_actual_telemetry(self, session: AsyncSession) -> None:
        """
        Fill in `actual_arrival_seconds`/`departure_seconds` for stops genuinely reached.

        Bounded to each vehicle's currently locked prefix (`VehicleSnapshot.locked_prefix_length`,
        plus the depot start), which is the exact set of stops guaranteed to be
        bit-identical to whatever the plan looked like before this tick, so a
        stop that merely happens to have an early `arrival_time_seconds` in a
        freshly re-optimized, not-yet-committed tail is never mistaken for one
        the fleet has actually already visited.
        """
        current_time_seconds = self.simulator.clock.current_time_seconds
        fleet_snapshot = self.simulator.fleet_snapshot()

        for route in self.simulator.state.routes:
            route_evaluation = self.simulator.route_evaluations_by_vehicle_id.get(route.vehicle_id)
            if route_evaluation is None or not route_evaluation.is_reachable:
                continue

            snapshot = fleet_snapshot[route.vehicle_id]
            vehicle_row_id = self.vehicle_row_id_by_domain_id[route.vehicle_id]
            safe_position_count = min(snapshot.locked_prefix_length + 1, len(route_evaluation.stop_schedule))

            for position in range(safe_position_count):
                stop = route_evaluation.stop_schedule[position]
                if current_time_seconds < stop.arrival_time_seconds:
                    break
                await session.execute(
                    update(RouteStop)
                    .where(
                        RouteStop.workday_plan_id == self.workday_plan_id,
                        RouteStop.vehicle_id == vehicle_row_id,
                        RouteStop.sequence_order == position,
                        RouteStop.actual_arrival_seconds.is_(None),
                    )
                    .values(
                        actual_arrival_seconds=int(round(stop.arrival_time_seconds)),
                        departure_seconds=int(round(stop.departure_time_seconds)),
                    )
                )

    async def _persist_urgent_order_pair(self, session: AsyncSession, event: UrgentOrderEvent) -> None:
        """Persist the depot pickup and customer delivery `Order` rows of a newly injected VRPPD pair."""
        workday = self.simulator.workday
        pickup_customer = workday.customers_by_id[event.pickup_customer_id]
        delivery_customer = workday.customers_by_id[event.delivery_customer_id]

        street_network_graph = self.simulator.cost_matrix.street_network_graph
        depot_node = self.simulator.cost_matrix.depot_node
        depot_attributes = street_network_graph.nodes[depot_node]
        delivery_attributes = street_network_graph.nodes[delivery_customer.node_id]

        delivery_order = Order(
            workday_plan_id=self.workday_plan_id,
            customer_name=f"Urgent order {event.order_id}",
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
            workday_plan_id=self.workday_plan_id,
            customer_name=f"Urgent order {event.order_id} (depot pickup)",
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

        self.orders_by_customer_id[pickup_customer.customer_id] = pickup_order
        self.orders_by_customer_id[delivery_customer.customer_id] = delivery_order

    # -- Real-time event injection --------------------------------------------

    async def inject_traffic_incident(
        self,
        first_node: int,
        second_node: int,
        reopen_after_minutes: int | None = None,
        description: str = "Traffic incident",
    ) -> int:
        """
        Close the street between two adjacent nodes and re-optimize any affected route.

        Returns
        -------
        int
            The simulated minute at which the incident was applied.

        Raises
        ------
        StreetNotAdjacentError
            If the two nodes share no street edge in the network.
        """
        async with self.lock:
            street_network_graph = self.simulator.cost_matrix.street_network_graph
            closed_edges = find_street_edges(street_network_graph, first_node, second_node)
            if not closed_edges:
                message = f"Nodes {first_node} and {second_node} are not adjacent in the street network."
                raise StreetNotAdjacentError(message)

            trigger_minute = self.simulator.clock.current_minute
            event = TrafficIncidentEvent(
                trigger_minute=trigger_minute,
                first_node=first_node,
                second_node=second_node,
                # Reopening is tracked independently by this session (see the
                # module docstring), never through the simulator's own,
                # `run()`-only reopening mechanism.
                reopen_minute=None,
                description=description,
            )

            await asyncio.to_thread(self.simulator.handle_traffic_incident, event)

            if reopen_after_minutes is not None:
                reopen_at_minute = trigger_minute + reopen_after_minutes
                self._pending_reopenings.append((reopen_at_minute, closed_edges))
                self._pending_reopenings.sort(key=lambda scheduled: scheduled[0])

            payload = {
                "first_node": first_node,
                "second_node": second_node,
                "reopen_after_minutes": reopen_after_minutes,
                "description": description,
            }
            async with _resolve_session_factory()() as session:
                session.add(
                    SimulationEvent(
                        workday_plan_id=self.workday_plan_id,
                        event_type=SimulationEventType.TRAFFIC_INCIDENT,
                        trigger_minute=trigger_minute,
                        payload_json=payload,
                    )
                )
                await self._rewrite_route_stops(session)
                await self._sync_actual_telemetry(session)
                await session.commit()

            await self._broadcast_injected_event(SimulationEventType.TRAFFIC_INCIDENT, trigger_minute, payload)
            return trigger_minute

    async def inject_urgent_order(
        self,
        delivery_node: int,
        demand: float,
        order_id: str | None = None,
        pickup_service_time_seconds: float = 180.0,
        delivery_service_time_seconds: float = 300.0,
        deadline_minutes_after_trigger: float = 90.0,
        description: str = "Urgent order",
    ) -> tuple[str, int]:
        """
        Insert a same-day depot-pickup-and-delivery pair and re-optimize its placement.

        Returns
        -------
        tuple[str, int]
            The resolved order identifier and the simulated minute it was applied at.

        Raises
        ------
        DeliveryNodeNotInNetworkError
            If `delivery_node` is not one of this session's reserved urgent-order nodes
            (see `get_reserved_urgent_order_nodes`).
        NoVehicleAvailableError
            If the active fleet has no route the order could be appended to.
        """
        async with self.lock:
            if delivery_node not in self.simulator.cost_matrix.matrix_index_by_node:
                message = (
                    f"Node {delivery_node} is not a valid urgent-order delivery target for this session; "
                    "call GET /api/v1/workdays/{id}/events/urgent-order-nodes for the eligible set."
                )
                raise DeliveryNodeNotInNetworkError(message)

            resolved_order_id = order_id or f"URG-{uuid4().hex[:8].upper()}"
            trigger_minute = self.simulator.clock.current_minute
            event = UrgentOrderEvent(
                trigger_minute=trigger_minute,
                order_id=resolved_order_id,
                delivery_node=delivery_node,
                demand=demand,
                pickup_service_time_seconds=pickup_service_time_seconds,
                delivery_service_time_seconds=delivery_service_time_seconds,
                deadline_minutes_after_trigger=deadline_minutes_after_trigger,
                description=description,
            )

            await asyncio.to_thread(self.simulator.handle_urgent_order, event)

            if event.pickup_customer_id not in self.simulator.workday.customers_by_id:
                message = f"No active vehicle is available to serve urgent order '{resolved_order_id}'."
                raise NoVehicleAvailableError(message)

            payload = {
                "order_id": resolved_order_id,
                "delivery_node": delivery_node,
                "demand": demand,
                "pickup_service_time_seconds": pickup_service_time_seconds,
                "delivery_service_time_seconds": delivery_service_time_seconds,
                "deadline_minutes_after_trigger": deadline_minutes_after_trigger,
                "description": description,
            }
            async with _resolve_session_factory()() as session:
                await self._persist_urgent_order_pair(session, event)
                session.add(
                    SimulationEvent(
                        workday_plan_id=self.workday_plan_id,
                        event_type=SimulationEventType.URGENT_ORDER,
                        trigger_minute=trigger_minute,
                        payload_json=payload,
                    )
                )
                await self._rewrite_route_stops(session)
                await self._sync_actual_telemetry(session)
                await session.commit()

            await self._broadcast_injected_event(SimulationEventType.URGENT_ORDER, trigger_minute, payload)
            return resolved_order_id, trigger_minute

    async def _broadcast_injected_event(
        self, event_type: SimulationEventType, trigger_minute: int, payload: dict[str, Any]
    ) -> None:
        """Notify every connected client of a dispatched event, its re-optimization, and the new state."""
        await self._broadcast(
            {
                "type": "event",
                "workday_plan_id": self.workday_plan_id,
                "event_type": event_type.value,
                "trigger_minute": trigger_minute,
                "payload": payload,
            }
        )
        latest_telemetry = self.simulator.telemetry_log[-1] if self.simulator.telemetry_log else None
        if latest_telemetry is not None and latest_telemetry.triggered_at_minute == trigger_minute:
            await self._broadcast(_reoptimization_payload(self.workday_plan_id, latest_telemetry))
        await self._broadcast(self._build_state_payload("tick"))

    # -- Tick loop --------------------------------------------------------------

    async def _process_due_reopenings(self) -> None:
        """Reopen every street closure whose scheduled reopening minute has arrived."""
        current_minute = self.simulator.clock.current_minute
        due = [edges for minute, edges in self._pending_reopenings if minute <= current_minute]
        self._pending_reopenings = [
            (minute, edges) for minute, edges in self._pending_reopenings if minute > current_minute
        ]
        for closed_edges in due:
            await self._reopen_and_reoptimize(closed_edges)

    async def _reopen_and_reoptimize(self, closed_edges: tuple[EdgeIdentifier, ...]) -> None:
        """Reopen a street and re-optimize every route's unlocked remainder."""
        simulator = self.simulator

        await asyncio.to_thread(simulator.cost_matrix.reopen_streets, closed_edges)

        locked_prefix_lengths = FleetTracker.compute_locked_prefix_lengths(
            simulator.state.routes, simulator.route_evaluations_by_vehicle_id, simulator.clock.current_time_seconds
        )
        state_before = simulator.state
        evaluation_before = evaluate_state(
            simulator.state, simulator.workday, simulator.cost_matrix, simulator.evaluation_weights
        )

        result = await asyncio.to_thread(
            run_tabu_search,
            simulator.state,
            simulator.workday,
            simulator.cost_matrix,
            simulator.reoptimization_config,
            locked_prefix_lengths,
        )

        locked_prefixes_respected = all(
            result.best_state.routes[index].customer_sequence[: locked_prefix_lengths.get(route.vehicle_id, 0)]
            == route.customer_sequence[: locked_prefix_lengths.get(route.vehicle_id, 0)]
            for index, route in enumerate(state_before.routes)
        )

        simulator.state = result.best_state
        simulator.route_evaluations_by_vehicle_id = {
            route.vehicle_id: evaluate_route(route, simulator.workday, simulator.cost_matrix)
            for route in simulator.state.routes
        }
        telemetry = ReoptimizationTelemetry(
            triggered_at_minute=simulator.clock.current_minute,
            trigger_description="Street reopened",
            iterations_completed=result.iterations_completed,
            elapsed_seconds=result.elapsed_seconds,
            cost_before=evaluation_before.total_cost,
            cost_after=result.best_evaluation.total_cost,
            feasible_before=evaluation_before.is_feasible,
            feasible_after=result.best_evaluation.is_feasible,
            locked_prefixes_respected=locked_prefixes_respected,
        )
        simulator.telemetry_log.append(telemetry)

        async with _resolve_session_factory()() as session:
            await self._rewrite_route_stops(session)
            await self._sync_actual_telemetry(session)
            await session.commit()

        await self._broadcast(_reoptimization_payload(self.workday_plan_id, telemetry))
        await self._broadcast(self._build_state_payload("tick"))

    async def _run_loop(self) -> None:
        """Advance the simulated clock one minute at a time until the workday finishes."""
        try:
            await self._broadcast(self._build_state_payload("tick"))
            while not self.simulator.clock.is_finished:
                await asyncio.sleep(self.tick_interval_seconds)
                async with self.lock:
                    self.simulator.clock.advance()
                    await self._process_due_reopenings()
                    async with _resolve_session_factory()() as session:
                        await self._sync_actual_telemetry(session)
                        await session.commit()
                    await self._broadcast(self._build_state_payload("tick"))
            await self._finalize()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001  Surface unexpected failures to connected clients before propagating.
            await self._broadcast({"type": "error", "workday_plan_id": self.workday_plan_id, "detail": str(error)})
            raise

    async def _finalize(self) -> None:
        """Mark the workday plan COMPLETED, broadcast a final message, and deregister this session."""
        simulator = self.simulator
        final_evaluation = evaluate_state(simulator.state, simulator.workday, simulator.cost_matrix, simulator.evaluation_weights)

        async with _resolve_session_factory()() as session:
            workday_plan = await session.get(WorkdayPlan, self.workday_plan_id)
            if workday_plan is not None:
                workday_plan.status = WorkdayStatus.COMPLETED
                workday_plan.total_cost = final_evaluation.total_cost
                workday_plan.total_distance_km = final_evaluation.total_distance_meters / 1000.0
                await session.commit()

        await self._broadcast(
            {
                "type": "finished",
                "workday_plan_id": self.workday_plan_id,
                "final_cost": final_evaluation.total_cost,
                "is_feasible": final_evaluation.is_feasible,
            }
        )
        live_simulation_manager.discard(self.workday_plan_id)


class LiveSimulationManager:
    """Process-wide registry of running `LiveSimulationSession`s, at most one per workday plan."""

    def __init__(self) -> None:
        self._sessions: dict[int, LiveSimulationSession] = {}
        self._creation_lock = asyncio.Lock()

    def get_session(self, workday_plan_id: int) -> LiveSimulationSession | None:
        """Return the running session for a workday plan, if any, without creating one."""
        return self._sessions.get(workday_plan_id)

    async def get_or_create_session(
        self, workday_plan_id: int, tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS
    ) -> LiveSimulationSession:
        """
        Return the running session for a workday plan, creating and starting one if needed.

        Safe to call concurrently from both the WebSocket endpoint and the
        REST event-injection endpoints: a per-manager creation lock ensures
        at most one session is ever built for a given `workday_plan_id`,
        regardless of which entry point reaches it first.

        Raises
        ------
        WorkdayNotFoundError
            If no plan with `workday_plan_id` exists.
        WorkdayNotActiveError
            If the plan's status is not currently ACTIVE.
        """
        existing = self._sessions.get(workday_plan_id)
        if existing is not None:
            return existing

        async with self._creation_lock:
            existing = self._sessions.get(workday_plan_id)
            if existing is not None:
                return existing

            session = await LiveSimulationSession.create(workday_plan_id, tick_interval_seconds)
            session.start()
            self._sessions[workday_plan_id] = session
            return session

    def discard(self, workday_plan_id: int) -> None:
        """Deregister a session that has already stopped itself after its workday finished."""
        self._sessions.pop(workday_plan_id, None)

    async def remove_session(self, workday_plan_id: int) -> None:
        """Forcibly stop and deregister a session, for example during application shutdown."""
        session = self._sessions.pop(workday_plan_id, None)
        if session is not None:
            await session.stop()

    async def shutdown_all(self) -> None:
        """Stop and deregister every running session."""
        for workday_plan_id in list(self._sessions):
            await self.remove_session(workday_plan_id)


live_simulation_manager = LiveSimulationManager()

__all__ = [
    "DEFAULT_TICK_INTERVAL_SECONDS",
    "LiveSimulationError",
    "WorkdayNotActiveError",
    "StreetNotAdjacentError",
    "NoVehicleAvailableError",
    "LiveSimulationSession",
    "LiveSimulationManager",
    "live_simulation_manager",
    "set_session_factory_for_testing",
]
