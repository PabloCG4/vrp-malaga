"""Pydantic v2 schemas (DTOs) for the `Vehicle` entity."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class VehicleBase(BaseModel):
    """Fields common to every representation of a vehicle."""

    license_plate: str = Field(..., min_length=1, max_length=20, description="Unique vehicle license plate.")
    capacity_kg: float = Field(..., gt=0.0, description="Maximum load, in kilograms, this vehicle may carry at once.")
    default_driver_id: int | None = Field(
        default=None, description="Identifier of the driver usually assigned to this vehicle, if any."
    )
    is_active: bool = Field(default=True, description="Whether this vehicle is currently available for dispatch.")


class VehicleCreate(VehicleBase):
    """Payload required to register a new vehicle."""


class VehicleRead(VehicleBase):
    """Full representation of a vehicle as persisted in the database."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
