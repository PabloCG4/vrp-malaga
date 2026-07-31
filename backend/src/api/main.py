"""
FastAPI application entry point for the Rich VRP solver's Control Tower API.

Run locally, from the repository root, with:

    uvicorn backend.src.api.main:app --reload --port 8000

Interactive API documentation is then available at
`http://127.0.0.1:8000/docs` (Swagger UI) and `http://127.0.0.1:8000/redoc`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..db.session import create_all_tables, dispose_engine
from .routers.live_simulation import router as live_simulation_router
from .routers.network import router as network_router
from .routers.workdays import router as workdays_router
from .services.live_simulation import live_simulation_manager


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Ensure the schema exists on startup, and cleanly tear down on shutdown."""
    await create_all_tables()
    yield
    # Stop every running live simulation task before disposing of the engine,
    # so no background tick loop is left trying to open a session against an
    # engine that is about to be shut down.
    await live_simulation_manager.shutdown_all()
    await dispose_engine()


app = FastAPI(
    title="Rich VRP Solver - Control Tower API",
    description=(
        "REST API exposing workday plans and the 1-click static dispatch "
        "optimization for a regional logistics depot in Malaga."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Permissive CORS for local development against the future dashboard frontend,
# served from a different origin/port during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(workdays_router)
app.include_router(live_simulation_router)
app.include_router(network_router)


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    """Lightweight liveness probe used by uptime checks and local smoke tests."""
    return {"status": "ok"}
