"""Pydantic v2 schema for the result of a 1-click static dispatch optimization."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .workday_plan import WorkdayPlanDetailRead


class WorkdayOptimizationResult(BaseModel):
    """Outcome of a `POST /api/v1/workdays/{id}/optimize` call."""

    workday_plan: WorkdayPlanDetailRead = Field(
        ..., description="The updated workday plan, including its newly computed route stops."
    )
    route_stop_count: int = Field(..., ge=0, description="Number of route stops written by this optimization.")
    iterations_completed: int = Field(..., ge=0, description="Tabu Search iterations completed before stopping.")
    elapsed_seconds: float = Field(..., ge=0.0, description="Wall-clock time spent computing this plan, in seconds.")
    is_feasible: bool = Field(
        ..., description="Whether the resulting plan respects every hard constraint (capacity, workday, precedence)."
    )
