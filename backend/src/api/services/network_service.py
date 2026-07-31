"""
Builds the lightweight, cacheable street-network payload the frontend's map
consumes for plotting nodes and validating street-closure adjacency.

Kept separate from `network_provider.py` (which owns only the raw graph and
depot node, with no dependency on the Pydantic schema layer) so that module
stays a dependency-free, purely-graph-loading concern. The transformation to
a JSON-serializable payload lives here instead, cached exactly once per
process since the underlying street network never changes at runtime.
"""

from __future__ import annotations

import threading

from . import network_provider
from ..schemas.network import NetworkGraph, NetworkNode

_lock = threading.Lock()
_cached_payload: NetworkGraph | None = None


def get_network_graph_payload() -> NetworkGraph:
    """Return the process-wide network graph payload, building it on first use."""
    global _cached_payload
    if _cached_payload is None:
        with _lock:
            if _cached_payload is None:
                _cached_payload = _build_network_graph_payload()
    return _cached_payload


def _build_network_graph_payload() -> NetworkGraph:
    graph = network_provider.get_street_network_graph()
    depot_node = network_provider.get_depot_node()

    nodes = [
        NetworkNode(node_id=node_id, latitude=float(data["y"]), longitude=float(data["x"]))
        for node_id, data in graph.nodes(data=True)
    ]

    # Collapse the directed (and, for duplicated carriageways, parallel) edges
    # of the `MultiDiGraph` into a de-duplicated, undirected adjacency list:
    # the frontend only ever needs to know whether two nodes share a street,
    # never which specific directed lane, mirroring the aggregate closure
    # `find_street_edges` performs on the backend for an entire street.
    edge_pairs: set[tuple[int, int]] = set()
    for first_node, second_node in graph.edges(keys=False):
        if first_node == second_node:
            continue
        edge_pairs.add((first_node, second_node) if first_node < second_node else (second_node, first_node))

    return NetworkGraph(depot_node_id=depot_node, nodes=nodes, edges=sorted(edge_pairs))


def reset_network_graph_cache_for_testing() -> None:
    """Clear the cached payload; intended for use by the test suite only."""
    global _cached_payload
    with _lock:
        _cached_payload = None
