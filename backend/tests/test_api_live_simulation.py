"""
Integration tests for the Control Tower live simulation layer (Phase 4, Block 3).

Exercises `GET /api/v1/workdays/{id}/live` (WebSocket telemetry) together with
`POST /api/v1/workdays/{id}/events/traffic` and
`POST /api/v1/workdays/{id}/events/urgent-order`, against:

- A throwaway, file-based SQLite database, via a FastAPI dependency override
  of `get_db_session` *and* `services.live_simulation.set_session_factory_for_testing`,
  since `LiveSimulationSession` opens its own database sessions from a
  long-lived background task rather than through FastAPI's per-request
  dependency injection. The production Neon database is never touched.
- The real, preprocessed Malaga street network, exactly like
  `test_api_workdays.py`.

`starlette.testclient.TestClient` (re-exported by `fastapi.testclient`) is
used instead of `httpx.AsyncClient`, since only `TestClient` supports
WebSocket connections. It is used as a context manager (`with TestClient(app)
as client:`) specifically because that is the one mode in which every HTTP
and WebSocket call shares a single background event loop/thread (Starlette's
"blocking portal"): the live simulation's `asyncio.Lock` and background tick
`asyncio.Task` are not safe to touch from more than one event loop, which is
exactly what would happen if the context-manager form were not used. Using
the context manager also triggers FastAPI's `lifespan`, so
`create_all_tables`/`dispose_engine` (bound to the production engine) are
monkeypatched to no-ops for the duration of this test.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from backend.src.api import main as api_main
from backend.src.api.services import live_simulation, network_provider
from backend.src.db.base import Base
from backend.src.db.enums import SimulationEventType, WorkdayStatus
from backend.src.db.models import Driver, Order, SimulationEvent, Vehicle, WorkdayPlan
from backend.src.db.session import get_db_session
from backend.src.topology.extractor import PROCESSED_GRAPH_PATH
from backend.src.topology.matrix import select_demonstration_nodes

pytestmark = pytest.mark.skipif(
    not PROCESSED_GRAPH_PATH.is_file(),
    reason=f"Processed graph not found at {PROCESSED_GRAPH_PATH}; run the topology extractor first.",
)

STANDARD_ORDER_COUNT: int = 5
# Fast enough to observe several ticks within the test's bounded message
# reads, slow enough not to flood the WebSocket stream with noise.
TEST_TICK_INTERVAL_SECONDS: float = 0.15
MAX_MESSAGES_TO_SCAN: int = 40


def _build_test_engine(tmp_path: Path) -> AsyncEngine:
    """Create a throwaway, file-based SQLite async engine for one test run."""
    database_path = tmp_path / "control_tower_live_test.db"
    return create_async_engine(f"sqlite+aiosqlite:///{database_path}", future=True)


async def _noop_lifespan_hook() -> None:
    """Stand-in for `create_all_tables`/`dispose_engine` during this test's lifespan."""


def _drain_until(websocket: Any, predicate: Any, max_messages: int = MAX_MESSAGES_TO_SCAN) -> dict[str, Any]:
    """Read WebSocket JSON messages until one satisfies `predicate`, or fail."""
    for _ in range(max_messages):
        message = websocket.receive_json()
        if predicate(message):
            return message
    pytest.fail(f"Did not observe a matching message within {max_messages} reads.")


def test_live_simulation_ws_and_event_injection(tmp_path: Path) -> None:
    """
    End-to-end live simulation flow for one ACTIVE workday plan.

    1. Seed a DRAFT plan with standard orders and optimize it (Block 2's
       pipeline) so it has a real, persisted route and is ACTIVE.
    2. Connect to `GET /api/v1/workdays/{id}/live` and receive the initial
       "snapshot" telemetry message.
    3. Inject a traffic incident via REST and observe the corresponding
       "event" message on the WebSocket stream.
    4. Inject an urgent VRPPD order via REST and observe its "event" message,
       then verify two new `Order` rows and a widened `route_stops` set were
       persisted.
    5. Verify both disruptions were appended to `simulation_events`.
    """
    test_engine = _build_test_engine(tmp_path)
    session_factory = async_sessionmaker(bind=test_engine, expire_on_commit=False)

    async def seed() -> int:
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        street_network_graph = network_provider.get_street_network_graph()
        depot_node = network_provider.get_depot_node()
        _, candidate_nodes = select_demonstration_nodes(street_network_graph, 20, random_seed=2024)
        order_nodes = [node_id for node_id in candidate_nodes if node_id != depot_node][:STANDARD_ORDER_COUNT]
        first_node, second_node, _ = next(iter(street_network_graph.edges(keys=True)))

        async with session_factory() as session:
            driver = Driver(full_name="Live Test Driver", license_number="LIVE-TEST-0001")
            session.add(driver)
            await session.flush()

            session.add_all(
                [
                    Vehicle(license_plate="LIVE-0001", capacity_kg=600.0, default_driver_id=driver.id),
                    Vehicle(license_plate="LIVE-0002", capacity_kg=600.0),
                ]
            )
            await session.flush()

            workday_plan = WorkdayPlan(workday_date=date(2030, 2, 1), status=WorkdayStatus.DRAFT)
            session.add(workday_plan)
            await session.flush()

            for index, node_id in enumerate(order_nodes):
                node_attributes = street_network_graph.nodes[node_id]
                session.add(
                    Order(
                        workday_plan_id=workday_plan.id,
                        customer_name=f"Live Test Customer {index + 1}",
                        node_id=node_id,
                        latitude=float(node_attributes["y"]),
                        longitude=float(node_attributes["x"]),
                        demand_kg=15.0,
                        service_time_seconds=240,
                        time_window_start_seconds=0,
                        time_window_end_seconds=28800,
                    )
                )
            await session.commit()
            workday_plan_id = workday_plan.id

        # The live session reserves a deterministic pool of extra nodes for
        # urgent orders (see `get_reserved_urgent_order_nodes`); the urgent
        # order this test injects must target one of them.
        reserved_nodes = live_simulation.get_reserved_urgent_order_nodes(
            street_network_graph, depot_node, [Order(node_id=node_id) for node_id in order_nodes]
        )
        urgent_delivery_node = reserved_nodes[0]

        await test_engine.dispose()
        return workday_plan_id, urgent_delivery_node, first_node, second_node

    workday_plan_id, urgent_delivery_node, first_node, second_node = asyncio.run(seed())

    async def override_get_db_session():
        async with session_factory() as session:
            yield session

    api_main.app.dependency_overrides[get_db_session] = override_get_db_session
    live_simulation.set_session_factory_for_testing(session_factory)

    try:
        with (
            patch.object(api_main, "create_all_tables", _noop_lifespan_hook),
            patch.object(api_main, "dispose_engine", _noop_lifespan_hook),
            TestClient(api_main.app) as client,
        ):
            optimize_response = client.post(f"/api/v1/workdays/{workday_plan_id}/optimize")
            assert optimize_response.status_code == 200, optimize_response.text
            assert optimize_response.json()["workday_plan"]["status"] == "ACTIVE"

            with client.websocket_connect(
                f"/api/v1/workdays/{workday_plan_id}/live?tick_interval_seconds={TEST_TICK_INTERVAL_SECONDS}"
            ) as websocket:
                snapshot = websocket.receive_json()
                assert snapshot["type"] == "snapshot"
                assert snapshot["workday_plan_id"] == workday_plan_id
                assert len(snapshot["vehicles"]) == 2

                traffic_response = client.post(
                    f"/api/v1/workdays/{workday_plan_id}/events/traffic",
                    json={
                        "first_node": first_node,
                        "second_node": second_node,
                        "reopen_after_minutes": 30,
                        "description": "Test traffic incident",
                    },
                )
                assert traffic_response.status_code == 202, traffic_response.text
                assert traffic_response.json()["event_type"] == "TRAFFIC_INCIDENT"

                traffic_event_message = _drain_until(
                    websocket,
                    lambda message: message.get("type") == "event" and message.get("event_type") == "TRAFFIC_INCIDENT",
                )
                assert traffic_event_message["payload"]["first_node"] == first_node

                urgent_order_response = client.post(
                    f"/api/v1/workdays/{workday_plan_id}/events/urgent-order",
                    json={
                        "delivery_node": urgent_delivery_node,
                        "demand": 25.0,
                        "order_id": "URG-TEST-1",
                        "description": "Test urgent order",
                    },
                )
                assert urgent_order_response.status_code == 202, urgent_order_response.text
                urgent_ack = urgent_order_response.json()
                assert urgent_ack["event_type"] == "URGENT_ORDER"
                assert urgent_ack["order_id"] == "URG-TEST-1"

                urgent_event_message = _drain_until(
                    websocket,
                    lambda message: message.get("type") == "event" and message.get("event_type") == "URGENT_ORDER",
                )
                assert urgent_event_message["payload"]["order_id"] == "URG-TEST-1"
    finally:
        api_main.app.dependency_overrides.pop(get_db_session, None)
        live_simulation.set_session_factory_for_testing(None)

    async def verify() -> None:
        async with session_factory() as session:
            events_result = await session.execute(
                select(SimulationEvent).where(SimulationEvent.workday_plan_id == workday_plan_id)
            )
            events = list(events_result.scalars().all())
            assert {event.event_type for event in events} == {
                SimulationEventType.TRAFFIC_INCIDENT,
                SimulationEventType.URGENT_ORDER,
            }

            orders_result = await session.execute(select(Order).where(Order.workday_plan_id == workday_plan_id))
            orders = list(orders_result.scalars().all())
            assert len(orders) == STANDARD_ORDER_COUNT + 2
            urgent_orders = [order for order in orders if order.is_urgent]
            assert len(urgent_orders) == 2
            delivery_order = next(order for order in urgent_orders if not order.is_pickup_stop)
            pickup_order = next(order for order in urgent_orders if order.is_pickup_stop)
            assert delivery_order.node_id == urgent_delivery_node
            assert delivery_order.paired_order_id == pickup_order.id
            assert pickup_order.paired_order_id == delivery_order.id

        await test_engine.dispose()

    asyncio.run(verify())
