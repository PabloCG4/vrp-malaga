"""
Regression tests protecting the mathematical invariants of the cost matrix.

Two families of guarantees are exercised, both against the real, preprocessed
Malaga street network loaded by `conftest.malaga_street_network`, with no mocks
or synthetic graphs standing in for it:

1. Integrity and asymmetry of a freshly built N x N cost matrix: correct
   shape, a zero diagonal, full reachability (guaranteed by the strongly
   connected component the extractor module produces), non-negative costs, and
   genuine asymmetry, since the network is directed. Every entry is also
   cross-checked against an independent `networkx.single_source_dijkstra_path_length`
   computation, so the module's own targeted Dijkstra cannot silently diverge
   from a trusted reference.
2. The "patched equals rebuilt" invariant that makes dynamic street closures
   safe: after `CostMatrix.apply_street_closures` repairs only the rows the
   inverted edge index flags as affected, the resulting matrices, shortest-path
   trees and inverted index must be indistinguishable from those of a matrix
   built completely from scratch under the same closed-edge conditions.
"""

from __future__ import annotations

import math
import random

import networkx as nx
import numpy as np
import pytest
from matrix import (
    CostMatrix,
    EdgeIdentifier,
    build_cost_matrix,
)

# Number of customers sampled for the workday instance under test. Small enough
# to keep the suite fast, large enough to reliably expose asymmetric costs and
# closures with an observable, non-trivial impact.
SAMPLED_CUSTOMER_COUNT: int = 24

# Seed guaranteeing that the sampled workday nodes are identical across runs,
# so that any failure is deterministic and reproducible.
NODE_SAMPLING_SEED: int = 2026

# Numerical tolerance for floating-point comparisons between independently
# computed matrices.
FLOATING_POINT_TOLERANCE: float = 1e-9


@pytest.fixture(scope="module")
def sampled_workday_nodes(malaga_street_network: nx.MultiDiGraph) -> tuple[int, list[int]]:
    """
    Draw a reproducible depot and customer set from the real street network.

    Parameters
    ----------
    malaga_street_network:
        The preprocessed graph to sample nodes from.

    Returns
    -------
    tuple[int, list[int]]
        The depot node and the list of customer nodes, disjoint from it.
    """
    sortable_node_ids = sorted(malaga_street_network.nodes)
    sampled_node_ids = random.Random(NODE_SAMPLING_SEED).sample(
        sortable_node_ids, SAMPLED_CUSTOMER_COUNT + 1
    )

    return sampled_node_ids[0], sampled_node_ids[1:]


@pytest.fixture(scope="module")
def pristine_cost_matrix(
    malaga_street_network: nx.MultiDiGraph, sampled_workday_nodes: tuple[int, list[int]]
) -> CostMatrix:
    """
    Build the workday cost matrix once and share it read-only across tests.

    Building the matrix runs a real Dijkstra search from every node of
    interest over the full network, so it is deliberately built once per
    module instead of per test. Tests that need to mutate a matrix, for
    example by closing streets, must use the `cost_matrix` fixture instead,
    which hands out an independent clone.

    Parameters
    ----------
    malaga_street_network:
        The preprocessed graph the matrix is built from.
    sampled_workday_nodes:
        The depot and customers of the workday instance under test.

    Returns
    -------
    CostMatrix
        The fully populated, unmodified reference matrix.
    """
    depot_node, customer_nodes = sampled_workday_nodes

    return build_cost_matrix(malaga_street_network, depot_node, customer_nodes)


@pytest.fixture
def cost_matrix(pristine_cost_matrix: CostMatrix) -> CostMatrix:
    """
    Provide an independent, mutable clone of the pristine matrix per test.

    Cloning copies the NumPy matrices and the auxiliary dictionaries and sets,
    but reuses the same graph object, since the graph itself is never mutated
    by any operation under test. This keeps every test isolated from the
    mutations performed by the others without paying the cost of a fresh
    Dijkstra-based rebuild for each one.

    Parameters
    ----------
    pristine_cost_matrix:
        The shared, unmodified reference matrix to clone.

    Returns
    -------
    CostMatrix
        A deep-enough copy safe to mutate within a single test.
    """
    return clone_cost_matrix(pristine_cost_matrix)


def clone_cost_matrix(source: CostMatrix) -> CostMatrix:
    """
    Create an independent copy of a cost matrix's mutable state.

    Parameters
    ----------
    source:
        Matrix to clone.

    Returns
    -------
    CostMatrix
        A new `CostMatrix` sharing the same street network graph reference
        but with its own matrices, closed-edge set, shortest-path trees and
        inverted index, so that mutating the clone never affects `source`.
    """
    return CostMatrix(
        street_network_graph=source.street_network_graph,
        node_ids=source.node_ids,
        matrix_index_by_node=dict(source.matrix_index_by_node),
        travel_time_matrix=source.travel_time_matrix.copy(),
        distance_matrix=source.distance_matrix.copy(),
        closed_edges=set(source.closed_edges),
        pruned_tree_by_origin={
            origin_node: dict(tree) for origin_node, tree in source.pruned_tree_by_origin.items()
        },
        origin_indices_by_edge={
            edge: set(origin_indices) for edge, origin_indices in source.origin_indices_by_edge.items()
        },
    )


def select_active_edge(
    cost_matrix_under_test: CostMatrix, random_generator: random.Random | None = None
) -> EdgeIdentifier:
    """
    Pick an edge that at least one current optimal path actually traverses.

    Parameters
    ----------
    cost_matrix_under_test:
        Matrix whose inverted index is sampled.
    random_generator:
        Source of randomness used to pick among all active edges. When
        omitted, the single busiest edge (the one the largest number of rows
        depend on) is returned instead, which keeps single-closure tests
        fully deterministic and exercises the largest possible repair.

    Returns
    -------
    EdgeIdentifier
        An edge guaranteed to be a key of `origin_indices_by_edge`, so closing
        it is guaranteed to invalidate at least one row.
    """
    active_edges = sorted(cost_matrix_under_test.origin_indices_by_edge)
    if not active_edges:
        pytest.fail("The sampled workday has no active edges to close, cannot exercise a patch.")

    if random_generator is None:
        return max(
            active_edges, key=lambda edge: len(cost_matrix_under_test.origin_indices_by_edge[edge])
        )

    return random_generator.choice(active_edges)


def select_lightly_used_active_edge(cost_matrix_under_test: CostMatrix) -> EdgeIdentifier:
    """
    Pick an active edge whose closure is expected to leave some rows untouched.

    Unlike `select_active_edge`, which defaults to the single busiest edge to
    exercise the largest possible repair, this helper deliberately picks the
    least depended-upon active edge, so that a test can assert that rows not
    depending on it are provably left untouched by the patch.

    Parameters
    ----------
    cost_matrix_under_test:
        Matrix whose inverted index is sampled.

    Returns
    -------
    EdgeIdentifier
        An active edge that at least one, but not every, row depends on.
    """
    active_edges = sorted(cost_matrix_under_test.origin_indices_by_edge)
    if not active_edges:
        pytest.fail("The sampled workday has no active edges to close, cannot exercise a patch.")

    return min(
        active_edges, key=lambda edge: len(cost_matrix_under_test.origin_indices_by_edge[edge])
    )


def build_reference_matrix_from_scratch(
    street_network_graph: nx.MultiDiGraph,
    node_ids: tuple[int, ...],
    matrix_index_by_node: dict[int, int],
    closed_edges: set[EdgeIdentifier],
) -> CostMatrix:
    """
    Build a brand-new cost matrix, bypassing every incremental patching path.

    `CostMatrix.rebuild_all_rows` discards any previous state and recomputes
    the complete N x N matrices, the pruned shortest-path trees and the
    inverted index from an empty starting point, consulting only
    `closed_edges` to exclude streets from the search. Comparing a matrix that
    reached the same closed-edge state through `apply_street_closures` against
    this reference is therefore an independent, from-scratch check of the
    incremental repair, sharing no mutable state with the code path under test.

    Parameters
    ----------
    street_network_graph:
        The street network the matrix is built over.
    node_ids:
        Nodes of interest, in matrix order, with the depot first.
    matrix_index_by_node:
        Reverse lookup from graph node identifier to matrix index.
    closed_edges:
        Edges to exclude from every shortest-path search.

    Returns
    -------
    CostMatrix
        The freshly rebuilt reference matrix.
    """
    reference_matrix = CostMatrix(
        street_network_graph=street_network_graph,
        node_ids=node_ids,
        matrix_index_by_node=dict(matrix_index_by_node),
        closed_edges=set(closed_edges),
    )
    reference_matrix.rebuild_all_rows()

    return reference_matrix


class TestMatrixIntegrityAndAsymmetry:
    """Validate the mathematical shape and semantics of a freshly built cost matrix."""

    def test_matrices_have_the_expected_shape(self, pristine_cost_matrix: CostMatrix) -> None:
        """Both matrices must be square with side N, the number of nodes of interest."""
        expected_shape = (pristine_cost_matrix.node_count, pristine_cost_matrix.node_count)

        assert pristine_cost_matrix.node_count == SAMPLED_CUSTOMER_COUNT + 1
        assert pristine_cost_matrix.travel_time_matrix.shape == expected_shape
        assert pristine_cost_matrix.distance_matrix.shape == expected_shape

    def test_depot_occupies_matrix_index_zero(
        self, pristine_cost_matrix: CostMatrix, sampled_workday_nodes: tuple[int, list[int]]
    ) -> None:
        """The optimization engine relies on the depot always being row and column 0."""
        depot_node, _ = sampled_workday_nodes

        assert pristine_cost_matrix.node_ids[0] == depot_node
        assert pristine_cost_matrix.depot_node == depot_node

    def test_diagonal_is_exactly_zero(self, pristine_cost_matrix: CostMatrix) -> None:
        """The cost of staying at the same node must be exactly zero, not merely small."""
        assert np.all(np.diagonal(pristine_cost_matrix.travel_time_matrix) == 0.0)
        assert np.all(np.diagonal(pristine_cost_matrix.distance_matrix) == 0.0)

    def test_no_unreachable_node_artifacts(self, pristine_cost_matrix: CostMatrix) -> None:
        """
        Every pair of nodes of interest must be connected. The extractor module
        reduces the network to its largest strongly connected component before
        serialization, which mathematically guarantees mutual reachability for
        any subset of its nodes; an infinite entry here would indicate that
        guarantee had been silently broken upstream.
        """
        assert pristine_cost_matrix.find_unreachable_pairs() == ()
        assert np.all(np.isfinite(pristine_cost_matrix.travel_time_matrix))
        assert np.all(np.isfinite(pristine_cost_matrix.distance_matrix))

    def test_costs_are_non_negative(self, pristine_cost_matrix: CostMatrix) -> None:
        """Travel time and distance are physical quantities and cannot be negative."""
        assert np.all(pristine_cost_matrix.travel_time_matrix >= 0.0)
        assert np.all(pristine_cost_matrix.distance_matrix >= 0.0)

    def test_matrices_are_asymmetric(self, pristine_cost_matrix: CostMatrix) -> None:
        """
        The street network is directed, with one-way streets, so the cost of a
        trip generally differs from the cost of its reverse. A symmetric matrix
        would mean the directionality of the graph was lost somewhere in the
        computation.
        """
        assert not np.allclose(
            pristine_cost_matrix.travel_time_matrix, pristine_cost_matrix.travel_time_matrix.T
        )
        assert not np.allclose(
            pristine_cost_matrix.distance_matrix, pristine_cost_matrix.distance_matrix.T
        )

    def test_accessors_agree_with_direct_matrix_indexing(
        self, pristine_cost_matrix: CostMatrix
    ) -> None:
        """The O(1) accessor methods must return exactly what direct NumPy indexing does."""
        for origin_index, origin_node in enumerate(pristine_cost_matrix.node_ids):
            for destination_index, destination_node in enumerate(pristine_cost_matrix.node_ids):
                assert pristine_cost_matrix.travel_time_between(
                    origin_node, destination_node
                ) == pytest.approx(
                    float(pristine_cost_matrix.travel_time_matrix[origin_index, destination_index])
                )
                assert pristine_cost_matrix.distance_between(
                    origin_node, destination_node
                ) == pytest.approx(
                    float(pristine_cost_matrix.distance_matrix[origin_index, destination_index])
                )

    def test_matches_independent_networkx_dijkstra(
        self, malaga_street_network: nx.MultiDiGraph, pristine_cost_matrix: CostMatrix
    ) -> None:
        """
        Cross-validate every travel time against NetworkX's own Dijkstra
        implementation, computed independently over the same "travel_time"
        edge weight. This guards against the module's targeted, early-
        terminating Dijkstra silently diverging from a trusted reference
        algorithm.
        """
        for origin_node in pristine_cost_matrix.node_ids:
            reference_travel_times = nx.single_source_dijkstra_path_length(
                malaga_street_network, origin_node, weight="travel_time"
            )
            for destination_node in pristine_cost_matrix.node_ids:
                assert pristine_cost_matrix.travel_time_between(
                    origin_node, destination_node
                ) == pytest.approx(
                    reference_travel_times[destination_node], abs=FLOATING_POINT_TOLERANCE
                )

    def test_evaluate_route_matches_manual_leg_summation(
        self, pristine_cost_matrix: CostMatrix, sampled_workday_nodes: tuple[int, list[int]]
    ) -> None:
        """The vectorized route evaluation must agree with summing individual legs."""
        depot_node, customer_nodes = sampled_workday_nodes
        route = [depot_node, *customer_nodes[:6], depot_node]

        total_travel_time, total_distance = pristine_cost_matrix.evaluate_route(route)
        manual_travel_time = sum(
            pristine_cost_matrix.travel_time_between(route[position], route[position + 1])
            for position in range(len(route) - 1)
        )
        manual_distance = sum(
            pristine_cost_matrix.distance_between(route[position], route[position + 1])
            for position in range(len(route) - 1)
        )

        assert total_travel_time == pytest.approx(manual_travel_time)
        assert total_distance == pytest.approx(manual_distance)


class TestDynamicPatchingInvariant:
    """
    Validate that incrementally repairing the matrices after a street closure
    is mathematically equivalent to rebuilding them from scratch.
    """

    def test_patched_matrix_equals_full_rebuild_after_closure(
        self, cost_matrix: CostMatrix
    ) -> None:
        """
        This is the central invariant of the dynamic patching design: closing
        an edge that is actively used by at least one optimal path and
        repairing only the rows the inverted index flags must produce time and
        distance matrices that are numerically identical, within floating
        point tolerance, to a full rebuild performed from scratch under the
        same closed-edge condition.
        """
        edge_to_close = select_active_edge(cost_matrix)

        patch_report = cost_matrix.apply_street_closures([edge_to_close])
        assert patch_report.applied_edges == (edge_to_close,)
        assert len(patch_report.affected_origin_nodes) > 0

        reference_matrix = build_reference_matrix_from_scratch(
            cost_matrix.street_network_graph,
            cost_matrix.node_ids,
            cost_matrix.matrix_index_by_node,
            {edge_to_close},
        )

        assert np.allclose(
            cost_matrix.travel_time_matrix,
            reference_matrix.travel_time_matrix,
            atol=FLOATING_POINT_TOLERANCE,
        )
        assert np.allclose(
            cost_matrix.distance_matrix,
            reference_matrix.distance_matrix,
            atol=FLOATING_POINT_TOLERANCE,
        )

    def test_patched_matrix_equals_rebuild_on_a_physically_modified_graph(
        self, cost_matrix: CostMatrix
    ) -> None:
        """
        Strengthens the previous test by validating against a completely
        independent code path: instead of marking the edge as closed inside a
        `CostMatrix`, it is physically deleted from a separate copy of the
        street network, and a fresh matrix is built over that modified graph
        through the public `build_cost_matrix` entry point. Agreement between
        the two confirms that excluding a closed edge during the search is
        truly equivalent to the edge not existing at all.
        """
        edge_to_close = select_active_edge(cost_matrix)

        cost_matrix.apply_street_closures([edge_to_close])

        modified_graph = cost_matrix.street_network_graph.copy()
        modified_graph.remove_edge(*edge_to_close)
        reference_matrix = build_cost_matrix(
            modified_graph, cost_matrix.node_ids[0], list(cost_matrix.node_ids[1:])
        )

        assert np.allclose(
            cost_matrix.travel_time_matrix,
            reference_matrix.travel_time_matrix,
            atol=FLOATING_POINT_TOLERANCE,
        )
        assert np.allclose(
            cost_matrix.distance_matrix,
            reference_matrix.distance_matrix,
            atol=FLOATING_POINT_TOLERANCE,
        )

    def test_untouched_rows_are_left_bit_identical(self, cost_matrix: CostMatrix) -> None:
        """
        Rows whose optimal path never traverses the closed edge are provably
        still optimal, since removing an edge can only increase costs. The
        patch must therefore leave them bit-for-bit untouched rather than
        merely numerically unchanged, avoiding any wasted recomputation.
        """
        edge_to_close = select_lightly_used_active_edge(cost_matrix)
        travel_time_before_patch = cost_matrix.travel_time_matrix.copy()
        distance_before_patch = cost_matrix.distance_matrix.copy()

        patch_report = cost_matrix.apply_street_closures([edge_to_close])
        affected_indices = {
            cost_matrix.index_of_node(node_id) for node_id in patch_report.affected_origin_nodes
        }
        untouched_indices = [
            index for index in range(cost_matrix.node_count) if index not in affected_indices
        ]

        assert len(untouched_indices) > 0
        assert np.array_equal(
            cost_matrix.travel_time_matrix[untouched_indices, :],
            travel_time_before_patch[untouched_indices, :],
        )
        assert np.array_equal(
            cost_matrix.distance_matrix[untouched_indices, :],
            distance_before_patch[untouched_indices, :],
        )

    def test_patched_auxiliary_structures_match_full_rebuild(
        self, cost_matrix: CostMatrix
    ) -> None:
        """
        The dynamic patching structures themselves, not only the numeric
        matrices, must match a from-scratch rebuild: the inverted edge index,
        because it drives every future closure's targeting, and the pruned
        shortest-path trees, because they are what the simulation walks to
        advance a vehicle along its route.
        """
        edge_to_close = select_active_edge(cost_matrix)

        cost_matrix.apply_street_closures([edge_to_close])
        reference_matrix = build_reference_matrix_from_scratch(
            cost_matrix.street_network_graph,
            cost_matrix.node_ids,
            cost_matrix.matrix_index_by_node,
            {edge_to_close},
        )

        assert cost_matrix.origin_indices_by_edge == reference_matrix.origin_indices_by_edge
        assert cost_matrix.pruned_tree_by_origin == reference_matrix.pruned_tree_by_origin

    def test_closing_an_unused_edge_is_a_true_no_op(self, cost_matrix: CostMatrix) -> None:
        """
        Closing a street that no current optimal path traverses must not
        trigger any recomputation at all, which is what makes the inverted
        index precise rather than merely conservative.
        """
        used_edges = set(cost_matrix.origin_indices_by_edge)
        unused_edge = next(
            (
                edge
                for edge in cost_matrix.street_network_graph.edges(keys=True)
                if edge not in used_edges
            ),
            None,
        )
        if unused_edge is None:
            pytest.skip("Every edge of the sampled network happens to be in active use.")

        travel_time_before_patch = cost_matrix.travel_time_matrix.copy()
        distance_before_patch = cost_matrix.distance_matrix.copy()

        patch_report = cost_matrix.apply_street_closures([unused_edge])

        assert patch_report.recomputed_pair_count == 0
        assert patch_report.affected_origin_nodes == ()
        assert np.array_equal(cost_matrix.travel_time_matrix, travel_time_before_patch)
        assert np.array_equal(cost_matrix.distance_matrix, distance_before_patch)

    def test_reopening_a_closed_street_restores_the_pristine_matrix(
        self, cost_matrix: CostMatrix, pristine_cost_matrix: CostMatrix
    ) -> None:
        """Closing and then reopening the same street must be a perfect round trip."""
        edge_to_close = select_active_edge(cost_matrix)

        cost_matrix.apply_street_closures([edge_to_close])
        cost_matrix.reopen_streets([edge_to_close])

        assert cost_matrix.closed_edges == set()
        assert np.array_equal(
            cost_matrix.travel_time_matrix, pristine_cost_matrix.travel_time_matrix
        )
        assert np.array_equal(cost_matrix.distance_matrix, pristine_cost_matrix.distance_matrix)

    def test_sequential_closures_remain_consistent_with_rebuild_at_every_step(
        self, cost_matrix: CostMatrix
    ) -> None:
        """
        Applies a short sequence of closures and, after every single one,
        verifies the patched matrix still equals an independent full rebuild
        under the cumulative closed-edge state. A single-closure test cannot
        catch state incorrectly leaking between successive patches, such as a
        stale inverted index entry surviving a previous repair; this one can.
        """
        random_generator = random.Random(99)
        cumulative_closed_edges: set[EdgeIdentifier] = set()

        for closure_round in range(4):
            edge_to_close = select_active_edge(cost_matrix, random_generator)
            cost_matrix.apply_street_closures([edge_to_close])
            cumulative_closed_edges.add(edge_to_close)

            reference_matrix = build_reference_matrix_from_scratch(
                cost_matrix.street_network_graph,
                cost_matrix.node_ids,
                cost_matrix.matrix_index_by_node,
                cumulative_closed_edges,
            )

            assert np.allclose(
                cost_matrix.travel_time_matrix,
                reference_matrix.travel_time_matrix,
                atol=FLOATING_POINT_TOLERANCE,
            ), f"Patched matrix diverged from a full rebuild after closure round {closure_round}."

    def test_isolating_a_customer_reports_unreachability_without_raising(
        self, cost_matrix: CostMatrix
    ) -> None:
        """
        Cutting every inbound edge of a customer must surface the resulting
        unreachability through `PatchReport.newly_unreachable_pairs`, a
        legitimate simulation event, rather than raising an exception.
        """
        isolated_customer = cost_matrix.node_ids[1]
        incoming_edges = tuple(
            cost_matrix.street_network_graph.in_edges(isolated_customer, keys=True)
        )
        assert incoming_edges, "The sampled customer has no inbound edges to close."

        patch_report = cost_matrix.apply_street_closures(incoming_edges)

        assert len(patch_report.newly_unreachable_pairs) > 0
        assert all(
            math.isinf(cost_matrix.travel_time_between(origin_node, isolated_customer))
            for origin_node in cost_matrix.node_ids
            if origin_node != isolated_customer
        )
