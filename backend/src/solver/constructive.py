"""
Deterministic constructive heuristic for an initial VRP candidate solution.

This module builds a complete, feasible-as-possible `VRPState` for a
`WorkdayInstance` from scratch, so that the upcoming Tabu Search metaheuristic
always has a valid starting point to improve upon rather than having to
special-case an empty or partial solution on its first iteration.

Algorithm: Parallel Cheapest Feasible Append
----------------------------------------------
The heuristic grows every vehicle's route simultaneously, one customer at a
time: at each step, it considers appending every still-unassigned customer to
the end of every vehicle's current route, and commits the single
(vehicle, customer) pair whose marginal cost is lowest under the exact same
weighted objective the evaluator scores a final solution with
(`EvaluationWeights`, see `backend/src/solver/evaluator.py`). This repeats
until every customer has been assigned.

Restricting insertion to the end of a route, rather than every possible
position (as Solomon's I1 insertion heuristic does), is what keeps this
heuristic fast: appending is the only kind of insertion whose effect on the
route's clock, EU rest breaks and capacity can be evaluated in O(1) from a
handful of running totals, because nothing downstream of the new customer
needs to be re-simulated. Insertion at an arbitrary position would shift the
arrival time of every customer visited afterwards, which can only be
re-validated by re-simulating the rest of the route, turning every candidate
check into an O(route length) operation and the whole construction into
O(customers^4). Since this heuristic only has to produce a *starting* point
for a Tabu Search that is free to relocate and reorder customers afterwards,
trading a small amount of route quality for a guaranteed O(fleet size x
customers^2) running time, small enough to stay in the millisecond range even
for a large workday, is the right trade-off here.

This also explains why the classic Clarke-Wright Savings algorithm was not
chosen: its merge step still requires re-validating full route feasibility
under time windows and rest breaks, which is exactly the O(route length)
re-simulation this design avoids, without the algorithm actually gaining
anything from that cost the way an arbitrary-position insertion heuristic
would. See `docs/TECHNICAL_CHANGELOG.md` for the complete comparison.

Because the same weighted cost function used by the final evaluator drives
every append decision, and because `capacity_penalty_weight` and
`workday_penalty_weight` dominate the other terms by several orders of
magnitude, the search naturally prefers a feasible append whenever one
exists, and only ever accepts an infeasible one when literally no feasible
placement remains for a customer in any route, in which case it settles for
the least expensive violation available. This guarantees 100 percent customer
coverage, the structural contract of `VRPState`, under any fleet and demand
combination, while still steering towards feasibility whenever it is
achievable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .evaluator import EvaluationWeights, StateEvaluation, evaluate_state
from ..domain.entities import Customer, Route, Vehicle, VRPState, WorkdayInstance
from ..topology.matrix import CostMatrix


class ConstructiveHeuristicError(Exception):
    """Base class for every error raised while building an initial VRPState."""


class EmptyFleetError(ConstructiveHeuristicError):
    """Raised when a workday has customers to serve but no vehicle in its fleet."""


@dataclass(slots=True)
class _RouteUnderConstruction:
    """
    Mutable running simulation state of one vehicle's route while it is built.

    Unlike the frozen entities of `domain.entities`, this class is
    deliberately mutable: it exists only inside the hot loop of
    `build_initial_state`, is updated in place every time a customer is
    appended, and is discarded once translated into an immutable `Route`.
    Allowing mutation here is what makes every append an O(1) update instead
    of the O(route length) rebuild a frozen dataclass would require.

    Attributes
    ----------
    vehicle:
        Vehicle driving this route.
    customer_sequence:
        Customer identifiers appended so far, in visiting order.
    last_node:
        Graph node identifier the vehicle would currently be departing from:
        the depot until the first customer is appended, the most recently
        appended customer's node afterwards.
    total_demand:
        Sum of the demand of every customer appended so far.
    clock_seconds:
        Workday clock reading at `last_node`, excluding the eventual return
        trip to the depot, which is only added once the route is finalized.
    continuous_driving_seconds:
        Continuous driving time accumulated since the last mandatory rest
        break, or since the depot if none has been necessary yet.
    """

    vehicle: Vehicle
    customer_sequence: list[str] = field(default_factory=list)
    last_node: int = 0
    total_demand: float = 0.0
    clock_seconds: float = 0.0
    continuous_driving_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class _AppendOutcome:
    """
    Result of simulating one candidate customer appended to one route.

    Attributes
    ----------
    marginal_cost:
        Weighted cost this single append would add, computed with the exact
        same weights and formula as `evaluator.EvaluationWeights`. Positive
        infinity if the leg to the candidate is currently unreachable.
    new_last_node:
        Node the vehicle would be at after the append.
    new_clock_seconds:
        Workday clock reading after the append.
    new_continuous_driving_seconds:
        Continuous driving time after the append.
    """

    marginal_cost: float
    new_last_node: int
    new_clock_seconds: float
    new_continuous_driving_seconds: float


def _evaluate_append(
    route: _RouteUnderConstruction,
    customer: Customer,
    vehicle: Vehicle,
    cost_matrix: CostMatrix,
    weights: EvaluationWeights,
) -> _AppendOutcome:
    """
    Simulate appending one customer to the end of one route in O(1).

    The simulation mirrors `evaluator.evaluate_route` exactly for the single
    new leg: a mandatory EU rest break is inserted first if continuous
    driving would otherwise exceed `Vehicle.max_continuous_driving_seconds`,
    the vehicle then waits if it would arrive before the customer's time
    window opens or accrues a soft lateness penalty if it arrives after the
    window closes, and finally the customer's service time is added. Because
    only the marginal, additive contribution of this one customer is
    computed, no earlier part of the route needs to be revisited.

    The workday and capacity penalties are each charged only for the portion
    of the excess this specific append newly introduces (or removes), by
    comparing the excess before and after; total demand and the open route
    clock are always monotonically non-decreasing, so this delta is always
    non-negative and safe to add directly to the weighted marginal cost.

    Parameters
    ----------
    route:
        Route under construction to extend.
    customer:
        Candidate customer to append.
    vehicle:
        Vehicle driving the route, repeated here rather than read from
        `route.vehicle` purely to make the dependency explicit at the call
        site.
    cost_matrix:
        Precomputed cost matrix providing O(1) travel time and distance
        lookups.
    weights:
        Weights combining travel time, distance and the time window,
        capacity and workday penalties into a single scalar.

    Returns
    -------
    _AppendOutcome
        The marginal cost of the append and the resulting route state.
    """
    leg_travel_time_seconds = cost_matrix.travel_time_between(route.last_node, customer.node_id)
    leg_distance_meters = cost_matrix.distance_between(route.last_node, customer.node_id)

    if not math.isfinite(leg_travel_time_seconds) or not math.isfinite(leg_distance_meters):
        return _AppendOutcome(
            marginal_cost=math.inf,
            new_last_node=customer.node_id,
            new_clock_seconds=math.inf,
            new_continuous_driving_seconds=math.inf,
        )

    break_seconds = 0.0
    if route.continuous_driving_seconds + leg_travel_time_seconds > vehicle.max_continuous_driving_seconds:
        break_seconds = vehicle.mandatory_break_seconds
        new_continuous_driving_seconds = leg_travel_time_seconds
    else:
        new_continuous_driving_seconds = route.continuous_driving_seconds + leg_travel_time_seconds

    clock_after_driving = route.clock_seconds + break_seconds + leg_travel_time_seconds

    waiting_seconds = 0.0
    lateness_seconds = 0.0
    if clock_after_driving < customer.time_window.start_seconds:
        waiting_seconds = customer.time_window.start_seconds - clock_after_driving
    elif clock_after_driving > customer.time_window.end_seconds:
        lateness_seconds = clock_after_driving - customer.time_window.end_seconds

    new_clock_seconds = clock_after_driving + waiting_seconds + customer.service_time_seconds

    capacity_excess_before = max(0.0, route.total_demand - vehicle.max_capacity)
    capacity_excess_after = max(0.0, route.total_demand + customer.demand - vehicle.max_capacity)

    # The open route clock, excluding the eventual return trip to the depot,
    # is used as a conservative proxy for the workday budget during
    # construction; the final `evaluate_state` call re-checks the true,
    # closed-route duration once the depot return leg is known.
    workday_excess_before = max(0.0, route.clock_seconds - vehicle.max_workday_seconds)
    workday_excess_after = max(0.0, new_clock_seconds - vehicle.max_workday_seconds)

    marginal_cost = (
        weights.time_weight * leg_travel_time_seconds
        + weights.distance_weight * leg_distance_meters
        + weights.time_window_penalty_weight * lateness_seconds
        + weights.capacity_penalty_weight * (capacity_excess_after - capacity_excess_before)
        + weights.workday_penalty_weight * (workday_excess_after - workday_excess_before)
    )

    return _AppendOutcome(
        marginal_cost=marginal_cost,
        new_last_node=customer.node_id,
        new_clock_seconds=new_clock_seconds,
        new_continuous_driving_seconds=new_continuous_driving_seconds,
    )


def build_initial_state(
    workday: WorkdayInstance,
    cost_matrix: CostMatrix,
    weights: EvaluationWeights = EvaluationWeights(),
) -> VRPState:
    """
    Build a complete initial `VRPState` for a workday via cheapest append.

    Every workday customer is assigned to exactly one route, so the returned
    state always satisfies `VRPState.validate_customer_coverage` by
    construction. Vehicles are considered in `workday.fleet` order and
    unassigned customers in ascending customer identifier order whenever more
    than one candidate ties on cost, which makes the result fully
    deterministic for a given workday, cost matrix and weight configuration.

    Parameters
    ----------
    workday:
        Problem instance (fleet and customers) to build a candidate solution
        for.
    cost_matrix:
        Precomputed cost matrix whose nodes of interest match the workday's
        depot and customers.
    weights:
        Weights guiding every append decision. Defaults to the same
        configuration `evaluate_state` defaults to, so the heuristic and the
        eventual metaheuristic agree on what "cheap" means unless the caller
        deliberately overrides one of them.

    Returns
    -------
    VRPState
        A complete candidate solution covering every workday customer exactly
        once, preferring feasible routes whenever the fleet's capacity and
        workday duration limits allow it.

    Raises
    ------
    EmptyFleetError
        If the workday has customers to serve but no vehicle in its fleet,
        making complete coverage structurally impossible.
    """
    if not workday.fleet:
        if workday.customers:
            message = (
                f"The workday has {len(workday.customers)} customer(s) to serve but no "
                "vehicle in its fleet."
            )
            raise EmptyFleetError(message)
        return VRPState(routes=())

    depot_node = cost_matrix.depot_node
    routes_under_construction = {
        vehicle.vehicle_id: _RouteUnderConstruction(vehicle=vehicle, last_node=depot_node)
        for vehicle in workday.fleet
    }
    unassigned_customer_ids = sorted(workday.customer_ids)

    while unassigned_customer_ids:
        best_marginal_cost = math.inf
        best_vehicle_id: str | None = None
        best_customer_id: str | None = None
        best_outcome: _AppendOutcome | None = None

        for vehicle in workday.fleet:
            route = routes_under_construction[vehicle.vehicle_id]

            for customer_id in unassigned_customer_ids:
                customer = workday.customers_by_id[customer_id]
                outcome = _evaluate_append(route, customer, vehicle, cost_matrix, weights)

                if best_outcome is None or outcome.marginal_cost < best_marginal_cost:
                    best_marginal_cost = outcome.marginal_cost
                    best_vehicle_id = vehicle.vehicle_id
                    best_customer_id = customer_id
                    best_outcome = outcome

        if best_outcome is None or best_vehicle_id is None or best_customer_id is None:
            message = (
                "No append candidate was found despite a non-empty fleet and unassigned "
                "customers; this indicates an internal invariant violation."
            )
            raise ConstructiveHeuristicError(message)

        chosen_route = routes_under_construction[best_vehicle_id]
        chosen_route.customer_sequence.append(best_customer_id)
        chosen_route.last_node = best_outcome.new_last_node
        chosen_route.clock_seconds = best_outcome.new_clock_seconds
        chosen_route.continuous_driving_seconds = best_outcome.new_continuous_driving_seconds
        chosen_route.total_demand += workday.customers_by_id[best_customer_id].demand

        unassigned_customer_ids.remove(best_customer_id)

    routes = tuple(
        Route(vehicle_id=vehicle.vehicle_id, customer_sequence=tuple(routes_under_construction[vehicle.vehicle_id].customer_sequence))
        for vehicle in workday.fleet
    )

    return VRPState(routes=routes)


def build_initial_solution(
    workday: WorkdayInstance,
    cost_matrix: CostMatrix,
    weights: EvaluationWeights = EvaluationWeights(),
) -> tuple[VRPState, StateEvaluation]:
    """
    Build an initial `VRPState` and immediately score it with the evaluator.

    Convenience wrapper around `build_initial_state` followed by
    `evaluator.evaluate_state`, provided so that callers who only need a
    ready-to-report starting point, such as a demonstration script or the
    first iteration of the Tabu Search, do not need to import both functions
    and repeat the call. The two underlying functions remain independently
    usable, and this wrapper adds no behavior of its own.

    Parameters
    ----------
    workday:
        Problem instance (fleet and customers) to build a candidate solution
        for.
    cost_matrix:
        Precomputed cost matrix whose nodes of interest match the workday's
        depot and customers.
    weights:
        Weights guiding both the construction and the final evaluation.

    Returns
    -------
    tuple[VRPState, StateEvaluation]
        The constructed state, together with its authoritative evaluation.
    """
    initial_state = build_initial_state(workday, cost_matrix, weights)
    initial_state_evaluation = evaluate_state(initial_state, workday, cost_matrix, weights)

    return initial_state, initial_state_evaluation


if __name__ == "__main__":
    # This module is only meant to be run as `python -m backend.src.solver.constructive`
    # from the repository root, for the same import resolution reason
    # documented in `evaluator.py`'s own __main__ block.
    import time

    from ..domain.entities import TimeWindow
    from ..topology.extractor import load_processed_graph
    from ..topology.matrix import build_cost_matrix, select_demonstration_nodes

    DEMONSTRATION_CUSTOMER_COUNT: int = 60
    DEMONSTRATION_VEHICLE_COUNT: int = 5

    print("Loading the preprocessed street network and building its cost matrix.")
    malaga_street_network = load_processed_graph()
    demonstration_depot, demonstration_customer_nodes = select_demonstration_nodes(
        malaga_street_network, DEMONSTRATION_CUSTOMER_COUNT
    )
    demonstration_cost_matrix = build_cost_matrix(
        malaga_street_network, demonstration_depot, demonstration_customer_nodes
    )

    demonstration_workday = WorkdayInstance(
        customers=tuple(
            Customer(
                node_id=node_id,
                demand=float(15 + (index % 4) * 5),
                service_time_seconds=180.0 + 30.0 * (index % 6),
                # Windows are staggered widely across the workday, rather than
                # clustered into a handful of shared slots, so that a route
                # visiting stops in whatever order is geographically cheapest
                # is not forced into artificial lateness by the demonstration
                # data itself.
                time_window=TimeWindow(start_seconds=200.0 * index, end_seconds=200.0 * index + 5400.0),
            )
            for index, node_id in enumerate(demonstration_customer_nodes)
        ),
        fleet=tuple(
            Vehicle(vehicle_id=f"VAN-{vehicle_index + 1}", max_capacity=400.0)
            for vehicle_index in range(DEMONSTRATION_VEHICLE_COUNT)
        ),
    )

    construction_started_at = time.perf_counter()
    demonstration_state, demonstration_evaluation = build_initial_solution(
        demonstration_workday, demonstration_cost_matrix
    )
    construction_elapsed_ms = (time.perf_counter() - construction_started_at) * 1000.0

    print(
        f"\nConstructed an initial state for {DEMONSTRATION_CUSTOMER_COUNT} customers and "
        f"{DEMONSTRATION_VEHICLE_COUNT} vehicles in {construction_elapsed_ms:.2f} ms."
    )
    for route_evaluation in demonstration_evaluation.route_evaluations:
        route = next(
            route for route in demonstration_state.routes if route.vehicle_id == route_evaluation.vehicle_id
        )
        print(
            f"  {route_evaluation.vehicle_id}: {len(route.customer_sequence)} customer(s), "
            f"travel={route_evaluation.travel_time_seconds:.1f} s, "
            f"duration={route_evaluation.route_duration_seconds:.1f} s, "
            f"demand={route_evaluation.total_demand:.1f}, "
            f"capacity ok={route_evaluation.respects_capacity}, "
            f"workday ok={route_evaluation.respects_workday_duration}, "
            f"lateness={route_evaluation.time_window_lateness_seconds:.1f} s"
        )
    print(
        f"\nf(S) = {demonstration_evaluation.total_cost:.2f}, "
        f"feasible={demonstration_evaluation.is_feasible}"
    )
