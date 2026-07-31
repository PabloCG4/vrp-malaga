"""Pydantic v2 schemas (DTOs) for the read-only street network topology endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field


class NetworkNode(BaseModel):
    """One Malaga street network node, positioned for map rendering."""

    node_id: int = Field(..., description="Malaga street network node identifier.")
    latitude: float = Field(..., description="Node latitude, in degrees (EPSG:4326).")
    longitude: float = Field(..., description="Node longitude, in degrees (EPSG:4326).")


class NetworkGraph(BaseModel):
    """
    Lightweight, cacheable snapshot of the whole Malaga street network.

    Consumed exclusively by the frontend map: `nodes` lets it place the depot
    and every order marker at its true street coordinate, and `edges` (an
    undirected, de-duplicated adjacency list) lets it validate, before ever
    calling `POST .../events/traffic`, that two nodes the dispatcher selected
    on the map actually share a street, mirroring the same adjacency check
    `topology.matrix.find_street_edges` performs server-side.
    """

    depot_node_id: int = Field(..., description="Fixed depot node id shared by every workday plan.")
    nodes: list[NetworkNode] = Field(..., description="Every node of the street network.")
    edges: list[tuple[int, int]] = Field(
        ..., description="Undirected, de-duplicated adjacency pairs between directly connected nodes."
    )
