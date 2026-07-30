"""
Event-driven, minute-by-minute dynamic simulation engine for the Rich VRP solver.

`DynamicSimulator` plays out a workday that was already planned by the
Phase 2.3/2.4 static pipeline (`build_initial_state` followed by
`run_tabu_search`) against an accelerated clock (`simulation.clock.SimulationClock`),
injecting two kinds of disruption along the way (`simulation.events`):

- A `TrafficIncidentEvent` closes (and, optionally, later reopens) a street,
  updating the shared `CostMatrix` in place through the incremental patching
  mechanism of `backend/src/topology/matrix.py`.
- An `UrgentOrderEvent` introduces a same-day delivery that must first be
  picked up at the depot, modelled as a paired depot-pickup and
  customer-delivery `Customer` (see `backend/src/domain/entities.py`),
  appended to a chosen vehicle's route and left completely free for
  `run_tabu_search` to place, subject only to the hard precedence penalty in
  `backend/src/solver/evaluator.py`.

Every disruption is followed by a bounded, real-time re-optimization: the
engine derives `locked_prefix_lengths` from `simulation.fleet_tracker.FleetTracker`,
so that whatever a vehicle has already driven, or is currently driving
towards, is never rearranged, and calls `run_tabu_search` with a tight time
budget over the still-mutable remainder of every route.

Design note on locking unaffected vehicles
-------------------------------------------
A traffic incident or an urgent order rarely affects every vehicle in the
fleet. Rather than introducing a separate mechanism to restrict which routes
`run_tabu_search` considers, this engine reuses the existing
`locked_prefix_lengths` contract itself: a vehicle whose route cost is
provably unchanged by the disruption (checked with the same
`evaluator.evaluate_route_cost` fast path the search uses internally) is
locked in full, i.e. `locked_prefix_lengths[vehicle_id] = len(customer_sequence)`,
which makes every one of its positions immutable for that search. This keeps
the single, already thoroughly tested `run_tabu_search` entry point as the
only optimization code path in the system, rather than duplicating its
neighborhood generation for a "subset of vehicles" variant.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..domain.entities import Customer, Route, TimeWindow, VRPState, WorkdayInstance
from ..solver.evaluator import (
    EvaluationWeights,
    RouteEvaluation,
    evaluate_route,
    evaluate_route_cost,
    evaluate_state,
)
from ..solver.metaheuristic import TabuSearchConfig, run_tabu_search
from ..topology.matrix import CostMatrix, EdgeIdentifier, find_street_edges
from .clock import SimulationClock
from .events import TrafficIncidentEvent, UrgentOrderEvent
from .fleet_tracker import FleetTracker, VehicleSnapshot

DynamicEvent = TrafficIncidentEvent | UrgentOrderEvent

# Absolute and relative tolerances used to decide whether a disruption
# actually changed a route's cost, and therefore whether that vehicle is
# worth reoptimizing. Matches the scale of a single second of travel time,
# several orders of magnitude below the hard penalties, so genuine changes
# are never missed while floating-point noise never triggers a spurious
# re-optimization.
_COST_CHANGE_ABSOLUTE_TOLERANCE: float = 1e-6
_COST_CHANGE_RELATIVE_TOLERANCE: float = 1e-9


@dataclass(frozen=True, slots=True)
class ReoptimizationTelemetry:
    """
    Record of one real-time re-optimization triggered by a dynamic event.

    Attributes
    ----------
    triggered_at_minute:
        Simulated minute, since workday start, at which the triggering event
        fired.
    trigger_description:
        Human-readable label of the event that triggered this re-optimization.
    iterations_completed, elapsed_seconds:
        Diagnostics copied from the underlying `TabuSearchResult`.
    cost_before, cost_after:
        Total weighted cost f(S) of the state immediately before and after
        this re-optimization.
    feasible_before, feasible_after:
        Feasibility of the state immediately before and after.
    locked_prefixes_respected:
        Whether every locked prefix used for this re-optimization is
        bit-identical between the state before and the state after,
        confirming the search never rearranged an already completed or
        en-route stop.
    """

    triggered_at_minute: int
    trigger_description: str
    iterations_completed: int
    elapsed_seconds: float
    cost_before: float
    cost_after: float
    feasible_before: bool
    feasible_after: bool
    locked_prefixes_respected: bool


def _default_reoptimization_config() -> TabuSearchConfig:
    """
    Build the tight, real-time Tabu Search configuration this engine uses.

    A short `time_limit_seconds` is what keeps every re-optimization inside
    the 1.5-3.0 second real-time budget requested for this engine, at the
    cost of accepting a search that may stop well short of full convergence;
    that trade-off is appropriate here because the search is always seeded
    from an already good state (the previous best solution, only locally
    disturbed by one event) rather than from scratch.
    """
    return TabuSearchConfig(
        max_iterations=400,
        max_iterations_without_improvement=120,
        time_limit_seconds=2.0,
        tabu_tenure=12,
    )


def _costs_differ(cost_before: float, cost_after: float) -> bool:
    """Return whether two route costs differ by more than floating-point noise."""
    if math.isinf(cost_before) or math.isinf(cost_after):
        return cost_before != cost_after
    return not math.isclose(
        cost_before, cost_after, rel_tol=_COST_CHANGE_RELATIVE_TOLERANCE, abs_tol=_COST_CHANGE_ABSOLUTE_TOLERANCE
    )


@dataclass(slots=True)
class DynamicSimulator:
    """
    Orchestrates a minute-by-minute simulated workday with dynamic events.

    Attributes
    ----------
    workday:
        Current problem instance. Replaced by a new, extended instance every
        time an `UrgentOrderEvent` introduces a pickup-and-delivery pair.
    cost_matrix:
        Shared cost matrix, patched in place by `TrafficIncidentEvent`
        handling through `CostMatrix.apply_street_closures`/`reopen_streets`.
    state:
        Current best-known candidate solution, updated after every
        re-optimization.
    clock:
        Minute-by-minute simulated clock driving the event loop.
    reoptimization_config:
        Tabu Search configuration used for every real-time re-optimization.
    evaluation_weights:
        Weights defining f(S), shared between every cost computation this
        engine performs and the `run_tabu_search` calls it issues.
    pending_events:
        Dynamic events not yet triggered, kept sorted by `trigger_minute`.
    telemetry_log:
        Chronological record of every re-optimization performed so far.
    """

    workday: WorkdayInstance
    cost_matrix: CostMatrix
    state: VRPState
    clock: SimulationClock = field(default_factory=SimulationClock)
    reoptimization_config: TabuSearchConfig = field(default_factory=_default_reoptimization_config)
    evaluation_weights: EvaluationWeights = field(default_factory=EvaluationWeights)
    pending_events: list[DynamicEvent] = field(default_factory=list)
    telemetry_log: list[ReoptimizationTelemetry] = field(default_factory=list, init=False)
    route_evaluations_by_vehicle_id: dict[str, RouteEvaluation] = field(default_factory=dict, init=False)
    _pending_reopenings: list[tuple[int, tuple[EdgeIdentifier, ...]]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.pending_events = sorted(self.pending_events, key=lambda event: event.trigger_minute)
        self._refresh_route_evaluations()

    def _refresh_route_evaluations(self) -> None:
        """Recompute the cached `RouteEvaluation` of every route after a state change."""
        self.route_evaluations_by_vehicle_id = {
            route.vehicle_id: evaluate_route(route, self.workday, self.cost_matrix) for route in self.state.routes
        }

    def schedule_event(self, event: DynamicEvent) -> None:
        """Add a dynamic event to the pending queue, keeping it sorted by trigger minute."""
        self.pending_events.append(event)
        self.pending_events.sort(key=lambda pending: pending.trigger_minute)

    def fleet_snapshot(self) -> dict[str, VehicleSnapshot]:
        """Return the current real-time state of every vehicle, keyed by vehicle identifier."""
        return FleetTracker.snapshot_fleet(
            self.state.routes, self.route_evaluations_by_vehicle_id, self.clock.current_time_seconds
        )

    def _locked_prefix_lengths(self) -> dict[str, int]:
        """Return the `run_tabu_search`-ready locked prefix mapping for the current instant."""
        return FleetTracker.compute_locked_prefix_lengths(
            self.state.routes, self.route_evaluations_by_vehicle_id, self.clock.current_time_seconds
        )

    def _pop_due_events(self) -> list[DynamicEvent]:
        """Remove and return every pending event due at or before the current minute."""
        current_minute = self.clock.current_minute
        due_events = [event for event in self.pending_events if event.trigger_minute <= current_minute]
        self.pending_events = [event for event in self.pending_events if event.trigger_minute > current_minute]
        return due_events

    def _pop_due_reopenings(self) -> list[tuple[EdgeIdentifier, ...]]:
        """Remove and return the closed-edge sets scheduled to reopen at the current minute."""
        current_minute = self.clock.current_minute
        due = [edges for reopen_minute, edges in self._pending_reopenings if reopen_minute <= current_minute]
        self._pending_reopenings = [
            (reopen_minute, edges) for reopen_minute, edges in self._pending_reopenings if reopen_minute > current_minute
        ]
        return due

    def run(self) -> None:
        """
        Run the simulation to completion, dispatching every scheduled event.

        Advances the clock one minute at a time until the workday elapses,
        triggering every due `TrafficIncidentEvent`, scheduled reopening and
        `UrgentOrderEvent` along the way, and logging every vehicle status
        transition and re-optimization to standard output.
        """
        print(f"[{self.clock.formatted_timestamp()}] Workday started with {len(self.state.routes)} vehicle(s).")
        previous_snapshots = self.fleet_snapshot()

        while not self.clock.is_finished:
            self.clock.advance()

            for edges in self._pop_due_reopenings():
                self._handle_street_reopen(edges)

            for event in self._pop_due_events():
                if isinstance(event, TrafficIncidentEvent):
                    self.handle_traffic_incident(event)
                elif isinstance(event, UrgentOrderEvent):
                    self.handle_urgent_order(event)

            current_snapshots = self.fleet_snapshot()
            for vehicle_id, snapshot in current_snapshots.items():
                previous_status = previous_snapshots.get(vehicle_id)
                if previous_status is None or previous_status.status != snapshot.status:
                    print(
                        f"[{self.clock.formatted_timestamp()}] {vehicle_id}: "
                        f"{'starts' if previous_status is None else previous_status.status.value} -> "
                        f"{snapshot.status.value}"
                        + (f" (customer {snapshot.active_customer_id})" if snapshot.active_customer_id else "")
                    )
            previous_snapshots = current_snapshots

        print(f"[{self.clock.formatted_timestamp()}] Workday finished.")

    def handle_traffic_incident(self, event: TrafficIncidentEvent) -> None:
        """
        Close the street identified by a `TrafficIncidentEvent` and re-optimize.

        Parameters
        ----------
        event:
            The traffic incident due to fire.
        """
        closed_edges = find_street_edges(self.cost_matrix.street_network_graph, event.first_node, event.second_node)
        if not closed_edges:
            print(
                f"[{self.clock.formatted_timestamp()}] {event.description}: nodes {event.first_node} and "
                f"{event.second_node} are not adjacent; ignoring."
            )
            return

        weights = self.evaluation_weights
        cost_before_by_vehicle_id = self._route_costs(weights)

        print(
            f"[{self.clock.formatted_timestamp()}] {event.description}: closing the street between "
            f"{event.first_node} and {event.second_node} ({len(closed_edges)} directed edge(s))."
        )
        patch_report = self.cost_matrix.apply_street_closures(closed_edges)
        print(
            f"    Recomputed {patch_report.recomputed_pair_count} matrix entr{'y' if patch_report.recomputed_pair_count == 1 else 'ies'} "
            f"across {len(patch_report.affected_origin_nodes)} row(s); "
            f"{len(patch_report.newly_unreachable_pairs)} pair(s) newly unreachable."
        )

        if event.reopen_minute is not None:
            self._pending_reopenings.append((event.reopen_minute, closed_edges))
            self._pending_reopenings.sort(key=lambda scheduled: scheduled[0])

        self._reoptimize_if_affected(cost_before_by_vehicle_id, event.description)

    def _handle_street_reopen(self, closed_edges: tuple[EdgeIdentifier, ...]) -> None:
        """Reopen a previously closed street and re-optimize if any route actually benefits."""
        weights = self.evaluation_weights
        cost_before_by_vehicle_id = self._route_costs(weights)

        print(f"[{self.clock.formatted_timestamp()}] Street reopened ({len(closed_edges)} directed edge(s)).")
        patch_report = self.cost_matrix.reopen_streets(closed_edges)
        print(f"    Recomputed {patch_report.recomputed_pair_count} matrix entries across the full matrix.")

        self._reoptimize_if_affected(cost_before_by_vehicle_id, "Street reopened")

    def _route_costs(self, weights: EvaluationWeights) -> dict[str, float]:
        """Return the current weighted cost of every route, keyed by vehicle identifier."""
        return {
            route.vehicle_id: evaluate_route_cost(
                route.customer_sequence, self.workday.fleet_by_vehicle_id[route.vehicle_id], self.workday, self.cost_matrix, weights
            )
            for route in self.state.routes
        }

    def _reoptimize_if_affected(self, cost_before_by_vehicle_id: dict[str, float], description: str) -> None:
        """
        Re-optimize only the vehicles whose route cost actually changed.

        Every vehicle whose cost is provably unaffected by the matrix change
        is locked in full, so `run_tabu_search` only ever rearranges the
        vehicles the disruption could plausibly improve.
        """
        weights = self.evaluation_weights
        cost_after_by_vehicle_id = self._route_costs(weights)

        affected_vehicle_ids = {
            vehicle_id
            for vehicle_id, cost_after in cost_after_by_vehicle_id.items()
            if _costs_differ(cost_before_by_vehicle_id[vehicle_id], cost_after)
        }

        if not affected_vehicle_ids:
            print(f"[{self.clock.formatted_timestamp()}] No route is affected; skipping re-optimization.")
            return

        base_locked_prefix_lengths = self._locked_prefix_lengths()
        locked_prefix_lengths = {
            route.vehicle_id: (
                base_locked_prefix_lengths.get(route.vehicle_id, 0)
                if route.vehicle_id in affected_vehicle_ids
                else len(route.customer_sequence)
            )
            for route in self.state.routes
        }

        self._run_reoptimization(description, locked_prefix_lengths)

    def handle_urgent_order(self, event: UrgentOrderEvent) -> None:
        """
        Insert a mid-day pickup-and-delivery pair and re-optimize its placement.

        A depot pickup and a customer delivery `Customer` are created,
        mutually paired via `Customer.paired_customer_id`, and appended to
        the end of whichever vehicle's route currently offers the cheapest
        marginal insertion, purely as a starting point: neither stop's
        sequence position is locked, so `run_tabu_search` is free to move the
        pair anywhere in the unvisited remainder of any vehicle, guided by
        the hard precedence penalty that keeps the pickup ahead of its
        delivery.

        Parameters
        ----------
        event:
            The urgent order due to arrive.
        """
        weights = self.evaluation_weights
        current_time_seconds = self.clock.current_time_seconds

        # Captured before the pair is inserted, from each vehicle's route as
        # it stands right now: `simulate_route_clock` always plans a route
        # from workday start (t=0), so a previously idle vehicle's freshly
        # appended stops would otherwise risk being computed with a
        # departure time far in the past and misread as already completed.
        # Locking against the pre-insertion route length instead guarantees
        # every position at or beyond it, including both new stops, stays
        # open to `run_tabu_search`, regardless of which vehicle receives
        # the pair or how tight its own time window is.
        locked_prefix_lengths = self._locked_prefix_lengths()

        pickup = Customer(
            node_id=self.cost_matrix.depot_node,
            demand=0.0,
            service_time_seconds=event.pickup_service_time_seconds,
            time_window=TimeWindow(0.0, math.inf),
            customer_id=event.pickup_customer_id,
            is_pickup_stop=True,
            paired_customer_id=event.delivery_customer_id,
        )
        delivery = Customer(
            node_id=event.delivery_node,
            demand=event.demand,
            service_time_seconds=event.delivery_service_time_seconds,
            time_window=TimeWindow(
                start_seconds=current_time_seconds,
                end_seconds=current_time_seconds + event.deadline_minutes_after_trigger * 60.0,
            ),
            customer_id=event.delivery_customer_id,
            is_pickup_stop=False,
            paired_customer_id=event.pickup_customer_id,
        )

        print(
            f"[{self.clock.formatted_timestamp()}] {event.description} '{event.order_id}': depot pickup "
            f"'{pickup.customer_id}' + delivery '{delivery.customer_id}' at node {event.delivery_node}, "
            f"demand={event.demand:.1f}."
        )

        new_workday = WorkdayInstance(customers=self.workday.customers + (pickup, delivery), fleet=self.workday.fleet)

        best_vehicle_id: str | None = None
        best_marginal_cost = math.inf
        for route in self.state.routes:
            vehicle = new_workday.fleet_by_vehicle_id[route.vehicle_id]
            candidate_sequence = route.customer_sequence + (pickup.customer_id, delivery.customer_id)
            candidate_cost = evaluate_route_cost(candidate_sequence, vehicle, new_workday, self.cost_matrix, weights)
            if candidate_cost < best_marginal_cost:
                best_marginal_cost = candidate_cost
                best_vehicle_id = route.vehicle_id

        if best_vehicle_id is None:
            print(f"[{self.clock.formatted_timestamp()}] No vehicle available to serve order '{event.order_id}'; ignoring.")
            return

        print(f"    Tentatively appended to '{best_vehicle_id}'; re-optimizing its placement.")

        updated_routes = tuple(
            Route(
                vehicle_id=route.vehicle_id,
                customer_sequence=route.customer_sequence + (pickup.customer_id, delivery.customer_id),
            )
            if route.vehicle_id == best_vehicle_id
            else route
            for route in self.state.routes
        )

        self.workday = new_workday
        self.state = VRPState(routes=updated_routes)
        self._refresh_route_evaluations()

        self._run_reoptimization(f"{event.description} ({event.order_id})", locked_prefix_lengths)

    def _run_reoptimization(self, description: str, locked_prefix_lengths: dict[str, int]) -> None:
        """Run `run_tabu_search` over the current state, commit its result and log telemetry."""
        weights = self.evaluation_weights
        evaluation_before = evaluate_state(self.state, self.workday, self.cost_matrix, weights)
        state_before = self.state

        config = self.reoptimization_config
        result = run_tabu_search(
            self.state, self.workday, self.cost_matrix, config, locked_prefix_lengths=locked_prefix_lengths
        )

        locked_prefixes_respected = all(
            result.best_state.routes[route_index].customer_sequence[: locked_prefix_lengths.get(route.vehicle_id, 0)]
            == route.customer_sequence[: locked_prefix_lengths.get(route.vehicle_id, 0)]
            for route_index, route in enumerate(state_before.routes)
        )

        telemetry = ReoptimizationTelemetry(
            triggered_at_minute=self.clock.current_minute,
            trigger_description=description,
            iterations_completed=result.iterations_completed,
            elapsed_seconds=result.elapsed_seconds,
            cost_before=evaluation_before.total_cost,
            cost_after=result.best_evaluation.total_cost,
            feasible_before=evaluation_before.is_feasible,
            feasible_after=result.best_evaluation.is_feasible,
            locked_prefixes_respected=locked_prefixes_respected,
        )
        self.telemetry_log.append(telemetry)

        self.state = result.best_state
        self._refresh_route_evaluations()

        print(
            f"    Re-optimized in {telemetry.elapsed_seconds * 1000.0:.1f} ms over "
            f"{telemetry.iterations_completed} iteration(s): f(S) {telemetry.cost_before:.2f} -> "
            f"{telemetry.cost_after:.2f}, feasible={telemetry.feasible_after}, "
            f"locked prefixes respected={telemetry.locked_prefixes_respected}."
        )


if __name__ == "__main__":
    # This module is only meant to be run as `python -m backend.src.simulation.engine`
    # from the repository root, for the same import resolution reason documented in
    # `evaluator.py`'s, `constructive.py`'s and `metaheuristic.py`'s own __main__ blocks.
    import time as time_module

    from ..domain.entities import Vehicle
    from ..solver.constructive import build_initial_state
    from ..topology.extractor import load_processed_graph
    from ..topology.matrix import build_cost_matrix, select_demonstration_nodes

    DEMONSTRATION_CUSTOMER_COUNT: int = 40
    DEMONSTRATION_RESERVED_NODE_COUNT: int = 5
    DEMONSTRATION_VEHICLE_COUNT: int = 5

    print("Loading the preprocessed street network and building its cost matrix.")
    malaga_street_network = load_processed_graph()

    # A pool of extra nodes, beyond the initially assigned customers, is drawn
    # into the cost matrix's nodes of interest but deliberately left without a
    # Customer at workday construction time. This is what lets an
    # UrgentOrderEvent target a genuinely fresh delivery location without
    # requiring the CostMatrix to grow a new row/column at simulation runtime,
    # a reasonable future enhancement kept out of this phase's scope.
    demonstration_depot, all_candidate_nodes = select_demonstration_nodes(
        malaga_street_network, DEMONSTRATION_CUSTOMER_COUNT + DEMONSTRATION_RESERVED_NODE_COUNT
    )
    initial_customer_nodes = all_candidate_nodes[:DEMONSTRATION_CUSTOMER_COUNT]
    reserved_delivery_nodes = all_candidate_nodes[DEMONSTRATION_CUSTOMER_COUNT:]

    demonstration_cost_matrix = build_cost_matrix(malaga_street_network, demonstration_depot, all_candidate_nodes)

    demonstration_workday = WorkdayInstance(
        customers=tuple(
            Customer(
                node_id=node_id,
                demand=float(15 + (index % 4) * 5),
                service_time_seconds=180.0 + 30.0 * (index % 6),
                time_window=TimeWindow(start_seconds=200.0 * index, end_seconds=200.0 * index + 5400.0),
            )
            for index, node_id in enumerate(initial_customer_nodes)
        ),
        fleet=tuple(
            Vehicle(vehicle_id=f"VAN-{vehicle_index + 1}", max_capacity=400.0)
            for vehicle_index in range(DEMONSTRATION_VEHICLE_COUNT)
        ),
    )

    print("Building the initial state and running the static Tabu Search pass.")
    initial_state = build_initial_state(demonstration_workday, demonstration_cost_matrix)
    initial_search_config = TabuSearchConfig(
        max_iterations=400, max_iterations_without_improvement=120, time_limit_seconds=6.0, tabu_tenure=15
    )
    initial_search_result = run_tabu_search(
        initial_state, demonstration_workday, demonstration_cost_matrix, initial_search_config
    )
    print(
        f"  Static plan ready: f(S) = {initial_search_result.best_evaluation.total_cost:.2f}, "
        f"feasible={initial_search_result.best_evaluation.is_feasible}."
    )

    simulator = DynamicSimulator(
        workday=demonstration_workday,
        cost_matrix=demonstration_cost_matrix,
        state=initial_search_result.best_state,
        clock=SimulationClock(workday_duration_seconds=28800.0, tick_duration_seconds=60.0),
    )

    # Schedule one traffic incident on the busiest street of the network,
    # reopened later in the day, and one urgent order targeting a reserved,
    # not-yet-assigned delivery node.
    busiest_street_edge = max(
        demonstration_cost_matrix.origin_indices_by_edge,
        key=lambda edge: len(demonstration_cost_matrix.origin_indices_by_edge[edge]),
    )
    simulator.schedule_event(
        TrafficIncidentEvent(
            trigger_minute=45,
            first_node=busiest_street_edge[0],
            second_node=busiest_street_edge[1],
            reopen_minute=180,
            description="Traffic accident",
        )
    )
    simulator.schedule_event(
        UrgentOrderEvent(
            trigger_minute=90,
            order_id="URG-1",
            delivery_node=reserved_delivery_nodes[0],
            demand=25.0,
            deadline_minutes_after_trigger=120.0,
            description="Same-day urgent order",
        )
    )

    print("\nRunning the dynamic simulation.\n")
    simulation_started_at = time_module.perf_counter()
    simulator.run()
    simulation_elapsed_seconds = time_module.perf_counter() - simulation_started_at

    final_evaluation = evaluate_state(simulator.state, simulator.workday, simulator.cost_matrix)
    print(
        f"\nSimulation wall-clock time: {simulation_elapsed_seconds:.2f} s for a "
        f"{simulator.clock.workday_duration_seconds / 3600.0:.1f} h simulated workday."
    )
    print(f"Final f(S) = {final_evaluation.total_cost:.2f}, feasible={final_evaluation.is_feasible}")
    print(f"Re-optimizations triggered: {len(simulator.telemetry_log)}")
    for telemetry_entry in simulator.telemetry_log:
        print(
            f"  [minute {telemetry_entry.triggered_at_minute:3d}] {telemetry_entry.trigger_description}: "
            f"f(S) {telemetry_entry.cost_before:.2f} -> {telemetry_entry.cost_after:.2f} "
            f"({telemetry_entry.iterations_completed} it., {telemetry_entry.elapsed_seconds * 1000.0:.1f} ms), "
            f"feasible={telemetry_entry.feasible_after}, locks respected={telemetry_entry.locked_prefixes_respected}"
        )
