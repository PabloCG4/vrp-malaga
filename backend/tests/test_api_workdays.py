"""
Integration tests for the Control Tower REST API (Phase 4, Block 2).

Exercises `GET /api/v1/workdays`, `GET /api/v1/workdays/{id}` and
`POST /api/v1/workdays/{id}/optimize` end to end, against:

- A throwaway, file-based SQLite database created and torn down by this
  test alone, via a FastAPI dependency override of `get_db_session`. The
  production Neon database configured through `.env` is never touched.
- The real, preprocessed Malaga street network, so `/optimize` exercises the
  actual constructive heuristic + Tabu Search pipeline on a small, realistic
  instance, exactly as production requests would.

`httpx.ASGITransport` does not invoke FastAPI's `lifespan` unless explicitly
asked to, so `create_all_tables()`/`dispose_engine()` (which are bound to the
production engine) never run here; this test creates its own schema on its
own throwaway engine instead.

No `pytest-asyncio` dependency is introduced: every test drives its own
`asyncio.run(...)` over a single async scenario function, keeping this suite
runnable with the same plain `pytest` invocation as the rest of the backend
test suite.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from backend.src.api.main import app
from backend.src.api.services import network_provider
from backend.src.db.base import Base
from backend.src.db.enums import WorkdayStatus
from backend.src.db.models import Driver, Order, Vehicle, WorkdayPlan
from backend.src.db.session import get_db_session
from backend.src.topology.extractor import PROCESSED_GRAPH_PATH
from backend.src.topology.matrix import select_demonstration_nodes

pytestmark = pytest.mark.skipif(
    not PROCESSED_GRAPH_PATH.is_file(),
    reason=f"Processed graph not found at {PROCESSED_GRAPH_PATH}; run the topology extractor first.",
)

STANDARD_ORDER_COUNT: int = 6


def _build_test_engine(tmp_path: Path) -> AsyncEngine:
    """Create a throwaway, file-based SQLite async engine for one test run."""
    database_path = tmp_path / "control_tower_test.db"
    return create_async_engine(f"sqlite+aiosqlite:///{database_path}", future=True)


def test_list_get_and_optimize_workday(tmp_path: Path) -> None:
    """
    Full happy-path plus the two documented error responses, against one plan.

    1. `GET /api/v1/workdays` lists the seeded DRAFT plan.
    2. `GET /api/v1/workdays/{id}` returns its orders and an empty route.
    3. `GET /api/v1/workdays/{missing_id}` returns 404.
    4. `POST /api/v1/workdays/{id}/optimize` computes and persists a feasible
       route, and flips the plan's status to ACTIVE.
    5. A second `POST .../optimize` on the now-ACTIVE plan returns 409.
    """
    test_engine = _build_test_engine(tmp_path)
    session_factory = async_sessionmaker(bind=test_engine, expire_on_commit=False)

    async def override_get_db_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_get_db_session

    async def scenario() -> None:
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        street_network_graph = network_provider.get_street_network_graph()
        depot_node = network_provider.get_depot_node()
        _, candidate_nodes = select_demonstration_nodes(street_network_graph, 20, random_seed=777)
        order_nodes = [node_id for node_id in candidate_nodes if node_id != depot_node][:STANDARD_ORDER_COUNT]
        assert len(order_nodes) == STANDARD_ORDER_COUNT

        async with session_factory() as session:
            driver = Driver(full_name="Test Driver", license_number="TEST-LIC-0001")
            session.add(driver)
            await session.flush()

            session.add_all(
                [
                    Vehicle(license_plate="TEST-0001", capacity_kg=500.0, default_driver_id=driver.id),
                    Vehicle(license_plate="TEST-0002", capacity_kg=500.0),
                ]
            )
            await session.flush()

            workday_plan = WorkdayPlan(workday_date=date(2030, 1, 1), status=WorkdayStatus.DRAFT)
            session.add(workday_plan)
            await session.flush()

            for index, node_id in enumerate(order_nodes):
                node_attributes = street_network_graph.nodes[node_id]
                session.add(
                    Order(
                        workday_plan_id=workday_plan.id,
                        customer_name=f"Test Customer {index + 1}",
                        node_id=node_id,
                        latitude=float(node_attributes["y"]),
                        longitude=float(node_attributes["x"]),
                        demand_kg=20.0,
                        service_time_seconds=300,
                        time_window_start_seconds=0,
                        time_window_end_seconds=28800,
                    )
                )
            await session.commit()
            workday_plan_id = workday_plan.id

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            list_response = await client.get("/api/v1/workdays")
            assert list_response.status_code == 200
            assert any(item["id"] == workday_plan_id for item in list_response.json())

            detail_response = await client.get(f"/api/v1/workdays/{workday_plan_id}")
            assert detail_response.status_code == 200
            detail = detail_response.json()
            assert detail["status"] == "DRAFT"
            assert len(detail["orders"]) == STANDARD_ORDER_COUNT
            assert detail["route_stops"] == []
            assert detail["simulation_events"] == []

            empty_geometry_response = await client.get(f"/api/v1/workdays/{workday_plan_id}/route-geometry")
            assert empty_geometry_response.status_code == 200
            empty_geometry = empty_geometry_response.json()
            assert empty_geometry["workday_plan_id"] == workday_plan_id
            assert empty_geometry["legs"] == []

            missing_response = await client.get("/api/v1/workdays/999999")
            assert missing_response.status_code == 404

            optimize_response = await client.post(f"/api/v1/workdays/{workday_plan_id}/optimize")
            assert optimize_response.status_code == 200, optimize_response.text
            optimization = optimize_response.json()
            assert optimization["is_feasible"] is True
            assert optimization["route_stop_count"] > 0
            assert optimization["workday_plan"]["status"] == "ACTIVE"
            assert len(optimization["workday_plan"]["route_stops"]) == optimization["route_stop_count"]
            assert len(optimization["workday_plan"]["orders"]) == STANDARD_ORDER_COUNT
            assert optimization["workday_plan"]["simulation_events"] == []

            geometry_response = await client.get(f"/api/v1/workdays/{workday_plan_id}/route-geometry")
            assert geometry_response.status_code == 200
            geometry = geometry_response.json()
            assert geometry["workday_plan_id"] == workday_plan_id
            assert len(geometry["legs"]) > 0
            longest_leg = max(geometry["legs"], key=lambda leg: len(leg["coordinates"]))
            assert len(longest_leg["coordinates"]) >= 3
            first_coordinate = longest_leg["coordinates"][0]
            assert len(first_coordinate) == 2
            assert isinstance(first_coordinate[0], float)
            assert isinstance(first_coordinate[1], float)

            missing_geometry_response = await client.get("/api/v1/workdays/999999/route-geometry")
            assert missing_geometry_response.status_code == 404

            repeat_response = await client.post(f"/api/v1/workdays/{workday_plan_id}/optimize")
            assert repeat_response.status_code == 409

    try:
        asyncio.run(scenario())
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        asyncio.run(test_engine.dispose())
