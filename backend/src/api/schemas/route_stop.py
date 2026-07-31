"""Pydantic v2 schemas (DTOs) for the `RouteStop` entity."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ...db.enums import RouteStopType


class RouteStopBase(BaseModel):
    """Fields common to every representation of a route stop."""

    vehicle_id: int = Field(..., description="Identifier of the vehicle driving this stop.")
    order_id: int | None = Field(
        default=None, description="Identifier of the order served here, or None for a depot start/end leg."
    )
    sequence_order: int = Field(..., ge=0, description="Zero-based position of this stop within the vehicle's route.")
    stop_type: RouteStopType = Field(..., description="Role this stop plays in the vehicle's visiting sequence.")
    node_id: int = Field(..., description="Malaga street network node identifier of this stop.")
    planned_arrival_seconds: int = Field(..., ge=0, description="Planned workday clock reading at arrival, in seconds.")
    actual_arrival_seconds: int | None = Field(
        default=None, ge=0, description="Real arrival time recorded during live simulation, in seconds."
    )
    departure_seconds: int | None = Field(default=None, ge=0, description="Departure time from this stop, in seconds.")


class RouteStopCreate(RouteStopBase):
    """Payload required to create a new planned route stop within a workday plan."""

    workday_plan_id: int = Field(..., description="Identifier of the workday plan this stop belongs to.")


class RouteStopTelemetryUpdate(BaseModel):
    """Payload for recording the real-time arrival/departure of a planned stop."""

    actual_arrival_seconds: int = Field(..., ge=0, description="Real arrival time observed, in seconds.")
    departure_seconds: int | None = Field(default=None, ge=0, description="Real departure time observed, in seconds.")


class RouteStopRead(RouteStopBase):
    """Full representation of a route stop as persisted in the database."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    workday_plan_id: int
