"""
FastAPI router exposing the Control Tower's workday plan endpoints.

Every handler here is intentionally thin: it validates the HTTP-level
concerns (path parameters, status codes), delegates all business logic to
`api.services.workday_service`, and serializes the resulting ORM objects
into Pydantic response models. Domain/service exceptions are translated into
the appropriate HTTP status codes at the boundary, so the service layer
itself never has to know about FastAPI or HTTP.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models import Vehicle, WorkdayPlan
from ...db.session import get_db_session
from ..schemas.optimization import WorkdayOptimizationResult
from ..schemas.order import OrderRead
from ..schemas.route_stop import RouteStopRead
from ..schemas.vehicle import VehicleRead
from ..schemas.workday_plan import WorkdayPlanDetailRead, WorkdayPlanRead
from ..services import workday_service
from ..services.workday_service import (
    NoActiveVehicleError,
    WorkdayHasNoOrdersError,
    WorkdayNotFoundError,
    WorkdayNotInDraftStatusError,
)

router = APIRouter(prefix="/api/v1/workdays", tags=["workdays"])


def _to_detail_schema(workday_plan: WorkdayPlan, vehicles: list[Vehicle]) -> WorkdayPlanDetailRead:
    """Serialize a workday plan, its orders/route stops, and the active fleet into one DTO."""
    return WorkdayPlanDetailRead(
        id=workday_plan.id,
        workday_date=workday_plan.workday_date,
        status=workday_plan.status,
        total_cost=workday_plan.total_cost,
        total_distance_km=workday_plan.total_distance_km,
        execution_time_ms=workday_plan.execution_time_ms,
        created_at=workday_plan.created_at,
        updated_at=workday_plan.updated_at,
        orders=[OrderRead.model_validate(order) for order in workday_plan.orders],
        route_stops=[RouteStopRead.model_validate(route_stop) for route_stop in workday_plan.route_stops],
        vehicles=[VehicleRead.model_validate(vehicle) for vehicle in vehicles],
    )


@router.get("", response_model=list[WorkdayPlanRead])
async def list_workdays(session: AsyncSession = Depends(get_db_session)) -> list[WorkdayPlanRead]:
    """Return every workday plan, most recently scheduled first."""
    workday_plans = await workday_service.list_workday_plans(session)
    return [WorkdayPlanRead.model_validate(workday_plan) for workday_plan in workday_plans]


@router.get("/{workday_id}", response_model=WorkdayPlanDetailRead)
async def get_workday(workday_id: int, session: AsyncSession = Depends(get_db_session)) -> WorkdayPlanDetailRead:
    """Return one workday plan with its orders, active fleet, and planned route stops."""
    try:
        workday_plan = await workday_service.get_workday_plan(session, workday_id)
    except WorkdayNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    vehicles = await workday_service.list_active_vehicles(session)
    return _to_detail_schema(workday_plan, vehicles)


@router.post(
    "/{workday_id}/optimize",
    response_model=WorkdayOptimizationResult,
    status_code=status.HTTP_200_OK,
)
async def optimize_workday(
    workday_id: int, session: AsyncSession = Depends(get_db_session)
) -> WorkdayOptimizationResult:
    """
    Trigger the 1-click static dispatch optimization for a DRAFT workday plan.

    Runs the constructive heuristic followed by Tabu Search off the event
    loop, persists the resulting route stops (replacing any previously
    stored ones for this plan), and updates the plan's status (DRAFT ->
    ACTIVE) and KPIs.

    Returns 404 if the plan does not exist, 409 if it is not currently
    DRAFT, and 422 if it has no orders or no active vehicle to serve them
    with.
    """
    try:
        workday_plan, outcome = await workday_service.optimize_workday_plan(session, workday_id)
    except WorkdayNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except WorkdayNotInDraftStatusError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except (WorkdayHasNoOrdersError, NoActiveVehicleError) as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    vehicles = await workday_service.list_active_vehicles(session)
    return WorkdayOptimizationResult(
        workday_plan=_to_detail_schema(workday_plan, vehicles),
        route_stop_count=len(workday_plan.route_stops),
        iterations_completed=outcome.iterations_completed,
        elapsed_seconds=outcome.elapsed_seconds,
        is_feasible=outcome.best_evaluation.is_feasible,
    )
