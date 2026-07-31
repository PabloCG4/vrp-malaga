"""Pydantic v2 schemas (DTOs) for street-following route geometry endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RouteLegGeometry(BaseModel):
    """
    One consecutive pair of route stops, expanded into the true street-network
    polyline the vehicle must drive between them.
    """

    vehicle_id: int = Field(..., description="Identifier of the vehicle driving this leg.")
    from_sequence_order: int = Field(..., ge=0, description="Sequence order of the stop this leg departs from.")
    to_sequence_order: int = Field(..., ge=0, description="Sequence order of the stop this leg arrives at.")
    from_node_id: int = Field(..., description="Street network node id of the departure stop.")
    to_node_id: int = Field(..., description="Street network node id of the arrival stop.")
    coordinates: list[tuple[float, float]] = Field(
        ...,
        description=(
            "Ordered polyline vertices as (latitude, longitude) pairs in Leaflet order, "
            "including both endpoints and every intermediate intersection along the "
            "CostMatrix shortest path."
        ),
    )


class WorkdayRouteGeometry(BaseModel):
    """Every street-following leg of every vehicle assigned to one workday plan."""

    workday_plan_id: int = Field(..., description="Identifier of the workday plan these legs belong to.")
    legs: list[RouteLegGeometry] = Field(
        default_factory=list,
        description="One entry per consecutive pair of route stops; empty when the plan has no routes yet.",
    )
