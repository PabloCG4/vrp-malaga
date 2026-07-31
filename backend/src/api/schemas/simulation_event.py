"""Pydantic v2 schemas (DTOs) for the `SimulationEvent` audit log entity."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ...db.enums import SimulationEventType


class SimulationEventBase(BaseModel):
    """Fields common to every representation of a simulation event."""

    event_type: SimulationEventType = Field(..., description="Kind of dynamic disruption this event recorded.")
    trigger_minute: int = Field(..., ge=0, description="Simulated minute, since workday start, this event fired at.")
    payload_json: dict[str, Any] = Field(
        default_factory=dict, description="Event-specific metadata (for example, closed nodes or urgent order data)."
    )


class SimulationEventCreate(SimulationEventBase):
    """Payload required to log a new simulation event within a workday plan."""

    workday_plan_id: int = Field(..., description="Identifier of the workday plan this event occurred during.")


class SimulationEventRead(SimulationEventBase):
    """Full representation of a simulation event as persisted in the database."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    workday_plan_id: int
    created_at: datetime
