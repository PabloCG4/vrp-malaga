"""
Process-wide, lazily-cached access to the preprocessed Malaga street network
and its fixed depot node.

Deserializing the street network graph is comparatively expensive (a
multi-thousand-node pickle load), so it must happen once per process rather
than once per HTTP request; both `get_street_network_graph` and
`get_depot_node` therefore memoize their result in a module-level global,
mirroring the lazy-singleton pattern `backend.src.db.session` already uses
for the database engine.

The depot node is deliberately derived with the exact same
`select_demonstration_nodes` seed and candidate pool size that
`backend.src.scripts.seed_db` uses to build its demonstration scenario. This
regional logistics operation has a single physical depot, so every workday
plan, whether it was seeded by that script or created some other way, must
optimize against that same depot location; reusing the script's constants
here (rather than re-declaring them) also means the two can never silently
drift apart.
"""

from __future__ import annotations

import threading

import networkx as nx

from ...scripts.seed_db import RANDOM_SEED, RESERVED_URGENT_NODE_COUNT, STANDARD_CUSTOMER_POOL_SIZE
from ...topology.extractor import load_processed_graph
from ...topology.matrix import select_demonstration_nodes

_graph_lock = threading.Lock()
_street_network_graph: nx.MultiDiGraph | None = None
_depot_node: int | None = None


def get_street_network_graph() -> nx.MultiDiGraph:
    """Return the process-wide Malaga street network graph, loading it on first use."""
    global _street_network_graph
    if _street_network_graph is None:
        with _graph_lock:
            if _street_network_graph is None:
                _street_network_graph = load_processed_graph()
    return _street_network_graph


def get_depot_node() -> int:
    """
    Return the fixed depot node id shared by every workday of this deployment.

    Computed once, deterministically, from the same random seed and candidate
    pool size `seed_db.py` uses, so a plan seeded by that script and later
    re-optimized through this API always departs from and returns to the
    same physical depot.

    The street graph is loaded *before* acquiring `_graph_lock`. Calling
    `get_street_network_graph()` while holding that non-reentrant lock
    deadlocks on a cold cache (the geometry endpoint used to hang forever
    and the map silently fell back to Euclidean polylines).
    """
    global _depot_node
    if _depot_node is not None:
        return _depot_node
    graph = get_street_network_graph()
    with _graph_lock:
        if _depot_node is None:
            depot_node, _candidate_nodes = select_demonstration_nodes(
                graph,
                STANDARD_CUSTOMER_POOL_SIZE + RESERVED_URGENT_NODE_COUNT,
                random_seed=RANDOM_SEED,
            )
            _depot_node = depot_node
        return _depot_node


def reset_network_cache_for_testing() -> None:
    """Clear the cached graph and depot node; intended for use by the test suite only."""
    global _street_network_graph, _depot_node
    with _graph_lock:
        _street_network_graph = None
        _depot_node = None
