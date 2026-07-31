"""Pydantic v2 schemas (DTOs) for the `WorkdayPlan` aggregate."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from ...db.enums import WorkdayStatus
from .order import OrderRead
from .route_stop import RouteStopRead
from .simulation_event import SimulationEventRead
from .vehicle import VehicleRead


class WorkdayPlanBase(BaseModel):
    """Fields common to every representation of a workday plan."""

    workday_date: date = Field(..., description="Calendar date this plan covers, unique across all plans.")
    status: WorkdayStatus = Field(default=WorkdayStatus.DRAFT, description="Lifecycle status of the plan.")
    total_cost: float = Field(default=0.0, ge=0.0, description="Weighted objective value f(S) of the committed plan.")
    total_distance_km: float = Field(default=0.0, ge=0.0, description="Total fleet driving distance, in kilometers.")
    execution_time_ms: int = Field(default=0, ge=0, description="Wall-clock time spent computing this plan, in milliseconds.")


class WorkdayPlanCreate(BaseModel):
    """Payload required to create a new, typically empty, workday plan."""

    workday_date: date = Field(..., description="Calendar date this plan covers, unique across all plans.")
    status: WorkdayStatus = Field(default=WorkdayStatus.DRAFT, description="Lifecycle status of the plan.")


class WorkdayPlanUpdate(BaseModel):
    """Partial payload for updating a plan's status and solver-computed metrics."""

    status: WorkdayStatus | None = None
    total_cost: float | None = Field(default=None, ge=0.0)
    total_distance_km: float | None = Field(default=None, ge=0.0)
    execution_time_ms: int | None = Field(default=None, ge=0)


class WorkdayPlanRead(WorkdayPlanBase):
    """Full representation of a workday plan as persisted in the database."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


class WorkdayPlanDetailRead(WorkdayPlanRead):
    """
    Workday plan with its full context: orders, fleet and planned route stops.

    Returned by `GET /api/v1/workdays/{id}` and by the optimize endpoint, so
    a dispatcher can inspect the exact orders behind a plan, the active fleet
    available to serve them, and, once optimized, the resulting per-vehicle
    sequence, in a single call.
    """

    orders: list[OrderRead] = Field(default_factory=list, description="Every order belonging to this plan.")
    route_stops: list[RouteStopRead] = Field(
        default_factory=list, description="Planned sequence of every vehicle, empty until the plan is optimized."
    )
    vehicles: list[VehicleRead] = Field(
        default_factory=list, description="Active fleet available to serve this plan's orders."
    )
    simulation_events: list[SimulationEventRead] = Field(
        default_factory=list,
        description="Audit log of traffic incidents and urgent orders injected during this plan's live simulation.",
    )
