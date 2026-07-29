"""
Multi-objective cost evaluation for VRP candidate solutions.

This module implements the evaluation function f(S) the metaheuristics of the
solver will minimize: given a candidate `VRPState` and the `CostMatrix`
precomputed for the workday, it returns a single deterministic scalar
combining travel time, travel distance and a strict penalty for any vehicle
whose load exceeds its capacity.

Every route is priced exclusively through `CostMatrix.evaluate_route`, which
resolves to O(1) NumPy lookups into the precomputed N x N matrices. This
module therefore never performs a graph traversal, and its cost does not grow
with the size of the underlying street network, only with the number of
routes and customers of the workday, which is exactly what allows a
metaheuristic to call `evaluate_state` millions of times during a search.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..domain.entities import Route, UnknownVehicleError, VRPState, WorkdayInstance
from ..topology.matrix import CostMatrix


@dataclass(frozen=True, slots=True)
class EvaluationWeights:
    """
    Configurable weights combining the objectives of the evaluation function.

    The evaluation function is f(S) = alpha * time + beta * distance +
    gamma * capacity_excess. `capacity_penalty_weight` (gamma) defaults to a
    value several orders of magnitude larger than the other two, so that, for
    any two states with a finite cost, one that is capacity-feasible is always
    cheaper than one that is not, regardless of how much shorter or faster the
    infeasible one might be. This is what makes a metaheuristic minimizing
    `total_cost` naturally reject overloaded solutions rather than trade a
    small amount of overload for a large amount of saved time.

    Attributes
    ----------
    time_weight:
        Weight (alpha) applied to the total travel time, in seconds, summed
        across every route.
    distance_weight:
        Weight (beta) applied to the total travel distance, in meters, summed
        across every route.
    capacity_penalty_weight:
        Weight (gamma) applied to the total capacity excess, in the same
        units as `Customer.demand`, summed across every route.
    """

    time_weight: float = 1.0
    distance_weight: float = 0.1
    capacity_penalty_weight: float = 100000.0

    def __post_init__(self) -> None:
        for weight_name, weight_value in (
            ("time_weight", self.time_weight),
            ("distance_weight", self.distance_weight),
            ("capacity_penalty_weight", self.capacity_penalty_weight),
        ):
            if weight_value < 0.0:
                message = (
                    f"Evaluation weight '{weight_name}' must be non-negative, got {weight_value}."
                )
                raise ValueError(message)


@dataclass(frozen=True, slots=True)
class RouteEvaluation:
    """
    Cost and capacity breakdown of a single route within an evaluated state.

    Attributes
    ----------
    vehicle_id:
        Identifier of the vehicle driving this route.
    travel_time_seconds:
        Total travel time of the route, as priced by the cost matrix. Positive
        infinity if any leg of the route is currently unreachable, for example
        because of a street closure.
    distance_meters:
        Total distance of the route, as priced by the cost matrix. Positive
        infinity under the same condition as `travel_time_seconds`.
    total_demand:
        Sum of the demand of every customer on the route.
    capacity_excess:
        Amount by which `total_demand` exceeds the vehicle's `max_capacity`,
        or zero if the route respects it.
    is_reachable:
        Whether every leg of the route has a finite cost.
    """

    vehicle_id: str
    travel_time_seconds: float
    distance_meters: float
    total_demand: float
    capacity_excess: float
    is_reachable: bool

    @property
    def respects_capacity(self) -> bool:
        """Return whether this route stays within its vehicle's capacity."""
        return self.capacity_excess <= 0.0


@dataclass(frozen=True, slots=True)
class StateEvaluation:
    """
    Deterministic, quantitative evaluation of one candidate `VRPState`.

    Attributes
    ----------
    total_cost:
        The scalar objective value f(S) a metaheuristic should minimize.
        Positive infinity whenever any route is not fully reachable,
        regardless of the configured weights; otherwise
        `time_weight * total_travel_time_seconds
        + distance_weight * total_distance_meters
        + capacity_penalty_weight * total_capacity_excess`.
    total_travel_time_seconds:
        Sum of `RouteEvaluation.travel_time_seconds` across every route.
    total_distance_meters:
        Sum of `RouteEvaluation.distance_meters` across every route.
    total_capacity_excess:
        Sum of `RouteEvaluation.capacity_excess` across every route.
    is_feasible:
        True only if every route is reachable and respects its vehicle's
        capacity. A metaheuristic may still choose to accept an infeasible
        state transiently, guided by the finite penalty embedded in
        `total_cost`, but should never report one as a final solution.
    route_evaluations:
        Per-route breakdown, in the same order as `VRPState.routes`.
    """

    total_cost: float
    total_travel_time_seconds: float
    total_distance_meters: float
    total_capacity_excess: float
    is_feasible: bool
    route_evaluations: tuple[RouteEvaluation, ...]


def evaluate_route(route: Route, workday: WorkdayInstance, cost_matrix: CostMatrix) -> RouteEvaluation:
    """
    Price a single route using only O(1) lookups on the cost matrix.

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
        Cost and capacity breakdown of the route.

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

    full_node_sequence = route.full_node_sequence(cost_matrix.depot_node)
    travel_time_seconds, distance_meters = cost_matrix.evaluate_route(full_node_sequence)
    is_reachable = math.isfinite(travel_time_seconds) and math.isfinite(distance_meters)

    return RouteEvaluation(
        vehicle_id=route.vehicle_id,
        travel_time_seconds=travel_time_seconds,
        distance_meters=distance_meters,
        total_demand=total_demand,
        capacity_excess=capacity_excess,
        is_reachable=is_reachable,
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
        Relative weighting of travel time, distance and the capacity penalty.
        Defaults to a configuration where the capacity penalty dominates by
        several orders of magnitude, so that a metaheuristic minimizing
        `total_cost` naturally rejects overloaded solutions before it ever
        starts trading off seconds against meters.

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
    is_fully_reachable = all(route_evaluation.is_reachable for route_evaluation in route_evaluations)
    is_feasible = is_fully_reachable and total_capacity_excess <= 0.0

    if is_fully_reachable:
        total_cost = (
            weights.time_weight * total_travel_time_seconds
            + weights.distance_weight * total_distance_meters
            + weights.capacity_penalty_weight * total_capacity_excess
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
    from ..domain.entities import Customer, Vehicle
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

    demonstration_workday = WorkdayInstance(
        customers=tuple(
            Customer(node_id=node_id, demand=float(20 + index * 2))
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
            f"  {route_evaluation.vehicle_id}: time={route_evaluation.travel_time_seconds:.1f} s, "
            f"distance={route_evaluation.distance_meters:.1f} m, "
            f"demand={route_evaluation.total_demand:.1f}, "
            f"capacity ok={route_evaluation.respects_capacity}"
        )
    print(
        f"\nf(S) = {demonstration_evaluation.total_cost:.2f} "
        f"(time={demonstration_evaluation.total_travel_time_seconds:.1f} s, "
        f"distance={demonstration_evaluation.total_distance_meters:.1f} m, "
        f"capacity excess={demonstration_evaluation.total_capacity_excess:.1f}), "
        f"feasible={demonstration_evaluation.is_feasible}"
    )
