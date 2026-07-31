"""Pydantic v2 schemas (DTOs) for the `Order` entity, including VRPPD pairs."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...db.models import DEFAULT_ORDER_SERVICE_TIME_SECONDS, DEFAULT_WORKDAY_DURATION_SECONDS


class OrderBase(BaseModel):
    """Fields common to every representation of an order."""

    customer_name: str = Field(..., min_length=1, max_length=200, description="Name of the customer to serve.")
    node_id: int = Field(..., description="Malaga street network node identifier this stop is located at.")
    latitude: float = Field(..., ge=-90.0, le=90.0, description="Latitude of the stop, in degrees (EPSG:4326).")
    longitude: float = Field(..., ge=-180.0, le=180.0, description="Longitude of the stop, in degrees (EPSG:4326).")
    demand_kg: float = Field(default=0.0, ge=0.0, description="Capacity units, in kilograms, this stop consumes.")
    service_time_seconds: int = Field(
        default=DEFAULT_ORDER_SERVICE_TIME_SECONDS, ge=0, description="Handling time required at this stop, in seconds."
    )
    time_window_start_seconds: int = Field(
        default=0, ge=0, description="Earliest service time, in seconds elapsed since 08:00."
    )
    time_window_end_seconds: int = Field(
        default=DEFAULT_WORKDAY_DURATION_SECONDS,
        ge=0,
        description="Latest desired service time, in seconds elapsed since 08:00.",
    )
    is_urgent: bool = Field(default=False, description="Whether this order arrived mid-day as an urgent request.")
    is_pickup_stop: bool = Field(
        default=False, description="Whether this row is the depot pickup leg of a VRPPD pair."
    )
    paired_order_id: int | None = Field(
        default=None, description="Identifier of the paired pickup or delivery order, for VRPPD pairs."
    )

    @model_validator(mode="after")
    def validate_time_window(self) -> "OrderBase":
        """Ensure the soft delivery time window is well-formed."""
        if self.time_window_end_seconds < self.time_window_start_seconds:
            message = (
                f"time_window_end_seconds ({self.time_window_end_seconds}) cannot precede "
                f"time_window_start_seconds ({self.time_window_start_seconds})."
            )
            raise ValueError(message)
        return self


class OrderCreate(OrderBase):
    """Payload required to create a new order within a workday plan."""

    workday_plan_id: int = Field(..., description="Identifier of the workday plan this order belongs to.")


class OrderRead(OrderBase):
    """Full representation of an order as persisted in the database."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    workday_plan_id: int
    created_at: datetime
