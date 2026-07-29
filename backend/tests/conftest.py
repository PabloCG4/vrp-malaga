"""
Shared pytest fixtures for the backend test suite.

The topology modules (`extractor` and `matrix`) live under `backend/src/topology`
without a package hierarchy above them, and support being imported either as a
package (`from .extractor import ...`) or as standalone scripts (`from extractor
import ...`), as documented in `matrix.py`. This conftest adds that directory to
`sys.path` once, at collection time, so every test module in this suite can use
the same plain `import matrix` / `import extractor` statements regardless of how
pytest is invoked or from which working directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import networkx as nx
import pytest

TOPOLOGY_SOURCE_DIRECTORY: Path = Path(__file__).resolve().parents[1] / "src" / "topology"
if str(TOPOLOGY_SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TOPOLOGY_SOURCE_DIRECTORY))

from extractor import PROCESSED_GRAPH_PATH, load_processed_graph  # noqa: E402


@pytest.fixture(scope="session")
def malaga_street_network() -> nx.MultiDiGraph:
    """
    Load the real, preprocessed Malaga street network from disk.

    The suite intentionally exercises the actual serialized graph produced by
    the extractor module rather than a synthetic stub, so that the invariants
    under test are validated against the same topology, one-way streets and
    travel times the production routing engine will operate on. The graph is
    loaded once per test session, since it is treated as an immutable fixture:
    no test in this suite is allowed to mutate it directly.

    Returns
    -------
    networkx.MultiDiGraph
        The strongly connected, travel-time-annotated street network.
    """
    if not PROCESSED_GRAPH_PATH.is_file():
        pytest.skip(
            f"Processed graph not found at {PROCESSED_GRAPH_PATH}. Run "
            "'python backend/src/topology/extractor.py' to generate it before running this suite."
        )

    return load_processed_graph()
