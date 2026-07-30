"""
Regression and benchmark tests for the Tabu Search metaheuristic engine.

Two kinds of scenarios are used, deliberately:

1. Small, hand-crafted, fully deterministic scenarios (a two-node synthetic
   `CostMatrix`, a handful of customers and vehicles) for the tests that must
   prove an exact algorithmic property of the search itself, such as the
   tabu list's expiry arithmetic or a forced anti-cycling scenario. Real
   street network data has no reason to reproducibly exhibit these
   properties, and a synthetic matrix is what lets the expected outcome be
   verified by hand instead of merely observed.
2. The real, preprocessed Malaga street network (`conftest.malaga_street_network`)
   for the tests that validate the search against realistic route geometry:
   the delta-costing-equals-full-recomputation invariant, the locked-prefix
   safety guarantee, and the iterations-per-second benchmark, mirroring the
   scale (30-100 customers) used to benchmark the Phase 2.3 constructive
   heuristic.
"""

from __future__ import annotations

import math

import networkx as nx
import numpy as np
import pytest

from backend.src.domain.entities import (
    Customer,
    Route,
    TimeWindow,
    Vehicle,
    VRPState,
    WorkdayInstance,
)
from backend.src.solver.constructive import build_initial_state
from backend.src.solver.evaluator import (
    EvaluationWeights,
    evaluate_route_cost,
    evaluate_state,
    simulate_route_clock,
)
from backend.src.solver.metaheuristic import (
    RelocateMove,
    SwapMove,
    TabuList,
    TabuSearchConfig,
    TwoOptMove,
    _generate_all_moves,
    move_is_admissible,
    run_tabu_search,
)
from backend.src.topology.matrix import CostMatrix, build_cost_matrix, select_demonstration_nodes


def _build_synthetic_cost_matrix(
    node_ids: tuple[int, ...], travel_time_seconds: float = 100.0, distance_meters: float = 1000.0
) -> CostMatrix:
    """
    Build a small, fully controlled `CostMatrix` for deterministic unit tests.

    Every pair of distinct nodes shares the same finite travel time and
    distance. This is sufficient for the tests that use it: they exercise
    properties of the search algorithm itself (tabu bookkeeping, forced
    anti-cycling), a concern independent of real street geometry, which is
    validated separately in `test_matrix.py` and by the real-graph tests
    below.
    """
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
    """Build a workday instance covering `customer_nodes` with a fleet of identical vans."""
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
# Tabu list and aspiration criterion (deterministic, no VRP data required)
# ---------------------------------------------------------------------------


def test_tabu_list_expiry_boundaries() -> None:
    """A forbidden key is tabu at its forbidding iteration and for tenure-1 iterations after, no more."""
    tabu_list = TabuList(tabu_tenure=3)
    key = ("relocate", "7", "VAN-1", "VAN-2")

    assert not tabu_list.is_tabu(key, current_iteration=0)

    tabu_list.forbid(key, current_iteration=10)

    assert tabu_list.is_tabu(key, current_iteration=10)
    assert tabu_list.is_tabu(key, current_iteration=12)
    assert not tabu_list.is_tabu(key, current_iteration=13)


def test_move_is_admissible_aspiration_criterion() -> None:
    """Exhaustively verifies the four tabu/cost combinations the aspiration rule must resolve."""
    # Not tabu: always admissible, even if it would worsen the current best.
    assert move_is_admissible(is_tabu=False, candidate_total_cost=100.0, best_total_cost_ever=50.0)
    # Tabu and merely tying the best-ever cost: not a new best, stays forbidden.
    assert not move_is_admissible(is_tabu=True, candidate_total_cost=50.0, best_total_cost_ever=50.0)
    # Tabu and worse: stays forbidden.
    assert not move_is_admissible(is_tabu=True, candidate_total_cost=60.0, best_total_cost_ever=50.0)
    # Tabu but strictly better than the best-ever cost: aspiration overrides it.
    assert move_is_admissible(is_tabu=True, candidate_total_cost=40.0, best_total_cost_ever=50.0)


def test_relocate_reverse_tabu_key_matches_commit_key() -> None:
    """The candidate key of the exact reverse relocation must equal the forward move's commit key."""
    move = RelocateMove(
        customer_id="9", source_vehicle_id="VAN-1", source_position=2, target_vehicle_id="VAN-2", target_position=0
    )
    reverse_move = RelocateMove(
        customer_id="9", source_vehicle_id="VAN-2", source_position=0, target_vehicle_id="VAN-1", target_position=2
    )

    assert reverse_move.candidate_tabu_key == move.commit_tabu_key


def test_swap_and_two_opt_moves_have_self_inverse_tabu_keys() -> None:
    """Swap and 2-opt undo themselves when repeated, so their commit and candidate keys must coincide."""
    swap = SwapMove(
        first_customer_id="3",
        first_vehicle_id="VAN-1",
        first_position=0,
        second_customer_id="8",
        second_vehicle_id="VAN-2",
        second_position=1,
    )
    assert swap.candidate_tabu_key == swap.commit_tabu_key

    two_opt = TwoOptMove(
        vehicle_id="VAN-1",
        segment_start_position=1,
        segment_end_position=3,
        segment_start_customer_id="11",
        segment_end_customer_id="17",
    )
    assert two_opt.candidate_tabu_key == two_opt.commit_tabu_key


def test_two_opt_tabu_key_is_independent_of_array_positions() -> None:
    """
    A 2-opt tabu key must depend only on the reversed segment's boundary
    customers, not on their array positions, since other Relocate and Swap
    moves can shift positions on a route across iterations without changing
    which customers occupy a given segment.
    """
    move_at_early_positions = TwoOptMove(
        vehicle_id="VAN-1",
        segment_start_position=0,
        segment_end_position=2,
        segment_start_customer_id="5",
        segment_end_customer_id="9",
    )
    move_at_later_positions = TwoOptMove(
        vehicle_id="VAN-1",
        segment_start_position=4,
        segment_end_position=6,
        segment_start_customer_id="5",
        segment_end_customer_id="9",
    )

    assert move_at_early_positions.candidate_tabu_key == move_at_later_positions.candidate_tabu_key


# ---------------------------------------------------------------------------
# End-to-end anti-cycling proof (synthetic, fully deterministic scenario)
# ---------------------------------------------------------------------------


def test_tabu_list_prevents_reverting_the_committed_relocate_immediately() -> None:
    """
    Prove, end to end, that the tabu list stops the search from immediately
    undoing a beneficial move.

    The scenario is deliberately minimal: a single customer whose demand
    only fits vehicle "VAN-2"'s capacity starts out, wrongly, assigned to
    "VAN-1". With a single customer in the whole workday, Swap and 2-opt
    have no candidates at all (both need at least two customers), so
    Relocate between these two routes is the only move available at every
    iteration. The first iteration must therefore relocate the customer to
    VAN-2. The only conceivable move at the second iteration is relocating
    it straight back to VAN-1, which is exactly the move the tabu list has
    just forbidden, and which would not reach a new best cost, so the
    aspiration criterion does not rescue it either. With no admissible move
    left, the search must stop after a single committed iteration, and must
    never revert to the penalized state.
    """
    depot_node_id = 0
    customer_node_id = 1
    cost_matrix = _build_synthetic_cost_matrix((depot_node_id, customer_node_id))

    customer = Customer(node_id=customer_node_id, demand=20.0, time_window=TimeWindow(0.0, math.inf))
    undersized_vehicle = Vehicle(vehicle_id="VAN-1", max_capacity=5.0)
    adequate_vehicle = Vehicle(vehicle_id="VAN-2", max_capacity=50.0)
    workday = WorkdayInstance(customers=(customer,), fleet=(undersized_vehicle, adequate_vehicle))

    initial_state = VRPState(
        routes=(
            Route(vehicle_id="VAN-1", customer_sequence=(customer.customer_id,)),
            Route(vehicle_id="VAN-2", customer_sequence=()),
        )
    )

    config = TabuSearchConfig(
        max_iterations=5, max_iterations_without_improvement=5, time_limit_seconds=5.0, tabu_tenure=3
    )
    result = run_tabu_search(initial_state, workday, cost_matrix, config)

    assert result.iterations_completed == 1

    routes_by_vehicle_id = {route.vehicle_id: route for route in result.best_state.routes}
    assert routes_by_vehicle_id["VAN-2"].customer_sequence == (customer.customer_id,)
    assert routes_by_vehicle_id["VAN-1"].customer_sequence == ()
    assert result.best_evaluation.is_feasible


# ---------------------------------------------------------------------------
# Precedence penalty for pickup-and-delivery pairs (synthetic, deterministic)
# ---------------------------------------------------------------------------


def _build_pickup_delivery_pair(depot_node_id: int, delivery_node_id: int) -> tuple[Customer, Customer]:
    """Build a mutually paired depot pickup and customer delivery stop."""
    pickup = Customer(
        node_id=depot_node_id,
        demand=0.0,
        customer_id="pickup-1",
        is_pickup_stop=True,
        paired_customer_id="delivery-1",
    )
    delivery = Customer(
        node_id=delivery_node_id,
        demand=10.0,
        customer_id="delivery-1",
        is_pickup_stop=False,
        paired_customer_id="pickup-1",
    )
    return pickup, delivery


def test_precedence_violation_is_recorded_when_delivery_precedes_pickup() -> None:
    """A route visiting the delivery before its paired pickup must record exactly one violation."""
    depot_node_id = 0
    delivery_node_id = 1
    cost_matrix = _build_synthetic_cost_matrix((depot_node_id, delivery_node_id))
    pickup, delivery = _build_pickup_delivery_pair(depot_node_id, delivery_node_id)
    workday = WorkdayInstance(customers=(pickup, delivery), fleet=(Vehicle(vehicle_id="VAN-1", max_capacity=50.0),))

    out_of_order_simulation = simulate_route_clock(
        (delivery.customer_id, pickup.customer_id),
        workday.fleet_by_vehicle_id["VAN-1"],
        workday,
        cost_matrix,
        build_schedule=False,
    )

    assert out_of_order_simulation.precedence_violations == 1
    assert out_of_order_simulation.violating_customer_ids == (delivery.customer_id,)


def test_precedence_is_not_violated_when_pickup_precedes_delivery() -> None:
    """A route visiting the pickup before its paired delivery must record zero violations."""
    depot_node_id = 0
    delivery_node_id = 1
    cost_matrix = _build_synthetic_cost_matrix((depot_node_id, delivery_node_id))
    pickup, delivery = _build_pickup_delivery_pair(depot_node_id, delivery_node_id)
    workday = WorkdayInstance(customers=(pickup, delivery), fleet=(Vehicle(vehicle_id="VAN-1", max_capacity=50.0),))

    in_order_simulation = simulate_route_clock(
        (pickup.customer_id, delivery.customer_id),
        workday.fleet_by_vehicle_id["VAN-1"],
        workday,
        cost_matrix,
        build_schedule=False,
    )

    assert in_order_simulation.precedence_violations == 0
    assert in_order_simulation.violating_customer_ids == ()


def test_precedence_violation_is_recorded_when_pair_is_split_across_vehicles() -> None:
    """
    A delivery visited by a route that never visits its paired pickup at all
    (because the pickup was assigned to a different vehicle) must still be
    recorded as a precedence violation, since the pickup was never visited by
    *that* route regardless of what any other vehicle does.
    """
    depot_node_id = 0
    delivery_node_id = 1
    cost_matrix = _build_synthetic_cost_matrix((depot_node_id, delivery_node_id))
    pickup, delivery = _build_pickup_delivery_pair(depot_node_id, delivery_node_id)
    vehicle = Vehicle(vehicle_id="VAN-2", max_capacity=50.0)
    workday = WorkdayInstance(customers=(pickup, delivery), fleet=(vehicle,))

    delivery_only_simulation = simulate_route_clock(
        (delivery.customer_id,), vehicle, workday, cost_matrix, build_schedule=False
    )

    assert delivery_only_simulation.precedence_violations == 1
    assert delivery_only_simulation.violating_customer_ids == (delivery.customer_id,)


def test_evaluate_route_cost_folds_in_the_precedence_penalty() -> None:
    """`evaluate_route_cost` must add `precedence_penalty_weight` once per violation."""
    depot_node_id = 0
    delivery_node_id = 1
    cost_matrix = _build_synthetic_cost_matrix((depot_node_id, delivery_node_id))
    pickup, delivery = _build_pickup_delivery_pair(depot_node_id, delivery_node_id)
    vehicle = Vehicle(vehicle_id="VAN-1", max_capacity=50.0)
    workday = WorkdayInstance(customers=(pickup, delivery), fleet=(vehicle,))
    weights = EvaluationWeights(precedence_penalty_weight=250000.0)

    out_of_order_cost = evaluate_route_cost(
        (delivery.customer_id, pickup.customer_id), vehicle, workday, cost_matrix, weights
    )
    in_order_cost = evaluate_route_cost(
        (pickup.customer_id, delivery.customer_id), vehicle, workday, cost_matrix, weights
    )

    assert out_of_order_cost == pytest.approx(in_order_cost + weights.precedence_penalty_weight)


# ---------------------------------------------------------------------------
# Delta costing vs. full recomputation (real street network)
# ---------------------------------------------------------------------------


def test_candidate_cost_matches_full_state_recomputation(malaga_street_network: nx.MultiDiGraph) -> None:
    """
    Confirm that the incremental candidate costing `run_tabu_search` performs
    (costing only the one or two routes a move touches, combined with the
    cached cost of every untouched route) matches, exactly, a full
    `evaluate_state` recomputation of the resulting state from scratch, for
    every move in a real neighborhood. This is what guarantees the search's
    fast path can never silently drift from the authoritative evaluator.
    """
    depot_node, customer_nodes = select_demonstration_nodes(malaga_street_network, customer_count=14)
    cost_matrix = build_cost_matrix(malaga_street_network, depot_node, customer_nodes)
    workday = _build_demo_workday(customer_nodes, vehicle_count=3)
    initial_state = build_initial_state(workday, cost_matrix)
    weights = EvaluationWeights()

    current_sequences = {route.vehicle_id: route.customer_sequence for route in initial_state.routes}
    vehicle_ids = tuple(current_sequences.keys())
    route_cost_by_vehicle_id = {
        vehicle_id: evaluate_route_cost(
            current_sequences[vehicle_id], workday.fleet_by_vehicle_id[vehicle_id], workday, cost_matrix, weights
        )
        for vehicle_id in vehicle_ids
    }

    candidates_checked = 0
    for move in _generate_all_moves(current_sequences, vehicle_ids, {}):
        touched_sequences = move.touched_sequences(current_sequences)
        touched_costs = {
            vehicle_id: evaluate_route_cost(
                sequence, workday.fleet_by_vehicle_id[vehicle_id], workday, cost_matrix, weights
            )
            for vehicle_id, sequence in touched_sequences.items()
        }
        incremental_total_cost = sum(touched_costs.values()) + sum(
            cost for vehicle_id, cost in route_cost_by_vehicle_id.items() if vehicle_id not in touched_costs
        )

        candidate_routes = tuple(
            Route(
                vehicle_id=vehicle_id,
                customer_sequence=touched_sequences.get(vehicle_id, current_sequences[vehicle_id]),
            )
            for vehicle_id in vehicle_ids
        )
        full_recomputation_cost = evaluate_state(
            VRPState(routes=candidate_routes), workday, cost_matrix, weights
        ).total_cost

        # Summing the touched and cached route costs in a different order than
        # evaluate_state's natural left-to-right route order can differ by a
        # single unit in the last place of the float; pytest.approx absorbs
        # that without masking a genuine algorithmic discrepancy.
        assert incremental_total_cost == pytest.approx(full_recomputation_cost)
        candidates_checked += 1

    assert candidates_checked > 0


# ---------------------------------------------------------------------------
# Locked-prefix safety (real street network, dynamic re-optimization mode)
# ---------------------------------------------------------------------------


def test_locked_prefix_positions_are_never_touched(malaga_street_network: nx.MultiDiGraph) -> None:
    """
    Confirm that every locked leading position of every route is bit-identical
    between the initial state and the search result, proving the dynamic
    re-optimization mode never rearranges stops already marked as completed.
    """
    depot_node, customer_nodes = select_demonstration_nodes(malaga_street_network, customer_count=18)
    cost_matrix = build_cost_matrix(malaga_street_network, depot_node, customer_nodes)
    workday = _build_demo_workday(customer_nodes, vehicle_count=3)
    initial_state = build_initial_state(workday, cost_matrix)

    locked_prefix_lengths = {
        route.vehicle_id: min(2, len(route.customer_sequence)) for route in initial_state.routes
    }

    config = TabuSearchConfig(
        max_iterations=150, max_iterations_without_improvement=60, time_limit_seconds=3.0, tabu_tenure=10
    )
    result = run_tabu_search(initial_state, workday, cost_matrix, config, locked_prefix_lengths=locked_prefix_lengths)

    initial_routes_by_vehicle_id = {route.vehicle_id: route for route in initial_state.routes}
    for route in result.best_state.routes:
        lock_length = locked_prefix_lengths[route.vehicle_id]
        expected_prefix = initial_routes_by_vehicle_id[route.vehicle_id].customer_sequence[:lock_length]
        assert route.customer_sequence[:lock_length] == expected_prefix


# ---------------------------------------------------------------------------
# Benchmarks (real street network, same scale as the Phase 2.3 benchmarks)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("customer_count", [30, 50, 70, 100])
def test_tabu_search_improves_cost_and_reports_throughput(
    malaga_street_network: nx.MultiDiGraph, customer_count: int
) -> None:
    """
    Benchmark `run_tabu_search` at the same customer-count scale used to
    benchmark the Phase 2.3 constructive heuristic (30/50/70/100 customers),
    asserting the search never worsens the initial solution and reporting
    iterations per second, cited in `docs/TECHNICAL_CHANGELOG.md`.
    """
    depot_node, customer_nodes = select_demonstration_nodes(malaga_street_network, customer_count)
    cost_matrix = build_cost_matrix(malaga_street_network, depot_node, customer_nodes)
    vehicle_count = max(3, customer_count // 15)
    workday = _build_demo_workday(customer_nodes, vehicle_count)
    initial_state = build_initial_state(workday, cost_matrix)
    initial_evaluation = evaluate_state(initial_state, workday, cost_matrix)

    config = TabuSearchConfig(
        max_iterations=300, max_iterations_without_improvement=80, time_limit_seconds=3.0, tabu_tenure=15
    )
    result = run_tabu_search(initial_state, workday, cost_matrix, config)

    iterations_per_second = (
        result.iterations_completed / result.elapsed_seconds if result.elapsed_seconds > 0.0 else float("inf")
    )
    print(
        f"\n[metaheuristic benchmark] customers={customer_count} vehicles={vehicle_count} "
        f"iterations={result.iterations_completed} elapsed={result.elapsed_seconds:.3f}s "
        f"({iterations_per_second:.2f} it/s) initial_cost={initial_evaluation.total_cost:.2f} "
        f"best_cost={result.best_evaluation.total_cost:.2f}"
    )

    assert result.best_evaluation.total_cost <= initial_evaluation.total_cost
    assert result.best_evaluation.is_feasible
