"""
Multi-objective cost evaluation for VRP candidate solutions.

This module implements the evaluation function f(S) the metaheuristics of the
solver will minimize: given a candidate `VRPState` and the `CostMatrix`
precomputed for the workday, it returns a single deterministic scalar
combining travel time, travel distance and a set of penalties for violating
the constraints the model extends the standard Capacitated VRP with: vehicle
capacity, soft delivery time windows, per-stop service time, and European
Union driver rest regulations (Regulation (EC) No 561/2006).

Unlike a plain distance-summation evaluator, `evaluate_route` simulates the
vehicle's workday clock leg by leg: it accumulates continuous driving time and
inserts a mandatory rest break whenever the EU limit would otherwise be
exceeded, makes the vehicle wait at a customer visited ahead of its time
window, and advances the clock by each customer's service time before
departing towards the next stop. Every leg is still priced through
`CostMatrix.travel_time_between` and `CostMatrix.distance_between`, which
resolve to O(1) NumPy lookups into the precomputed N x N matrices, so this
module never performs a graph traversal and its cost grows only with the
number of stops on a route, not with the size of the underlying street
network. This is what allows a metaheuristic to call `evaluate_state` millions
of times during a search.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from ..domain.entities import Route, UnknownVehicleError, VRPState, WorkdayInstance
from ..topology.matrix import CostMatrix


@dataclass(frozen=True, slots=True)
class EvaluationWeights:
    """
    Configurable weights combining the objectives of the evaluation function.

    The evaluation function is:

        f(S) = time_weight * total_travel_time_seconds
             + distance_weight * total_distance_meters
             + time_window_penalty_weight * total_time_window_lateness_seconds
             + capacity_penalty_weight * total_capacity_excess
             + workday_penalty_weight * total_workday_excess_seconds

    The base objectives (time, distance) are ordinary economic costs. The
    time window penalty is deliberately moderate: a late delivery degrades
    service quality but remains a legal, physically feasible outcome, so it
    should discourage lateness without dominating every other consideration.
    The capacity and workday penalties, in contrast, default to a value
    several orders of magnitude larger than the others, so that, for any two
    states with a finite cost, one that respects both hard constraints is
    always cheaper than one that does not, regardless of how much shorter or
    faster the infeasible one might be. This is what makes a metaheuristic
    minimizing `total_cost` naturally reject overloaded vehicles and workdays
    that overrun EU driving regulations, rather than trade a small amount of
    violation for a large amount of saved time.

    Attributes
    ----------
    time_weight:
        Weight applied to the total travel time, in seconds, summed across
        every route. Travel time excludes waiting, service and rest breaks.
    distance_weight:
        Weight applied to the total travel distance, in meters, summed across
        every route.
    time_window_penalty_weight:
        Soft penalty weight applied to the total number of seconds every
        route arrives late at a customer past its `TimeWindow.end_seconds`.
    capacity_penalty_weight:
        Hard penalty weight applied to the total capacity excess, in the same
        units as `Customer.demand`, summed across every route.
    workday_penalty_weight:
        Hard penalty weight applied to the total number of seconds every
        route's full duration (driving, waiting, service and breaks included)
        exceeds its vehicle's `max_workday_seconds`.
    """

    time_weight: float = 1.0
    distance_weight: float = 0.1
    time_window_penalty_weight: float = 5.0
    capacity_penalty_weight: float = 100000.0
    workday_penalty_weight: float = 100000.0

    def __post_init__(self) -> None:
        for weight_name, weight_value in (
            ("time_weight", self.time_weight),
            ("distance_weight", self.distance_weight),
            ("time_window_penalty_weight", self.time_window_penalty_weight),
            ("capacity_penalty_weight", self.capacity_penalty_weight),
            ("workday_penalty_weight", self.workday_penalty_weight),
        ):
            if weight_value < 0.0:
                message = (
                    f"Evaluation weight '{weight_name}' must be non-negative, got {weight_value}."
                )
                raise ValueError(message)


@dataclass(frozen=True, slots=True)
class RouteStopVisit:
    """
    Timeline record of a single stop along a simulated route.

    One instance is produced per node of `Route.full_node_sequence`,
    including the depot at both ends, giving the evaluator's caller the exact
    schedule a vehicle would follow, which the future event-loop simulation
    and frontend can consume directly instead of recomputing it.

    Attributes
    ----------
    node_id:
        Graph node identifier of this stop.
    arrival_time_seconds:
        Workday clock reading when the vehicle reaches this node, immediately
        after driving the incoming leg (and after any rest break taken
        beforehand), but before any waiting for a time window to open.
    waiting_seconds:
        Idle time spent waiting for `TimeWindow.start_seconds` to be reached.
        Zero at the depot or whenever the vehicle does not arrive early.
    lateness_seconds:
        Soft time window violation: the number of seconds by which
        `arrival_time_seconds` exceeds `TimeWindow.end_seconds`. Zero at the
        depot or whenever the vehicle is not late.
    service_time_seconds:
        Time spent servicing this stop. Zero at the depot.
    break_seconds_before_departure:
        Duration of a mandatory EU rest break taken at this stop, after
        service and before departing towards the next node, because driving
        the next leg would otherwise exceed `max_continuous_driving_seconds`.
        Zero if no break was necessary here.
    departure_time_seconds:
        Workday clock reading when the vehicle leaves this node, equal to
        `arrival_time_seconds + waiting_seconds + service_time_seconds +
        break_seconds_before_departure`. Equal to `arrival_time_seconds` for
        the final depot return, since no further action follows it.
    """

    node_id: int
    arrival_time_seconds: float
    waiting_seconds: float
    lateness_seconds: float
    service_time_seconds: float
    break_seconds_before_departure: float
    departure_time_seconds: float


@dataclass(frozen=True, slots=True)
class RouteEvaluation:
    """
    Cost, capacity and timeline breakdown of a single route within a state.

    Attributes
    ----------
    vehicle_id:
        Identifier of the vehicle driving this route.
    travel_time_seconds:
        Sum of the driving time of every leg of the route. Excludes waiting,
        service and rest break time. Positive infinity if any leg of the
        route is currently unreachable, for example because of a street
        closure.
    distance_meters:
        Sum of the driving distance of every leg of the route. Positive
        infinity under the same condition as `travel_time_seconds`.
    total_demand:
        Sum of the demand of every customer on the route.
    capacity_excess:
        Amount by which `total_demand` exceeds the vehicle's `max_capacity`,
        or zero if the route respects it.
    is_reachable:
        Whether every leg of the route has a finite cost.
    waiting_time_seconds:
        Sum, across every customer on the route, of the idle time spent
        waiting for a time window to open. Contributes to
        `route_duration_seconds` but not to `travel_time_seconds`.
    service_time_seconds:
        Sum of every customer's `Customer.service_time_seconds` on this route.
    break_time_seconds:
        Total time spent on mandatory EU rest breaks along this route.
    mandatory_breaks_taken:
        Number of mandatory rest breaks the route required.
    time_window_lateness_seconds:
        Sum, across every customer on the route, of the soft lateness penalty
        basis (seconds arrived past `TimeWindow.end_seconds`).
    route_duration_seconds:
        Total elapsed workday clock for this route: driving, waiting, service
        and rest breaks combined, from departing the depot to returning to
        it. Positive infinity if the route is not fully reachable.
    workday_excess_seconds:
        Amount by which `route_duration_seconds` exceeds the vehicle's
        `max_workday_seconds`, or zero if the route respects it.
    stop_schedule:
        Per-stop timeline of the route, depot included at both ends, in
        visiting order.
    """

    vehicle_id: str
    travel_time_seconds: float
    distance_meters: float
    total_demand: float
    capacity_excess: float
    is_reachable: bool
    waiting_time_seconds: float
    service_time_seconds: float
    break_time_seconds: float
    mandatory_breaks_taken: int
    time_window_lateness_seconds: float
    route_duration_seconds: float
    workday_excess_seconds: float
    stop_schedule: tuple[RouteStopVisit, ...]

    @property
    def respects_capacity(self) -> bool:
        """Return whether this route stays within its vehicle's capacity."""
        return self.capacity_excess <= 0.0

    @property
    def respects_workday_duration(self) -> bool:
        """Return whether this route stays within its vehicle's workday budget."""
        return self.workday_excess_seconds <= 0.0


@dataclass(frozen=True, slots=True)
class StateEvaluation:
    """
    Deterministic, quantitative evaluation of one candidate `VRPState`.

    Attributes
    ----------
    total_cost:
        The scalar objective value f(S) a metaheuristic should minimize.
        Positive infinity whenever any route is not fully reachable,
        regardless of the configured weights; otherwise the weighted sum
        described in `EvaluationWeights`.
    total_travel_time_seconds:
        Sum of `RouteEvaluation.travel_time_seconds` across every route.
    total_distance_meters:
        Sum of `RouteEvaluation.distance_meters` across every route.
    total_capacity_excess:
        Sum of `RouteEvaluation.capacity_excess` across every route.
    total_time_window_lateness_seconds:
        Sum of `RouteEvaluation.time_window_lateness_seconds` across every
        route.
    total_workday_excess_seconds:
        Sum of `RouteEvaluation.workday_excess_seconds` across every route.
    is_feasible:
        True only if every route is reachable and respects both its
        vehicle's capacity and its vehicle's maximum workday duration. Soft
        time window lateness never makes a state infeasible, only more
        expensive. A metaheuristic may still choose to accept an infeasible
        state transiently, guided by the finite penalty embedded in
        `total_cost`, but should never report one as a final solution.
    route_evaluations:
        Per-route breakdown, in the same order as `VRPState.routes`.
    """

    total_cost: float
    total_travel_time_seconds: float
    total_distance_meters: float
    total_capacity_excess: float
    total_time_window_lateness_seconds: float
    total_workday_excess_seconds: float
    is_feasible: bool
    route_evaluations: tuple[RouteEvaluation, ...]


def _depot_stop_visit(node_id: int, clock_seconds: float) -> RouteStopVisit:
    """Build the zero-activity `RouteStopVisit` record for a depot visit."""
    return RouteStopVisit(
        node_id=node_id,
        arrival_time_seconds=clock_seconds,
        waiting_seconds=0.0,
        lateness_seconds=0.0,
        service_time_seconds=0.0,
        break_seconds_before_departure=0.0,
        departure_time_seconds=clock_seconds,
    )


def evaluate_route(route: Route, workday: WorkdayInstance, cost_matrix: CostMatrix) -> RouteEvaluation:
    """
    Simulate a single route's workday clock using only O(1) matrix lookups.

    The simulation walks `route.full_node_sequence` leg by leg. For every
    leg it: inserts a mandatory EU rest break if driving the leg would push
    continuous driving time past `Vehicle.max_continuous_driving_seconds`;
    advances the clock by the leg's travel time; at a customer, makes the
    vehicle wait if it arrived before `TimeWindow.start_seconds`, or accrues
    a soft lateness penalty basis if it arrived after `TimeWindow.end_seconds`;
    and finally advances the clock by the customer's `service_time_seconds`
    before moving on to the next leg.

    Parameters
    ----------
    route:
        Route to evaluate.
    workday:
        Problem instance the route's vehicle and customers belong to.
    cost_matrix:
        Precomputed cost matrix whose nodes of interest cover the depot and
        every customer this route may visit.

    Returns
    -------
    RouteEvaluation
        Cost, capacity and timeline breakdown of the route.

    Raises
    ------
    UnknownVehicleError
        If the route references a vehicle absent from the workday fleet.
    UnknownCustomerError
        If the route visits a node that is not a workday customer.
    """
    vehicle = workday.fleet_by_vehicle_id.get(route.vehicle_id)
    if vehicle is None:
        message = f"Route references vehicle '{route.vehicle_id}', which is not part of the fleet."
        raise UnknownVehicleError(message)

    total_demand = route.total_demand(workday.customers_by_node_id)
    capacity_excess = max(0.0, total_demand - vehicle.max_capacity)

    depot_node = cost_matrix.depot_node
    full_node_sequence = route.full_node_sequence(depot_node)
    final_leg_index = len(full_node_sequence) - 2

    clock_seconds = 0.0
    continuous_driving_seconds = 0.0
    total_travel_time_seconds = 0.0
    total_distance_meters = 0.0
    total_waiting_seconds = 0.0
    total_service_time_seconds = 0.0
    total_break_seconds = 0.0
    total_lateness_seconds = 0.0
    mandatory_breaks_taken = 0
    is_reachable = True

    stop_schedule: list[RouteStopVisit] = [_depot_stop_visit(depot_node, clock_seconds)]

    for leg_index in range(final_leg_index + 1):
        origin_node = full_node_sequence[leg_index]
        destination_node = full_node_sequence[leg_index + 1]

        leg_travel_time_seconds = cost_matrix.travel_time_between(origin_node, destination_node)
        leg_distance_meters = cost_matrix.distance_between(origin_node, destination_node)

        if not math.isfinite(leg_travel_time_seconds) or not math.isfinite(leg_distance_meters):
            # The remainder of the route cannot be physically driven, so the
            # simulation stops here rather than producing a misleading
            # partial timeline for stops that would never be reached.
            is_reachable = False
            break

        if continuous_driving_seconds + leg_travel_time_seconds > vehicle.max_continuous_driving_seconds:
            break_seconds = vehicle.mandatory_break_seconds
            clock_seconds += break_seconds
            continuous_driving_seconds = 0.0
            total_break_seconds += break_seconds
            mandatory_breaks_taken += 1
            # The break is taken at the stop the vehicle is departing from,
            # so the previously recorded visit is amended retroactively; the
            # record is frozen, hence the replace-and-reassign pattern.
            stop_schedule[-1] = replace(
                stop_schedule[-1],
                break_seconds_before_departure=break_seconds,
                departure_time_seconds=stop_schedule[-1].departure_time_seconds + break_seconds,
            )

        clock_seconds += leg_travel_time_seconds
        continuous_driving_seconds += leg_travel_time_seconds
        total_travel_time_seconds += leg_travel_time_seconds
        total_distance_meters += leg_distance_meters

        arrival_time_seconds = clock_seconds
        is_final_depot_return = leg_index == final_leg_index

        waiting_seconds = 0.0
        lateness_seconds = 0.0
        service_time_seconds = 0.0

        if not is_final_depot_return:
            customer = workday.customers_by_node_id[destination_node]

            if arrival_time_seconds < customer.time_window.start_seconds:
                waiting_seconds = customer.time_window.start_seconds - arrival_time_seconds
                clock_seconds += waiting_seconds
                total_waiting_seconds += waiting_seconds
            elif arrival_time_seconds > customer.time_window.end_seconds:
                lateness_seconds = arrival_time_seconds - customer.time_window.end_seconds
                total_lateness_seconds += lateness_seconds

            service_time_seconds = customer.service_time_seconds
            clock_seconds += service_time_seconds
            total_service_time_seconds += service_time_seconds

        stop_schedule.append(
            RouteStopVisit(
                node_id=destination_node,
                arrival_time_seconds=arrival_time_seconds,
                waiting_seconds=waiting_seconds,
                lateness_seconds=lateness_seconds,
                service_time_seconds=service_time_seconds,
                break_seconds_before_departure=0.0,
                departure_time_seconds=clock_seconds,
            )
        )

    if is_reachable:
        route_duration_seconds = clock_seconds
        workday_excess_seconds = max(0.0, route_duration_seconds - vehicle.max_workday_seconds)
    else:
        # An unreachable route has no well-defined duration; treating it as
        # infinite keeps it consistent with the infinite travel time and
        # distance below, instead of reporting a partial, meaningless value.
        total_travel_time_seconds = math.inf
        total_distance_meters = math.inf
        route_duration_seconds = math.inf
        workday_excess_seconds = math.inf

    return RouteEvaluation(
        vehicle_id=route.vehicle_id,
        travel_time_seconds=total_travel_time_seconds,
        distance_meters=total_distance_meters,
        total_demand=total_demand,
        capacity_excess=capacity_excess,
        is_reachable=is_reachable,
        waiting_time_seconds=total_waiting_seconds,
        service_time_seconds=total_service_time_seconds,
        break_time_seconds=total_break_seconds,
        mandatory_breaks_taken=mandatory_breaks_taken,
        time_window_lateness_seconds=total_lateness_seconds,
        route_duration_seconds=route_duration_seconds,
        workday_excess_seconds=workday_excess_seconds,
        stop_schedule=tuple(stop_schedule),
    )


def evaluate_state(
    state: VRPState,
    workday: WorkdayInstance,
    cost_matrix: CostMatrix,
    weights: EvaluationWeights = EvaluationWeights(),
) -> StateEvaluation:
    """
    Evaluate a complete candidate solution against a precomputed cost matrix.

    Parameters
    ----------
    state:
        Candidate solution to evaluate. Must cover every workday customer
        exactly once; see `VRPState.validate_customer_coverage`.
    workday:
        Problem instance (fleet and customers) the state is a solution for.
    cost_matrix:
        Precomputed cost matrix whose nodes of interest match the workday's
        depot and customers.
    weights:
        Relative weighting of travel time, distance, and the time window,
        capacity and workday penalties. Defaults to a configuration where the
        capacity and workday penalties dominate by several orders of
        magnitude, so that a metaheuristic minimizing `total_cost` naturally
        rejects overloaded vehicles and EU-noncompliant workdays before it
        ever starts trading off seconds against meters.

    Returns
    -------
    StateEvaluation
        The deterministic score f(S), together with its full breakdown.

    Raises
    ------
    UnknownVehicleError
        If a route references a vehicle absent from the workday fleet.
    UnknownCustomerError
        If a route visits a node that is not a workday customer.
    DuplicateCustomerAssignmentError
        If a customer is assigned to more than one route.
    IncompleteCoverageError
        If a workday customer is not served by any route.
    """
    state.validate_customer_coverage(workday)

    route_evaluations = tuple(evaluate_route(route, workday, cost_matrix) for route in state.routes)

    total_travel_time_seconds = sum(
        route_evaluation.travel_time_seconds for route_evaluation in route_evaluations
    )
    total_distance_meters = sum(
        route_evaluation.distance_meters for route_evaluation in route_evaluations
    )
    total_capacity_excess = sum(
        route_evaluation.capacity_excess for route_evaluation in route_evaluations
    )
    total_time_window_lateness_seconds = sum(
        route_evaluation.time_window_lateness_seconds for route_evaluation in route_evaluations
    )
    total_workday_excess_seconds = sum(
        route_evaluation.workday_excess_seconds for route_evaluation in route_evaluations
    )
    is_fully_reachable = all(route_evaluation.is_reachable for route_evaluation in route_evaluations)
    is_feasible = (
        is_fully_reachable
        and total_capacity_excess <= 0.0
        and total_workday_excess_seconds <= 0.0
    )

    if is_fully_reachable:
        total_cost = (
            weights.time_weight * total_travel_time_seconds
            + weights.distance_weight * total_distance_meters
            + weights.time_window_penalty_weight * total_time_window_lateness_seconds
            + weights.capacity_penalty_weight * total_capacity_excess
            + weights.workday_penalty_weight * total_workday_excess_seconds
        )
    else:
        # An unreachable route makes the state unusable regardless of the
        # configured weights. Infinity is returned explicitly here instead of
        # being derived through weighted arithmetic, because
        # weight * math.inf silently evaluates to NaN whenever that
        # particular weight is exactly zero.
        total_cost = math.inf

    return StateEvaluation(
        total_cost=total_cost,
        total_travel_time_seconds=total_travel_time_seconds,
        total_distance_meters=total_distance_meters,
        total_capacity_excess=total_capacity_excess,
        total_time_window_lateness_seconds=total_time_window_lateness_seconds,
        total_workday_excess_seconds=total_workday_excess_seconds,
        is_feasible=is_feasible,
        route_evaluations=route_evaluations,
    )


if __name__ == "__main__":
    # This module is only meant to be run as `python -m backend.src.solver.evaluator`
    # from the repository root, since it relies on the relative imports above
    # to reach its sibling packages. The extractor and matrix demonstration
    # helpers are imported here, rather than at module level, for the same
    # reason `matrix.py` defers its own dependency on the extractor: keeping
    # this module free of OSMnx and Folium when it is loaded by the backend.
    from ..domain.entities import Customer, TimeWindow, Vehicle
    from ..topology.extractor import load_processed_graph
    from ..topology.matrix import build_cost_matrix, select_demonstration_nodes

    DEMONSTRATION_CUSTOMER_COUNT: int = 12

    print("Loading the preprocessed street network and building its cost matrix.")
    malaga_street_network = load_processed_graph()
    demonstration_depot, demonstration_customer_nodes = select_demonstration_nodes(
        malaga_street_network, DEMONSTRATION_CUSTOMER_COUNT
    )
    demonstration_cost_matrix = build_cost_matrix(
        malaga_street_network, demonstration_depot, demonstration_customer_nodes
    )

    # A mix of tight and generous time windows, and one deliberately narrow
    # window near the start of the day, so the demonstration output exercises
    # both waiting and soft lateness at least once.
    demonstration_workday = WorkdayInstance(
        customers=tuple(
            Customer(
                node_id=node_id,
                demand=float(20 + index * 2),
                service_time_seconds=300.0 + 60.0 * index,
                time_window=TimeWindow(start_seconds=600.0 * index, end_seconds=600.0 * index + 1800.0),
            )
            for index, node_id in enumerate(demonstration_customer_nodes)
        ),
        fleet=(
            Vehicle(vehicle_id="VAN-1", max_capacity=400.0),
            Vehicle(vehicle_id="VAN-2", max_capacity=400.0),
        ),
    )

    # Split the customers evenly between the two vans as a simple, non-optimized
    # starting solution: the evaluator does not search for good routes, only
    # scores whichever candidate it is given.
    midpoint = len(demonstration_customer_nodes) // 2
    demonstration_state = VRPState(
        routes=(
            Route(vehicle_id="VAN-1", customer_sequence=tuple(demonstration_customer_nodes[:midpoint])),
            Route(vehicle_id="VAN-2", customer_sequence=tuple(demonstration_customer_nodes[midpoint:])),
        )
    )

    demonstration_evaluation = evaluate_state(demonstration_state, demonstration_workday, demonstration_cost_matrix)

    print(f"\nFleet: {[vehicle.vehicle_id for vehicle in demonstration_workday.fleet]}")
    for route_evaluation in demonstration_evaluation.route_evaluations:
        print(
            f"  {route_evaluation.vehicle_id}: travel={route_evaluation.travel_time_seconds:.1f} s, "
            f"distance={route_evaluation.distance_meters:.1f} m, "
            f"waiting={route_evaluation.waiting_time_seconds:.1f} s, "
            f"service={route_evaluation.service_time_seconds:.1f} s, "
            f"breaks={route_evaluation.mandatory_breaks_taken} "
            f"({route_evaluation.break_time_seconds:.1f} s), "
            f"lateness={route_evaluation.time_window_lateness_seconds:.1f} s, "
            f"duration={route_evaluation.route_duration_seconds:.1f} s, "
            f"demand={route_evaluation.total_demand:.1f}, "
            f"capacity ok={route_evaluation.respects_capacity}, "
            f"workday ok={route_evaluation.respects_workday_duration}"
        )
    print(
        f"\nf(S) = {demonstration_evaluation.total_cost:.2f} "
        f"(time={demonstration_evaluation.total_travel_time_seconds:.1f} s, "
        f"distance={demonstration_evaluation.total_distance_meters:.1f} m, "
        f"lateness={demonstration_evaluation.total_time_window_lateness_seconds:.1f} s, "
        f"capacity excess={demonstration_evaluation.total_capacity_excess:.1f}, "
        f"workday excess={demonstration_evaluation.total_workday_excess_seconds:.1f} s), "
        f"feasible={demonstration_evaluation.is_feasible}"
    )
