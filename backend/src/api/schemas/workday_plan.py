"""Pydantic v2 schemas (DTOs) for the `WorkdayPlan` aggregate."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from ...db.enums import WorkdayStatus


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
