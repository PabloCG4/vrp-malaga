"""Pydantic v2 schemas (DTOs) for the `Driver` entity."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ...db.models import EU_MAX_CONTINUOUS_DRIVING_SECONDS


class DriverBase(BaseModel):
    """Fields common to every representation of a driver."""

    full_name: str = Field(..., min_length=1, max_length=200, description="Driver's full legal name.")
    license_number: str = Field(..., min_length=1, max_length=50, description="Unique driving license number.")
    max_continuous_driving_seconds: int = Field(
        default=EU_MAX_CONTINUOUS_DRIVING_SECONDS,
        gt=0,
        description="EU Regulation (EC) No 561/2006 continuous driving limit, in seconds, before a mandatory break.",
    )
    is_active: bool = Field(default=True, description="Whether this driver is currently available for dispatch.")


class DriverCreate(DriverBase):
    """Payload required to register a new driver."""


class DriverRead(DriverBase):
    """Full representation of a driver as persisted in the database."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
