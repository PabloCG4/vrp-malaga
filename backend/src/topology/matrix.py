"""
Cost matrix generation and dynamic patching for the VRP mathematical engine.

A metaheuristic evaluates millions of candidate routes per second, so it can
never afford to run a shortest-path search on the street graph while it is
optimizing. This module therefore precomputes, for the N nodes relevant to a
given workday (one depot plus K customers), a pair of dense N x N asymmetric
cost matrices holding travel time and distance, reducing every route
evaluation during the search to O(1) array lookups.

The matrices are asymmetric by construction: the underlying network is a
directed graph with one-way streets, so the cost from origin to destination is
generally not the cost of the reverse trip.

Dynamic preparedness for street closures is provided by an inverted edge
index. While the shortest paths are computed, the module records which graph
edges are actually traversed by the paths of the matrix. When a street is cut
during the simulation, that index identifies exactly which origin-destination
pairs depended on the closed edge, so only those entries are invalidated and
recomputed instead of rebuilding the whole matrix.

Two deliberate design decisions differ from a naive edge-to-pair mapping:

1. The inverted index maps each edge to the set of matrix *rows* (origins)
   whose paths traverse it, not to individual origin-destination pairs. Since
   Dijkstra is inherently a one-to-many algorithm, recomputing a single pair
   costs essentially the same as recomputing its entire row, so the row is the
   natural unit of repair. This keeps the index an order of magnitude smaller
   in memory while losing nothing in repair cost. The exact set of affected
   pairs remains recoverable on demand via `find_affected_pairs`.
2. Instead of storing the explicit node sequence of all N x N paths, only one
   pruned shortest-path tree per origin is kept, from which any path is
   reconstructed in time proportional to its length. This is what the
   simulation needs to advance vehicles node by node, and it costs far less
   memory than materializing N x N paths.

The correctness of partial repair after a closure relies on the monotonicity
of shortest paths under edge removal: closing edges can only increase path
costs, so a path that does not use any closed edge retains its previous cost
and therefore remains optimal. Reopening a street breaks that argument and
consequently forces a full rebuild.
"""

from __future__ import annotations

import heapq
import math
import random
import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import networkx as nx
import numpy as np

# Edge attribute minimized by the shortest-path search, in seconds.
TRAVEL_TIME_ATTRIBUTE: str = "travel_time"

# Edge attribute accumulated along the optimal path, in meters.
DISTANCE_ATTRIBUTE: str = "length"

# Row and column of the depot inside the cost matrices. The optimization engine
# relies on the depot occupying a fixed, known position.
DEPOT_MATRIX_INDEX: int = 0

# A directed graph edge, identified by origin node, destination node and the
# parallel edge key that MultiDiGraph uses to distinguish duplicated streets.
EdgeIdentifier = tuple[int, int, int]

# An origin-destination pair expressed as graph node identifiers.
NodePair = tuple[int, int]


class CostMatrixError(Exception):
    """Base class for every error raised by this module."""


class NodeNotInGraphError(CostMatrixError):
    """Raised when a requested depot or customer node is absent from the graph."""


class EdgeNotInGraphError(CostMatrixError):
    """Raised when a street closure refers to an edge absent from the graph."""


class MissingEdgeWeightError(CostMatrixError):
    """Raised when graph edges lack the travel time or length attributes."""


class UnreachableNodeError(CostMatrixError):
    """Raised when the initial matrix build cannot connect every node pair."""


@dataclass(frozen=True)
class OriginSearchResult:
    """
    Outcome of a single-origin shortest-path search over the street network.

    Attributes
    ----------
    travel_time_to_node:
        Optimal travel time, in seconds, from the origin to every node settled
        by the search.
    distance_to_node:
        Distance, in meters, accumulated along the travel-time-optimal path to
        every node settled by the search. This is the length of the fastest
        route, not the length of the independently shortest route, because a
        vehicle following the fastest route travels exactly this distance.
    parent_edge_by_node:
        Shortest-path tree encoded as a mapping from each settled node to the
        directed edge used to reach it.
    """

    travel_time_to_node: dict[int, float]
    distance_to_node: dict[int, float]
    parent_edge_by_node: dict[int, EdgeIdentifier]


@dataclass(frozen=True)
class PatchReport:
    """
    Summary of an incremental repair of the cost matrices.

    Attributes
    ----------
    applied_edges:
        Edges whose state actually changed, after discarding closures already
        in force or reopenings of streets that were never closed.
    affected_origin_nodes:
        Origins whose matrix rows had to be recomputed.
    affected_pairs:
        Origin-destination pairs whose previous optimal path traversed one of
        the applied edges, and which were therefore invalidated. Empty when the
        caller asked to skip this report.
    recomputed_pair_count:
        Number of matrix entries recomputed, useful to quantify the saving
        against the N x N entries of a full rebuild.
    newly_unreachable_pairs:
        Pairs that became unreachable as a consequence of the change, for
        example a customer isolated behind a closed street.
    """

    applied_edges: tuple[EdgeIdentifier, ...]
    affected_origin_nodes: tuple[int, ...]
    affected_pairs: tuple[NodePair, ...]
    recomputed_pair_count: int
    newly_unreachable_pairs: tuple[NodePair, ...]


@dataclass
class CostMatrix:
    """
    Dense N x N travel time and distance matrices with dynamic patching.

    The matrices are built from a preprocessed street network and a list of
    nodes of interest for a workday. Index 0 is always the depot, and the
    remaining indices follow the order in which the customers were supplied,
    so the optimization engine can address them positionally.

    Route evaluation is intended to be performed either through the O(1)
    accessors (`travel_time_between`, `distance_between`) or, preferably, by
    indexing `travel_time_matrix` and `distance_matrix` directly with NumPy
    fancy indexing, as `evaluate_route` demonstrates.

    Attributes
    ----------
    street_network_graph:
        The preprocessed directed street network the matrices are derived from.
    node_ids:
        Nodes of interest in matrix order, with the depot first.
    matrix_index_by_node:
        Reverse lookup from graph node identifier to matrix index.
    travel_time_matrix:
        N x N matrix of optimal travel times in seconds. Unreachable pairs hold
        positive infinity and the diagonal holds zeros.
    distance_matrix:
        N x N matrix of distances in meters accumulated along the
        travel-time-optimal paths.
    closed_edges:
        Edges currently cut, excluded from every shortest-path search without
        mutating the underlying graph, which keeps closures reversible and
        avoids copying the network.
    pruned_tree_by_origin:
        For each origin node, the shortest-path tree restricted to the edges
        that its N - 1 optimal paths actually use, mapping every node on those
        paths to the edge used to reach it.
    origin_indices_by_edge:
        Inverted edge index mapping each traversed edge to the matrix rows that
        depend on it, which is what makes targeted invalidation possible.
    """

    street_network_graph: nx.MultiDiGraph
    node_ids: tuple[int, ...]
    matrix_index_by_node: dict[int, int] = field(default_factory=dict)
    travel_time_matrix: np.ndarray = field(default_factory=lambda: np.zeros((0, 0)))
    distance_matrix: np.ndarray = field(default_factory=lambda: np.zeros((0, 0)))
    closed_edges: set[EdgeIdentifier] = field(default_factory=set)
    pruned_tree_by_origin: dict[int, dict[int, EdgeIdentifier]] = field(default_factory=dict)
    origin_indices_by_edge: dict[EdgeIdentifier, set[int]] = field(default_factory=dict)

    @property
    def node_count(self) -> int:
        """Return N, the number of nodes of interest covered by the matrices."""
        return len(self.node_ids)

    @property
    def depot_node(self) -> int:
        """Return the graph node identifier acting as the depot."""
        return self.node_ids[DEPOT_MATRIX_INDEX]

    def index_of_node(self, node_id: int) -> int:
        """
        Return the matrix index of a node of interest.

        Parameters
        ----------
        node_id:
            Graph node identifier to translate.

        Returns
        -------
        int
            Position of the node inside the cost matrices.

        Raises
        ------
        NodeNotInGraphError
            If the node is not part of the matrix, since silently returning a
            wrong index would corrupt every subsequent route evaluation.
        """
        matrix_index = self.matrix_index_by_node.get(node_id)
        if matrix_index is None:
            message = f"Node {node_id} is not one of the {self.node_count} nodes of interest."
            raise NodeNotInGraphError(message)

        return matrix_index

    def travel_time_between(self, origin_node: int, destination_node: int) -> float:
        """
        Return the optimal travel time in seconds between two nodes of interest.

        Parameters
        ----------
        origin_node:
            Graph node identifier the trip starts at.
        destination_node:
            Graph node identifier the trip ends at.

        Returns
        -------
        float
            Travel time in seconds, or positive infinity if the destination is
            currently unreachable from the origin.
        """
        return float(
            self.travel_time_matrix[self.index_of_node(origin_node), self.index_of_node(destination_node)]
        )

    def distance_between(self, origin_node: int, destination_node: int) -> float:
        """
        Return the distance in meters travelled along the fastest path.

        Parameters
        ----------
        origin_node:
            Graph node identifier the trip starts at.
        destination_node:
            Graph node identifier the trip ends at.

        Returns
        -------
        float
            Distance in meters, or positive infinity if the destination is
            currently unreachable from the origin.
        """
        return float(
            self.distance_matrix[self.index_of_node(origin_node), self.index_of_node(destination_node)]
        )

    def evaluate_route(self, route_node_ids: Sequence[int]) -> tuple[float, float]:
        """
        Evaluate the total travel time and distance of an ordered route.

        The evaluation is fully vectorized: the consecutive legs of the route
        are gathered in a single NumPy indexing operation, which is the access
        pattern the metaheuristic is expected to use in its inner loop.

        Parameters
        ----------
        route_node_ids:
            Ordered sequence of nodes of interest visited by the vehicle. The
            caller is responsible for including the depot at both ends if the
            route is meant to be closed.

        Returns
        -------
        tuple[float, float]
            Total travel time in seconds and total distance in meters.
        """
        if len(route_node_ids) < 2:
            return 0.0, 0.0

        route_indices = np.fromiter(
            (self.index_of_node(node_id) for node_id in route_node_ids),
            dtype=np.intp,
            count=len(route_node_ids),
        )
        leg_origins = route_indices[:-1]
        leg_destinations = route_indices[1:]

        total_travel_time = float(self.travel_time_matrix[leg_origins, leg_destinations].sum())
        total_distance = float(self.distance_matrix[leg_origins, leg_destinations].sum())

        return total_travel_time, total_distance

    def path_between(self, origin_node: int, destination_node: int) -> tuple[int, ...]:
        """
        Reconstruct the node sequence of the optimal path between two nodes.

        The sequence is rebuilt from the stored shortest-path tree, which is
        what allows the simulation to advance a vehicle node by node while
        keeping memory consumption independent of N squared.

        Parameters
        ----------
        origin_node:
            Graph node identifier the path starts at.
        destination_node:
            Graph node identifier the path ends at.

        Returns
        -------
        tuple[int, ...]
            Nodes traversed, from origin to destination inclusive. A single
            element is returned when both arguments are the same node, and an
            empty tuple when the destination is currently unreachable.
        """
        self.index_of_node(origin_node)
        self.index_of_node(destination_node)
        if origin_node == destination_node:
            return (origin_node,)

        parent_edge_by_node = self.pruned_tree_by_origin[origin_node]
        reversed_path: list[int] = [destination_node]
        current_node = destination_node
        while current_node != origin_node:
            parent_edge = parent_edge_by_node.get(current_node)
            if parent_edge is None:
                return ()
            current_node = parent_edge[0]
            reversed_path.append(current_node)

        reversed_path.reverse()

        return tuple(reversed_path)

    def edge_path_between(
        self, origin_node: int, destination_node: int
    ) -> tuple[EdgeIdentifier, ...]:
        """
        Reconstruct the directed edges of the optimal path between two nodes.

        Edges are required rather than nodes whenever the caller needs the
        geometry or the attributes of the streets being driven, for example to
        stream the vehicle position to the client.

        Parameters
        ----------
        origin_node:
            Graph node identifier the path starts at.
        destination_node:
            Graph node identifier the path ends at.

        Returns
        -------
        tuple[EdgeIdentifier, ...]
            Edges traversed in travel order, or an empty tuple when the
            destination is unreachable or coincides with the origin.
        """
        self.index_of_node(origin_node)
        self.index_of_node(destination_node)
        if origin_node == destination_node:
            return ()

        parent_edge_by_node = self.pruned_tree_by_origin[origin_node]
        reversed_edges: list[EdgeIdentifier] = []
        current_node = destination_node
        while current_node != origin_node:
            parent_edge = parent_edge_by_node.get(current_node)
            if parent_edge is None:
                return ()
            reversed_edges.append(parent_edge)
            current_node = parent_edge[0]

        reversed_edges.reverse()

        return tuple(reversed_edges)

    def run_dijkstra_from_origin(self, origin_node: int) -> OriginSearchResult:
        """
        Run a targeted Dijkstra search from one origin over the street network.

        The search minimizes travel time while accumulating distance along the
        selected path, and terminates as soon as every node of interest has
        been settled, which avoids exploring the remainder of the city once all
        relevant destinations are final. Closed edges are skipped in place, so
        the graph itself is never mutated or copied. Parallel edges of a
        MultiDiGraph are examined individually, which means the fastest of two
        duplicated streets is chosen and identified unambiguously by its key.

        Parameters
        ----------
        origin_node:
            Graph node identifier the search expands from.

        Returns
        -------
        OriginSearchResult
            Travel times, distances and the shortest-path tree of the settled
            region of the graph.
        """
        travel_time_to_node: dict[int, float] = {origin_node: 0.0}
        distance_to_node: dict[int, float] = {origin_node: 0.0}
        parent_edge_by_node: dict[int, EdgeIdentifier] = {}
        settled_nodes: set[int] = set()
        pending_targets: set[int] = set(self.node_ids) - {origin_node}
        priority_queue: list[tuple[float, int]] = [(0.0, origin_node)]

        while priority_queue and pending_targets:
            current_travel_time, current_node = heapq.heappop(priority_queue)
            if current_node in settled_nodes:
                continue
            settled_nodes.add(current_node)
            pending_targets.discard(current_node)
            current_distance = distance_to_node[current_node]

            for neighbour_node, parallel_edges in self.street_network_graph[current_node].items():
                if neighbour_node in settled_nodes:
                    continue
                for edge_key, edge_attributes in parallel_edges.items():
                    if (current_node, neighbour_node, edge_key) in self.closed_edges:
                        continue
                    candidate_travel_time = current_travel_time + edge_attributes[TRAVEL_TIME_ATTRIBUTE]
                    if candidate_travel_time < travel_time_to_node.get(neighbour_node, math.inf):
                        travel_time_to_node[neighbour_node] = candidate_travel_time
                        distance_to_node[neighbour_node] = (
                            current_distance + edge_attributes[DISTANCE_ATTRIBUTE]
                        )
                        parent_edge_by_node[neighbour_node] = (current_node, neighbour_node, edge_key)
                        heapq.heappush(priority_queue, (candidate_travel_time, neighbour_node))

        return OriginSearchResult(
            travel_time_to_node=travel_time_to_node,
            distance_to_node=distance_to_node,
            parent_edge_by_node=parent_edge_by_node,
        )

    def rebuild_rows(self, origin_indices: Iterable[int]) -> int:
        """
        Recompute the given matrix rows, their trees and their index entries.

        Each row is repaired by a single search from its origin, because
        Dijkstra reaches every destination of the row in one pass. While the
        results are written, the shortest-path tree is pruned to the edges used
        by the N - 1 optimal paths of the row, and those edges are registered
        in the inverted index. Pruning matters twice: it bounds memory by the
        size of the relevant subtree, and it keeps the index precise, so that
        closing a street that only serves irrelevant detours never triggers a
        recomputation.

        Parameters
        ----------
        origin_indices:
            Matrix rows to repair.

        Returns
        -------
        int
            Number of matrix entries recomputed, excluding the diagonal.
        """
        origin_indices_to_repair = list(origin_indices)
        recomputed_pair_count = 0

        if len(set(origin_indices_to_repair)) == self.node_count:
            # Discarding the auxiliary structures wholesale is cheaper than purging
            # them row by row, which guarantees that a patch touching every row
            # never costs more than a full rebuild.
            self.pruned_tree_by_origin.clear()
            self.origin_indices_by_edge.clear()
        else:
            for origin_index in origin_indices_to_repair:
                self.discard_origin_from_edge_index(origin_index)

        for origin_index in origin_indices_to_repair:
            origin_node = self.node_ids[origin_index]
            search_result = self.run_dijkstra_from_origin(origin_node)
            pruned_tree: dict[int, EdgeIdentifier] = {}

            for destination_index, destination_node in enumerate(self.node_ids):
                if destination_index == origin_index:
                    self.travel_time_matrix[origin_index, destination_index] = 0.0
                    self.distance_matrix[origin_index, destination_index] = 0.0
                    continue

                travel_time = search_result.travel_time_to_node.get(destination_node, math.inf)
                self.travel_time_matrix[origin_index, destination_index] = travel_time
                self.distance_matrix[origin_index, destination_index] = (
                    search_result.distance_to_node.get(destination_node, math.inf)
                )
                recomputed_pair_count += 1
                if math.isinf(travel_time):
                    continue

                # Walk the tree backwards to register the edges this path depends on.
                # Reaching an already registered node means the remaining prefix
                # towards the origin is shared with a previous destination.
                current_node = destination_node
                while current_node != origin_node and current_node not in pruned_tree:
                    parent_edge = search_result.parent_edge_by_node[current_node]
                    pruned_tree[current_node] = parent_edge
                    self.origin_indices_by_edge.setdefault(parent_edge, set()).add(origin_index)
                    current_node = parent_edge[0]

            self.pruned_tree_by_origin[origin_node] = pruned_tree

        return recomputed_pair_count

    def rebuild_all_rows(self) -> int:
        """
        Recompute the complete N x N matrices from scratch.

        Returns
        -------
        int
            Number of matrix entries recomputed, excluding the diagonal.
        """
        self.travel_time_matrix = np.zeros((self.node_count, self.node_count), dtype=np.float64)
        self.distance_matrix = np.zeros((self.node_count, self.node_count), dtype=np.float64)

        return self.rebuild_rows(range(self.node_count))

    def discard_origin_from_edge_index(self, origin_index: int) -> None:
        """
        Remove every inverted index entry contributed by one matrix row.

        This must run before a row is recomputed, otherwise the index would
        keep pointing at edges belonging to superseded paths and would trigger
        spurious invalidations later on.

        Parameters
        ----------
        origin_index:
            Matrix row whose contributions are dropped from the index.
        """
        origin_node = self.node_ids[origin_index]
        previous_tree = self.pruned_tree_by_origin.pop(origin_node, {})
        for parent_edge in set(previous_tree.values()):
            dependent_origins = self.origin_indices_by_edge.get(parent_edge)
            if dependent_origins is None:
                continue
            dependent_origins.discard(origin_index)
            if not dependent_origins:
                del self.origin_indices_by_edge[parent_edge]

    def find_affected_origin_indices(self, edges: Iterable[EdgeIdentifier]) -> set[int]:
        """
        Return the matrix rows whose optimal paths traverse any of the edges.

        This is the O(1) per edge lookup that the inverted index exists for:
        the answer is read from the index instead of being searched for in the
        graph.

        Parameters
        ----------
        edges:
            Directed edges under consideration, typically about to be closed.

        Returns
        -------
        set[int]
            Matrix rows that depend on at least one of the given edges.
        """
        affected_origin_indices: set[int] = set()
        for edge in edges:
            affected_origin_indices.update(self.origin_indices_by_edge.get(edge, set()))

        return affected_origin_indices

    def find_affected_pairs(self, edges: Iterable[EdgeIdentifier]) -> tuple[NodePair, ...]:
        """
        Return the exact origin-destination pairs that traverse any of the edges.

        The inverted index narrows the search down to the rows that can
        possibly be affected, and the stored shortest-path trees are then
        walked to determine precisely which destinations of those rows depend
        on the given edges. Deriving the pairs on demand keeps the permanent
        memory footprint proportional to the shortest-path trees rather than to
        the N squared paths.

        Parameters
        ----------
        edges:
            Directed edges under consideration.

        Returns
        -------
        tuple[NodePair, ...]
            Pairs of graph node identifiers whose current optimal path uses at
            least one of the given edges.
        """
        edges_under_consideration = set(edges)
        affected_pairs: list[NodePair] = []

        for origin_index in sorted(self.find_affected_origin_indices(edges_under_consideration)):
            origin_node = self.node_ids[origin_index]
            for destination_node in self.node_ids:
                if destination_node == origin_node:
                    continue
                path_edges = self.edge_path_between(origin_node, destination_node)
                if any(path_edge in edges_under_consideration for path_edge in path_edges):
                    affected_pairs.append((origin_node, destination_node))

        return tuple(affected_pairs)

    def validate_edges_exist(self, edges: Iterable[EdgeIdentifier]) -> None:
        """
        Verify that every given edge exists in the street network.

        Parameters
        ----------
        edges:
            Directed edges to validate.

        Raises
        ------
        EdgeNotInGraphError
            If any edge is absent from the graph, which would otherwise make a
            closure silently ineffective and leave the simulation believing a
            street had been cut.
        """
        unknown_edges = [
            edge
            for edge in edges
            if not self.street_network_graph.has_edge(edge[0], edge[1], edge[2])
        ]
        if unknown_edges:
            message = f"The following edges are not part of the street network: {unknown_edges}."
            raise EdgeNotInGraphError(message)

    def apply_street_closures(
        self, edges: Iterable[EdgeIdentifier], report_affected_pairs: bool = True
    ) -> PatchReport:
        """
        Close streets and repair only the invalidated part of the matrices.

        The affected rows are located through the inverted edge index and
        recomputed with the closed edges excluded from the search. Rows that do
        not depend on any closed edge are provably still optimal, because
        removing edges can only increase the cost of the alternatives, so they
        are left untouched.

        A street that is bidirectional in reality is represented by two
        opposite directed edges, and both must be supplied to cut it in both
        directions. `find_street_edges` builds that set from a pair of nodes.

        Parameters
        ----------
        edges:
            Directed edges to close. Edges already closed are ignored.
        report_affected_pairs:
            Whether to enumerate the exact invalidated pairs in the report.
            Determining them requires walking the shortest-path trees of the
            affected rows, so the simulation may switch this off in its hot
            path, where only the repaired matrices matter.

        Returns
        -------
        PatchReport
            Description of what was invalidated and recomputed, including any
            pair that became unreachable.

        Raises
        ------
        EdgeNotInGraphError
            If any edge does not exist in the street network.
        """
        requested_edges = set(edges)
        self.validate_edges_exist(requested_edges)
        edges_to_close = requested_edges - self.closed_edges
        if not edges_to_close:
            return PatchReport((), (), (), 0, ())

        affected_origin_indices = sorted(self.find_affected_origin_indices(edges_to_close))
        affected_pairs = self.find_affected_pairs(edges_to_close) if report_affected_pairs else ()
        travel_times_before_patch = self.travel_time_matrix[affected_origin_indices, :].copy()

        self.closed_edges.update(edges_to_close)
        recomputed_pair_count = self.rebuild_rows(affected_origin_indices)

        return PatchReport(
            applied_edges=tuple(sorted(edges_to_close)),
            affected_origin_nodes=tuple(self.node_ids[index] for index in affected_origin_indices),
            affected_pairs=affected_pairs,
            recomputed_pair_count=recomputed_pair_count,
            newly_unreachable_pairs=self.collect_newly_unreachable_pairs(
                affected_origin_indices, travel_times_before_patch
            ),
        )

    def reopen_streets(self, edges: Iterable[EdgeIdentifier]) -> PatchReport:
        """
        Reopen previously closed streets and rebuild the matrices completely.

        Unlike a closure, reopening a street lowers costs, and a cheaper edge
        can improve the optimal path of any pair, including pairs that never
        traversed the reopened street before. The inverted index cannot bound
        that set, so the only sound response is a full recomputation. This
        asymmetry is intentional and documented rather than hidden behind an
        unsafe incremental shortcut.

        Parameters
        ----------
        edges:
            Directed edges to reopen. Edges that are not closed are ignored.

        Returns
        -------
        PatchReport
            Description of the rebuild. Every row is reported as affected.
        """
        requested_edges = set(edges)
        self.validate_edges_exist(requested_edges)
        edges_to_reopen = requested_edges & self.closed_edges
        if not edges_to_reopen:
            return PatchReport((), (), (), 0, ())

        affected_origin_indices = list(range(self.node_count))
        travel_times_before_patch = self.travel_time_matrix.copy()

        self.closed_edges.difference_update(edges_to_reopen)
        recomputed_pair_count = self.rebuild_all_rows()

        return PatchReport(
            applied_edges=tuple(sorted(edges_to_reopen)),
            affected_origin_nodes=tuple(self.node_ids),
            affected_pairs=(),
            recomputed_pair_count=recomputed_pair_count,
            newly_unreachable_pairs=self.collect_newly_unreachable_pairs(
                affected_origin_indices, travel_times_before_patch
            ),
        )

    def collect_newly_unreachable_pairs(
        self, origin_indices: Sequence[int], travel_times_before_patch: np.ndarray
    ) -> tuple[NodePair, ...]:
        """
        Identify pairs that lost reachability during the last repair.

        Parameters
        ----------
        origin_indices:
            Matrix rows that were recomputed, in the same order as the rows of
            `travel_times_before_patch`.
        travel_times_before_patch:
            Copy of those rows taken before the repair.

        Returns
        -------
        tuple[NodePair, ...]
            Pairs of graph node identifiers that were reachable before the
            repair and are not reachable any more.
        """
        if not len(origin_indices):
            return ()

        travel_times_after_patch = self.travel_time_matrix[list(origin_indices), :]
        lost_reachability = np.isinf(travel_times_after_patch) & ~np.isinf(travel_times_before_patch)
        row_positions, column_positions = np.nonzero(lost_reachability)

        return tuple(
            (self.node_ids[origin_indices[row_position]], self.node_ids[column_position])
            for row_position, column_position in zip(row_positions, column_positions)
        )

    def find_unreachable_pairs(self) -> tuple[NodePair, ...]:
        """
        Return every pair of nodes of interest currently not connected.

        Returns
        -------
        tuple[NodePair, ...]
            Pairs of graph node identifiers whose travel time is infinite.
        """
        row_positions, column_positions = np.nonzero(np.isinf(self.travel_time_matrix))

        return tuple(
            (self.node_ids[row_position], self.node_ids[column_position])
            for row_position, column_position in zip(row_positions, column_positions)
        )

    def describe(self) -> str:
        """
        Return a formal, human-readable summary of the current matrix state.

        Returns
        -------
        str
            Multi-line report with the dimensions, the cost statistics and the
            memory relevant sizes of the auxiliary structures.
        """
        finite_travel_times = self.travel_time_matrix[np.isfinite(self.travel_time_matrix)]
        mean_travel_time = float(finite_travel_times.mean()) if finite_travel_times.size else math.nan
        maximum_travel_time = float(finite_travel_times.max()) if finite_travel_times.size else math.nan
        index_entry_count = sum(len(origins) for origins in self.origin_indices_by_edge.values())
        tree_entry_count = sum(len(tree) for tree in self.pruned_tree_by_origin.values())

        return (
            f"Nodes of interest (N): {self.node_count}\n"
            f"Depot node: {self.depot_node}\n"
            f"Matrix entries: {self.node_count ** 2}\n"
            f"Closed edges: {len(self.closed_edges)}\n"
            f"Unreachable pairs: {len(self.find_unreachable_pairs())}\n"
            f"Mean travel time (s): {mean_travel_time:.2f}\n"
            f"Maximum travel time (s): {maximum_travel_time:.2f}\n"
            f"Indexed edges: {len(self.origin_indices_by_edge)}\n"
            f"Inverted index entries: {index_entry_count}\n"
            f"Shortest-path tree entries: {tree_entry_count}"
        )


def validate_edge_weights(street_network_graph: nx.MultiDiGraph) -> None:
    """
    Verify that every edge carries the attributes the search relies on.

    Parameters
    ----------
    street_network_graph:
        Graph to validate.

    Raises
    ------
    MissingEdgeWeightError
        If any edge lacks a usable travel time or length, since Dijkstra would
        otherwise fail deep inside the search with an opaque error.
    """
    for origin_node, destination_node, edge_attributes in street_network_graph.edges(data=True):
        for attribute_name in (TRAVEL_TIME_ATTRIBUTE, DISTANCE_ATTRIBUTE):
            attribute_value = edge_attributes.get(attribute_name)
            if attribute_value is None or not math.isfinite(float(attribute_value)):
                message = (
                    f"Edge ({origin_node}, {destination_node}) has no usable "
                    f"'{attribute_name}' attribute. Run the travel time computation of the "
                    "extractor module before building a cost matrix."
                )
                raise MissingEdgeWeightError(message)


def validate_nodes_of_interest(
    street_network_graph: nx.MultiDiGraph, depot_node: int, customer_nodes: Sequence[int]
) -> tuple[int, ...]:
    """
    Validate the workday nodes and return them in matrix order.

    Parameters
    ----------
    street_network_graph:
        Graph the nodes must belong to.
    depot_node:
        Node the vehicles depart from and return to.
    customer_nodes:
        Nodes to be served during the workday.

    Returns
    -------
    tuple[int, ...]
        The depot followed by the customers, in the supplied order.

    Raises
    ------
    NodeNotInGraphError
        If any node is absent from the graph.
    ValueError
        If the depot is repeated among the customers, if the customers contain
        duplicates, or if there is no customer at all, because any of those
        would produce a degenerate or ambiguous matrix.
    """
    nodes_of_interest = (depot_node, *customer_nodes)
    missing_nodes = [
        node_id for node_id in nodes_of_interest if not street_network_graph.has_node(node_id)
    ]
    if missing_nodes:
        message = f"The following nodes are not part of the street network: {missing_nodes}."
        raise NodeNotInGraphError(message)

    if len(customer_nodes) == 0:
        message = "At least one customer node is required to build a cost matrix."
        raise ValueError(message)

    if len(set(nodes_of_interest)) != len(nodes_of_interest):
        message = (
            "The nodes of interest contain duplicates. Every customer must be distinct and "
            "different from the depot, otherwise the matrix would hold ambiguous rows."
        )
        raise ValueError(message)

    return nodes_of_interest


def build_cost_matrix(
    street_network_graph: nx.MultiDiGraph, depot_node: int, customer_nodes: Sequence[int]
) -> CostMatrix:
    """
    Build the N x N cost matrices for one workday.

    The graph is expected to be the preprocessed network produced by the
    extractor module: strongly connected and carrying travel time and length
    attributes on every edge.

    Parameters
    ----------
    street_network_graph:
        Preprocessed directed street network.
    depot_node:
        Node the vehicles depart from and return to, placed at index 0.
    customer_nodes:
        Nodes to be served during the workday, placed at the following indices
        in the supplied order.

    Returns
    -------
    CostMatrix
        Fully populated matrices together with the auxiliary structures needed
        for dynamic patching.

    Raises
    ------
    NodeNotInGraphError
        If the depot or any customer is absent from the graph.
    MissingEdgeWeightError
        If the graph edges lack travel time or length attributes.
    UnreachableNodeError
        If some pair cannot be connected. On a strongly connected network this
        can only happen if the caller supplied a graph that was never
        contracted to its largest strongly connected component, so failing
        loudly here prevents the optimizer from silently working with infinite
        costs.
    """
    nodes_of_interest = validate_nodes_of_interest(street_network_graph, depot_node, customer_nodes)
    validate_edge_weights(street_network_graph)

    cost_matrix = CostMatrix(
        street_network_graph=street_network_graph,
        node_ids=nodes_of_interest,
        matrix_index_by_node={
            node_id: matrix_index for matrix_index, node_id in enumerate(nodes_of_interest)
        },
    )
    cost_matrix.rebuild_all_rows()

    unreachable_pairs = cost_matrix.find_unreachable_pairs()
    if unreachable_pairs:
        message = (
            f"{len(unreachable_pairs)} node pairs are mutually unreachable, for example "
            f"{unreachable_pairs[0]}. The street network must be strongly connected before "
            "building a cost matrix."
        )
        raise UnreachableNodeError(message)

    return cost_matrix


def find_street_edges(
    street_network_graph: nx.MultiDiGraph, first_node: int, second_node: int
) -> tuple[EdgeIdentifier, ...]:
    """
    Return every directed edge connecting two adjacent nodes, in both senses.

    A street closure is a physical event affecting a stretch of road, whereas
    the graph models each sense of circulation as a separate directed edge, and
    duplicated carriageways as parallel edges. This helper collects all of them
    so that the simulation can cut a street completely without reasoning about
    the graph representation.

    Parameters
    ----------
    street_network_graph:
        Graph to inspect.
    first_node:
        One end of the street stretch.
    second_node:
        The other end of the street stretch.

    Returns
    -------
    tuple[EdgeIdentifier, ...]
        Directed edges between the two nodes, in both directions. Empty if the
        nodes are not adjacent.
    """
    street_edges: list[EdgeIdentifier] = []
    for origin_node, destination_node in ((first_node, second_node), (second_node, first_node)):
        if street_network_graph.has_edge(origin_node, destination_node):
            for edge_key in street_network_graph[origin_node][destination_node]:
                street_edges.append((origin_node, destination_node, edge_key))

    return tuple(street_edges)


def select_demonstration_nodes(
    street_network_graph: nx.MultiDiGraph, customer_count: int, random_seed: int = 42
) -> tuple[int, list[int]]:
    """
    Pick a reproducible depot and customer set for the demonstration run.

    Parameters
    ----------
    street_network_graph:
        Graph to sample nodes from.
    customer_count:
        Number of customers to draw.
    random_seed:
        Seed guaranteeing that consecutive executions produce the same
        scenario, which is required to compare timings between runs.

    Returns
    -------
    tuple[int, list[int]]
        The depot node and the list of customer nodes.
    """
    sortable_node_ids = sorted(street_network_graph.nodes)
    sampled_node_ids = random.Random(random_seed).sample(sortable_node_ids, customer_count + 1)

    return sampled_node_ids[0], sampled_node_ids[1:]


if __name__ == "__main__":
    # The extractor is only needed by this demonstration entry point, so it is
    # imported here to keep the module free of the OSMnx and Folium dependencies
    # when it is loaded by the simulation backend. The fallback covers direct
    # script execution, where no parent package is defined.
    try:
        from .extractor import PROCESSED_GRAPH_PATH, load_processed_graph
    except ImportError:
        from extractor import PROCESSED_GRAPH_PATH, load_processed_graph

    DEMONSTRATION_CUSTOMER_COUNT: int = 40

    print(f"Loading the preprocessed street network from {PROCESSED_GRAPH_PATH}.")
    malaga_street_network = load_processed_graph()
    print(
        f"Network loaded: {malaga_street_network.number_of_nodes()} nodes, "
        f"{malaga_street_network.number_of_edges()} edges."
    )

    demonstration_depot, demonstration_customers = select_demonstration_nodes(
        malaga_street_network, DEMONSTRATION_CUSTOMER_COUNT
    )

    print(
        f"Building the cost matrix for 1 depot and {DEMONSTRATION_CUSTOMER_COUNT} customers."
    )
    build_started_at = time.perf_counter()
    malaga_cost_matrix = build_cost_matrix(
        malaga_street_network, demonstration_depot, demonstration_customers
    )
    build_elapsed_seconds = time.perf_counter() - build_started_at
    print(f"Full build completed in {build_elapsed_seconds:.3f} s.")
    print(malaga_cost_matrix.describe())

    # Demonstrate an O(1) route evaluation over the depot and the first customers.
    demonstration_route = [
        demonstration_depot,
        *demonstration_customers[:5],
        demonstration_depot,
    ]
    route_travel_time, route_distance = malaga_cost_matrix.evaluate_route(demonstration_route)
    print(
        f"Sample route over {len(demonstration_route)} stops: "
        f"{route_travel_time:.2f} s, {route_distance:.2f} m."
    )

    # Measure the incremental repair on two street closures: an ordinary one and
    # the worst case, the street the matrix depends on most heavily. The second
    # case shows that the index degrades gracefully towards a full rebuild
    # instead of ever costing more than one.
    ordinary_street_edge = random.Random(11).choice(
        sorted(malaga_cost_matrix.origin_indices_by_edge)
    )
    busiest_street_edge = max(
        malaga_cost_matrix.origin_indices_by_edge,
        key=lambda edge: len(malaga_cost_matrix.origin_indices_by_edge[edge]),
    )
    total_pair_count = malaga_cost_matrix.node_count * (malaga_cost_matrix.node_count - 1)

    for scenario_description, street_edge in (
        ("an ordinary street", ordinary_street_edge),
        ("the most heavily used street", busiest_street_edge),
    ):
        street_edges_to_close = find_street_edges(
            malaga_street_network, street_edge[0], street_edge[1]
        )
        print(
            f"\nClosing {scenario_description}, stretch {street_edge[0]} - {street_edge[1]} "
            f"({len(street_edges_to_close)} directed edges)."
        )

        patch_started_at = time.perf_counter()
        closure_report = malaga_cost_matrix.apply_street_closures(street_edges_to_close)
        patch_elapsed_seconds = time.perf_counter() - patch_started_at

        print(
            f"Patch completed in {patch_elapsed_seconds:.3f} s, against "
            f"{build_elapsed_seconds:.3f} s for a full rebuild "
            f"({build_elapsed_seconds / patch_elapsed_seconds:.1f}x)."
        )
        print(
            f"Invalidated pairs: {len(closure_report.affected_pairs)} of {total_pair_count}. "
            f"Recomputed rows: {len(closure_report.affected_origin_nodes)} of "
            f"{malaga_cost_matrix.node_count}. "
            f"Newly unreachable pairs: {len(closure_report.newly_unreachable_pairs)}."
        )

        # Restore the pristine state so both scenarios start from equal conditions.
        malaga_cost_matrix.reopen_streets(street_edges_to_close)
