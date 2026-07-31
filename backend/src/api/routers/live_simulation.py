"""
FastAPI router exposing the Control Tower's live simulation WebSocket stream
and real-time disruption injection endpoints.

Every handler here stays HTTP/WebSocket-thin: connection lifecycle, request
validation and exception-to-status-code mapping only. All simulation state,
solver invocation and persistence live in `api.services.live_simulation`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.enums import SimulationEventType
from ...db.session import get_db_session
from ..schemas.live_simulation import (
    EligibleUrgentOrderNode,
    EventInjectionAck,
    TrafficIncidentInjectionRequest,
    UrgentOrderInjectionRequest,
)
from ..services import network_provider, workday_service
from ..services.live_simulation import (
    DEFAULT_TICK_INTERVAL_SECONDS,
    DeliveryNodeNotInNetworkError,
    NoVehicleAvailableError,
    StreetNotAdjacentError,
    WorkdayNotActiveError,
    get_reserved_urgent_order_nodes,
    live_simulation_manager,
)
from ..services.workday_service import WorkdayNotFoundError

router = APIRouter(prefix="/api/v1/workdays", tags=["live-simulation"])


@router.get(
    "/{workday_id}/events/urgent-order-nodes",
    response_model=list[EligibleUrgentOrderNode],
)
async def list_eligible_urgent_order_nodes(
    workday_id: int, session: AsyncSession = Depends(get_db_session)
) -> list[EligibleUrgentOrderNode]:
    """
    List the nodes a `POST .../events/urgent-order` call may legally target for this plan.

    `CostMatrix` cannot grow a new row/column once a live session is
    running, so an urgent order's delivery node must be one of a reserved
    pool decided ahead of time (see `services.live_simulation.get_reserved_urgent_order_nodes`).
    This endpoint lets a dispatcher UI discover, and place on a map, exactly
    which nodes are eligible before submitting an injection.
    """
    try:
        workday_plan = await workday_service.get_workday_plan(session, workday_id)
    except WorkdayNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    street_network_graph = network_provider.get_street_network_graph()
    depot_node = network_provider.get_depot_node()
    reserved_nodes = get_reserved_urgent_order_nodes(street_network_graph, depot_node, list(workday_plan.orders))

    return [
        EligibleUrgentOrderNode(
            node_id=node_id,
            latitude=float(street_network_graph.nodes[node_id]["y"]),
            longitude=float(street_network_graph.nodes[node_id]["x"]),
        )
        for node_id in reserved_nodes
    ]


@router.websocket("/{workday_id}/live")
async def stream_live_simulation(
    websocket: WebSocket,
    workday_id: int,
    tick_interval_seconds: float = Query(
        default=DEFAULT_TICK_INTERVAL_SECONDS,
        gt=0.0,
        le=60.0,
        description=(
            "Real-world seconds per simulated minute. Only honored the first "
            "time this workday plan's session is created; ignored when "
            "attaching to an already running one."
        ),
    ),
) -> None:
    """
    Stream minute-by-minute fleet telemetry for an ACTIVE workday plan.

    Lazily starts (or attaches to an already running) `LiveSimulationSession`
    for this plan, immediately sends a full "snapshot" message, and then
    pushes a "tick" message every simulated minute, plus "event" and
    "reoptimization" messages whenever a disruption is dispatched (by this
    connection, another client, or one of the REST injection endpoints
    below), and a final "finished" message once the workday completes.

    This endpoint is server-push only: the client is not expected to send
    anything, and the connection is closed automatically once it
    disconnects. The socket is closed with code 4404 if the plan does not
    exist, or 4409 if it is not currently ACTIVE.
    """
    await websocket.accept()
    try:
        session = await live_simulation_manager.get_or_create_session(workday_id, tick_interval_seconds)
    except WorkdayNotFoundError as error:
        await websocket.close(code=4404, reason=str(error))
        return
    except WorkdayNotActiveError as error:
        await websocket.close(code=4409, reason=str(error))
        return

    await session.add_subscriber(websocket)
    try:
        while True:
            # Server-push only: block until the client disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await session.remove_subscriber(websocket)


@router.post(
    "/{workday_id}/events/traffic",
    response_model=EventInjectionAck,
    status_code=status.HTTP_202_ACCEPTED,
)
async def inject_traffic_incident(workday_id: int, payload: TrafficIncidentInjectionRequest) -> EventInjectionAck:
    """
    Inject a real-time traffic incident (street closure) into a live simulation.

    Lazily starts the workday plan's `LiveSimulationSession` if it is not
    already running, closes the street between the two given nodes, and
    triggers a bounded, locked-prefix-aware re-optimization of every route
    the closure actually affects.

    Returns 404 if the plan does not exist, 409 if it is not currently
    ACTIVE, and 422 if the two nodes share no street edge in the network.
    """
    try:
        session = await live_simulation_manager.get_or_create_session(workday_id)
        trigger_minute = await session.inject_traffic_incident(
            first_node=payload.first_node,
            second_node=payload.second_node,
            reopen_after_minutes=payload.reopen_after_minutes,
            description=payload.description,
        )
    except WorkdayNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except WorkdayNotActiveError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except StreetNotAdjacentError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    return EventInjectionAck(
        workday_plan_id=workday_id,
        event_type=SimulationEventType.TRAFFIC_INCIDENT,
        trigger_minute=trigger_minute,
        message=f"Traffic incident applied at simulated minute {trigger_minute}.",
    )


@router.post(
    "/{workday_id}/events/urgent-order",
    response_model=EventInjectionAck,
    status_code=status.HTTP_202_ACCEPTED,
)
async def inject_urgent_order(workday_id: int, payload: UrgentOrderInjectionRequest) -> EventInjectionAck:
    """
    Inject a real-time, same-day urgent VRPPD order into a live simulation.

    Lazily starts the workday plan's `LiveSimulationSession` if it is not
    already running, models the order as a depot-pickup-and-delivery pair,
    persists both new `Order` rows, and lets a bounded re-optimization place
    them anywhere in the fleet's unvisited remainder.

    Returns 404 if the plan does not exist, 409 if it is not currently
    ACTIVE, and 422 if no active vehicle can be found to serve the order.
    """
    try:
        session = await live_simulation_manager.get_or_create_session(workday_id)
        order_id, trigger_minute = await session.inject_urgent_order(
            delivery_node=payload.delivery_node,
            demand=payload.demand,
            order_id=payload.order_id,
            pickup_service_time_seconds=payload.pickup_service_time_seconds,
            delivery_service_time_seconds=payload.delivery_service_time_seconds,
            deadline_minutes_after_trigger=payload.deadline_minutes_after_trigger,
            description=payload.description,
        )
    except WorkdayNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except WorkdayNotActiveError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except (DeliveryNodeNotInNetworkError, NoVehicleAvailableError) as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    return EventInjectionAck(
        workday_plan_id=workday_id,
        event_type=SimulationEventType.URGENT_ORDER,
        trigger_minute=trigger_minute,
        order_id=order_id,
        message=f"Urgent order '{order_id}' injected at simulated minute {trigger_minute}.",
    )
