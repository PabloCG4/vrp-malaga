"""
Bridges the ORM-persisted representation of a workday plan
(`backend/src/db/models.py`) with the immutable, in-memory domain model the
solver operates on (`backend/src/domain/entities.py`), and drives the
existing constructive heuristic + Tabu Search pipeline.

Every function in this module is a pure, synchronous, CPU-bound
transformation or computation that performs no database or network I/O. That
is precisely what makes `run_static_optimization` safe to hand to
`asyncio.to_thread` from the async service layer: it never needs to touch an
`AsyncSession` from a worker thread, and it never modifies the solver
mathematics, cost matrix or heuristics of `backend/src/solver/` themselves,
only translates data into and out of them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import networkx as nx

from ...db.models import Order, Vehicle
from ...domain.entities import Customer, TimeWindow, Vehicle as DomainVehicle, VRPState, WorkdayInstance
from ...solver.constructive import build_initial_state
from ...solver.evaluator import StateEvaluation
from ...solver.metaheuristic import TabuSearchConfig, run_tabu_search
from ...topology.matrix import CostMatrix, build_cost_matrix

# Stopping criteria for an on-demand, dispatcher-triggered optimization.
# Bounded to a few seconds of wall-clock time so a synchronous HTTP request
# from the Control Tower dashboard returns promptly even for a fully loaded
# workday, while still giving Tabu Search enough iterations to meaningfully
# improve on the constructive heuristic's initial state.
ON_DEMAND_SEARCH_CONFIG = TabuSearchConfig(
    max_iterations=500, max_iterations_without_improvement=150, time_limit_seconds=8.0, tabu_tenure=15
)


@dataclass(frozen=True, slots=True)
class OptimizationOutcome:
    """
    Result of running the static optimization pipeline for one workday.

    Attributes
    ----------
    best_state:
        Best `VRPState` found by Tabu Search.
    best_evaluation:
        Full cost, capacity and per-stop timeline breakdown of `best_state`.
    iterations_completed:
        Number of Tabu Search iterations actually run.
    elapsed_seconds:
        Wall-clock time spent building the initial state and searching.
    orders_by_customer_id:
        Reverse lookup from `Customer.customer_id` back to the `Order` row it
        was built from, so the caller can persist `RouteStop.order_id`
        without re-deriving the mapping.
    """

    best_state: VRPState
    best_evaluation: StateEvaluation
    iterations_completed: int
    elapsed_seconds: float
    orders_by_customer_id: dict[str, Order]


def build_workday_instance(
    orders: list[Order], vehicles: list[Vehicle]
) -> tuple[WorkdayInstance, dict[str, Order]]:
    """
    Translate persisted `Order`/`Vehicle` rows into an immutable `WorkdayInstance`.

    Every `Customer.customer_id` is set to `str(order.id)` rather than left to
    default from `node_id`, since several orders (for example, the depot
    pickup legs of multiple VRPPD pairs) legitimately share the same node.
    Pickup/delivery pairing is carried over verbatim from `Order.is_pickup_stop`
    and `Order.paired_order_id`; `WorkdayInstance.__post_init__` re-validates
    that the pairing is mutual and consistent.

    Parameters
    ----------
    orders:
        Every order belonging to the workday plan being optimized.
    vehicles:
        Active fleet available to serve them.

    Returns
    -------
    tuple[WorkdayInstance, dict[str, Order]]
        The domain problem instance, and a reverse lookup from customer
        identifier back to the `Order` row it was built from.
    """
    customers: list[Customer] = []
    orders_by_customer_id: dict[str, Order] = {}

    for order in orders:
        customer_id = str(order.id)
        customers.append(
            Customer(
                node_id=order.node_id,
                demand=order.demand_kg,
                service_time_seconds=float(order.service_time_seconds),
                time_window=TimeWindow(
                    start_seconds=float(order.time_window_start_seconds),
                    end_seconds=float(order.time_window_end_seconds),
                ),
                customer_id=customer_id,
                is_pickup_stop=order.is_pickup_stop,
                paired_customer_id=str(order.paired_order_id) if order.paired_order_id is not None else None,
            )
        )
        orders_by_customer_id[customer_id] = order

    fleet = tuple(
        DomainVehicle(vehicle_id=str(vehicle.id), max_capacity=vehicle.capacity_kg) for vehicle in vehicles
    )

    workday = WorkdayInstance(customers=tuple(customers), fleet=fleet)
    return workday, orders_by_customer_id


def build_cost_matrix_for_orders(
    street_network_graph: nx.MultiDiGraph, depot_node: int, orders: list[Order]
) -> CostMatrix:
    """
    Build a fresh `CostMatrix` covering the depot and every distinct order node.

    Several orders may share a physical node (most notably every depot pickup
    leg of a VRPPD pair, whose `node_id` equals `depot_node` itself), so the
    node set is de-duplicated, and the depot itself is excluded from the
    customer node list, before being handed to `build_cost_matrix`, which
    requires its "nodes of interest" to be free of duplicates.

    Parameters
    ----------
    street_network_graph:
        Preprocessed Malaga street network.
    depot_node:
        Graph node identifier every route departs from and returns to.
    orders:
        Every order belonging to the workday plan being optimized.

    Returns
    -------
    CostMatrix
        Precomputed travel time and distance matrix for this workday's stops.
    """
    distinct_customer_nodes = sorted({order.node_id for order in orders if order.node_id != depot_node})
    return build_cost_matrix(street_network_graph, depot_node, distinct_customer_nodes)


def run_static_optimization(
    street_network_graph: nx.MultiDiGraph,
    depot_node: int,
    orders: list[Order],
    vehicles: list[Vehicle],
) -> OptimizationOutcome:
    """
    Run the full constructive heuristic + Tabu Search pipeline, synchronously.

    Intended to be the single blocking call passed to `asyncio.to_thread`
    from the async service layer. Performs no database access, and raises
    only the existing, well-documented domain/solver exceptions
    (`domain.entities.DomainError` subclasses, `solver.constructive.EmptyFleetError`)
    if the translated data is malformed.

    Parameters
    ----------
    street_network_graph:
        Preprocessed Malaga street network, from `network_provider.get_street_network_graph`.
    depot_node:
        Graph node identifier every route departs from and returns to, from
        `network_provider.get_depot_node`.
    orders:
        Every order belonging to the workday plan being optimized.
    vehicles:
        Active fleet available to serve them.

    Returns
    -------
    OptimizationOutcome
        The best state found, its full evaluation, and search diagnostics.
    """
    workday, orders_by_customer_id = build_workday_instance(orders, vehicles)
    cost_matrix = build_cost_matrix_for_orders(street_network_graph, depot_node, orders)

    started_at = time.perf_counter()
    initial_state = build_initial_state(workday, cost_matrix)
    search_result = run_tabu_search(initial_state, workday, cost_matrix, ON_DEMAND_SEARCH_CONFIG)
    elapsed_seconds = time.perf_counter() - started_at

    return OptimizationOutcome(
        best_state=search_result.best_state,
        best_evaluation=search_result.best_evaluation,
        iterations_completed=search_result.iterations_completed,
        elapsed_seconds=elapsed_seconds,
        orders_by_customer_id=orders_by_customer_id,
    )
