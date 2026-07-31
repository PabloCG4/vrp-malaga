"""FastAPI router exposing the read-only street network topology endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from ..schemas.network import NetworkGraph
from ..services.network_service import get_network_graph_payload

router = APIRouter(prefix="/api/v1/network", tags=["network"])


@router.get("", response_model=NetworkGraph)
async def get_network() -> NetworkGraph:
    """
    Return the full Malaga street network's nodes, adjacency list, and depot.

    Workday-agnostic (the street network is shared by every plan) and safe to
    fetch once and cache indefinitely on the client: this endpoint is what
    lets the dashboard map plot every stop at its true coordinate and
    validate a dispatcher's map-based street-closure selection before
    submitting it to `POST /api/v1/workdays/{id}/events/traffic`.
    """
    return get_network_graph_payload()
