"""
Business logic for the Control Tower's workday plans.

This module owns every database interaction the workday endpoints need:
listing plans, retrieving one with its orders/route stops, resolving the
active fleet, and running the 1-click static dispatch optimization. Routers
stay free of SQLAlchemy and solver concerns, calling only the plain,
async functions defined here and mapping the exceptions they raise to HTTP
responses.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...db.enums import RouteStopType, WorkdayStatus
from ...db.models import Order, RouteStop, Vehicle, WorkdayPlan
from .network_provider import get_depot_node, get_street_network_graph
from .solver_bridge import OptimizationOutcome, run_static_optimization


class WorkdayServiceError(Exception):
    """Base class for every error raised by the workday service layer."""


class WorkdayNotFoundError(WorkdayServiceError):
    """Raised when a requested workday plan does not exist."""


class WorkdayNotInDraftStatusError(WorkdayServiceError):
    """Raised when optimization is requested for a plan that is not DRAFT."""


class WorkdayHasNoOrdersError(WorkdayServiceError):
    """Raised when optimization is requested for a plan with no orders to serve."""


class NoActiveVehicleError(WorkdayServiceError):
    """Raised when optimization is requested but no active vehicle exists in the fleet."""


async def list_workday_plans(session: AsyncSession) -> list[WorkdayPlan]:
    """Return every workday plan, most recently scheduled first."""
    result = await session.execute(select(WorkdayPlan).order_by(WorkdayPlan.workday_date.desc()))
    return list(result.scalars().all())


async def get_workday_plan(session: AsyncSession, workday_plan_id: int) -> WorkdayPlan:
    """
    Return one workday plan with its orders and route stops eagerly loaded.

    Raises
    ------
    WorkdayNotFoundError
        If no plan with `workday_plan_id` exists.
    """
    result = await session.execute(
        select(WorkdayPlan)
        .where(WorkdayPlan.id == workday_plan_id)
        .options(selectinload(WorkdayPlan.orders), selectinload(WorkdayPlan.route_stops))
    )
    workday_plan = result.scalar_one_or_none()
    if workday_plan is None:
        message = f"Workday plan {workday_plan_id} does not exist."
        raise WorkdayNotFoundError(message)
    return workday_plan


async def list_active_vehicles(session: AsyncSession) -> list[Vehicle]:
    """Return every vehicle currently available for dispatch, ordered by id."""
    result = await session.execute(select(Vehicle).where(Vehicle.is_active.is_(True)).order_by(Vehicle.id))
    return list(result.scalars().all())


async def optimize_workday_plan(
    session: AsyncSession, workday_plan_id: int
) -> tuple[WorkdayPlan, OptimizationOutcome]:
    """
    Run the 1-click static dispatch optimization for a DRAFT workday plan.

    Loads the plan's orders and the active fleet, runs the constructive
    heuristic followed by Tabu Search off the event loop (via
    `asyncio.to_thread`, since both are CPU-bound and would otherwise block
    every other request being served), replaces any previously stored
    `RouteStop` rows for this plan with the freshly computed sequence,
    updates the plan's KPIs, and transitions its status from DRAFT to ACTIVE
    (dispatched).

    Parameters
    ----------
    session:
        Request-scoped async database session.
    workday_plan_id:
        Identifier of the plan to optimize.

    Returns
    -------
    tuple[WorkdayPlan, OptimizationOutcome]
        The updated plan, with `orders` and `route_stops` refreshed, and the
        solver's diagnostics for the run that produced it.

    Raises
    ------
    WorkdayNotFoundError
        If no plan with `workday_plan_id` exists.
    WorkdayNotInDraftStatusError
        If the plan's status is not currently DRAFT.
    WorkdayHasNoOrdersError
        If the plan has no orders to optimize.
    NoActiveVehicleError
        If no active vehicle is available to serve them.
    """
    result = await session.execute(
        select(WorkdayPlan).where(WorkdayPlan.id == workday_plan_id).options(selectinload(WorkdayPlan.orders))
    )
    workday_plan = result.scalar_one_or_none()
    if workday_plan is None:
        message = f"Workday plan {workday_plan_id} does not exist."
        raise WorkdayNotFoundError(message)
    if workday_plan.status != WorkdayStatus.DRAFT:
        message = f"Workday plan {workday_plan_id} is '{workday_plan.status.value}', not 'DRAFT'."
        raise WorkdayNotInDraftStatusError(message)

    orders: list[Order] = list(workday_plan.orders)
    if not orders:
        message = f"Workday plan {workday_plan_id} has no orders to optimize."
        raise WorkdayHasNoOrdersError(message)

    vehicles = await list_active_vehicles(session)
    if not vehicles:
        message = "No active vehicle is available in the fleet."
        raise NoActiveVehicleError(message)

    street_network_graph = get_street_network_graph()
    depot_node = get_depot_node()

    outcome: OptimizationOutcome = await asyncio.to_thread(
        run_static_optimization, street_network_graph, depot_node, orders, vehicles
    )

    await session.execute(delete(RouteStop).where(RouteStop.workday_plan_id == workday_plan_id))

    vehicle_row_by_domain_id = {str(vehicle.id): vehicle for vehicle in vehicles}
    for route, route_evaluation in zip(outcome.best_state.routes, outcome.best_evaluation.route_evaluations):
        if not route_evaluation.is_reachable:
            continue

        vehicle_row = vehicle_row_by_domain_id[route.vehicle_id]
        for position, stop_visit in enumerate(route_evaluation.stop_schedule):
            if stop_visit.customer_id is None:
                stop_type = RouteStopType.DEPOT_START if position == 0 else RouteStopType.DEPOT_END
                order_id: int | None = None
            else:
                order_row = outcome.orders_by_customer_id[stop_visit.customer_id]
                stop_type = RouteStopType.DEPOT_PICKUP if order_row.is_pickup_stop else RouteStopType.CUSTOMER_DELIVERY
                order_id = order_row.id

            session.add(
                RouteStop(
                    workday_plan_id=workday_plan_id,
                    vehicle_id=vehicle_row.id,
                    order_id=order_id,
                    sequence_order=position,
                    stop_type=stop_type,
                    node_id=stop_visit.node_id,
                    planned_arrival_seconds=int(round(stop_visit.arrival_time_seconds)),
                    departure_seconds=int(round(stop_visit.departure_time_seconds)),
                )
            )

    workday_plan.status = WorkdayStatus.ACTIVE
    workday_plan.total_cost = outcome.best_evaluation.total_cost
    workday_plan.total_distance_km = outcome.best_evaluation.total_distance_meters / 1000.0
    workday_plan.execution_time_ms = int(outcome.elapsed_seconds * 1000.0)

    await session.commit()

    refreshed_workday_plan = await get_workday_plan(session, workday_plan_id)
    return refreshed_workday_plan, outcome
