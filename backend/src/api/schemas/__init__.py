"""
Pydantic v2 data validation schemas (DTOs) for the Control Tower API layer.

Every schema module mirrors one entity of `backend/src/db/models.py`, each
exposing a `*Create` schema (validated input, no server-assigned fields) and
a `*Read` schema (`from_attributes=True`, ready to be built directly from an
ORM instance) so that a future FastAPI layer never has to serialize or
validate SQLAlchemy models by hand.
"""

from __future__ import annotations

from .driver import DriverBase, DriverCreate, DriverRead
from .geometry import RouteLegGeometry, WorkdayRouteGeometry
from .live_simulation import (
    EligibleUrgentOrderNode,
    EventInjectionAck,
    TrafficIncidentInjectionRequest,
    UrgentOrderInjectionRequest,
)
from .network import NetworkGraph, NetworkNode
from .optimization import WorkdayOptimizationResult
from .order import OrderBase, OrderCreate, OrderRead
from .route_stop import (
    RouteStopBase,
    RouteStopCreate,
    RouteStopRead,
    RouteStopTelemetryUpdate,
)
from .simulation_event import (
    SimulationEventBase,
    SimulationEventCreate,
    SimulationEventRead,
)
from .vehicle import VehicleBase, VehicleCreate, VehicleRead
from .workday_plan import (
    WorkdayPlanBase,
    WorkdayPlanCreate,
    WorkdayPlanDetailRead,
    WorkdayPlanRead,
    WorkdayPlanUpdate,
)

__all__ = [
    "DriverBase",
    "DriverCreate",
    "DriverRead",
    "VehicleBase",
    "VehicleCreate",
    "VehicleRead",
    "WorkdayPlanBase",
    "WorkdayPlanCreate",
    "WorkdayPlanUpdate",
    "WorkdayPlanRead",
    "WorkdayPlanDetailRead",
    "WorkdayOptimizationResult",
    "OrderBase",
    "OrderCreate",
    "OrderRead",
    "RouteStopBase",
    "RouteStopCreate",
    "RouteStopTelemetryUpdate",
    "RouteStopRead",
    "SimulationEventBase",
    "SimulationEventCreate",
    "SimulationEventRead",
    "TrafficIncidentInjectionRequest",
    "UrgentOrderInjectionRequest",
    "EventInjectionAck",
    "EligibleUrgentOrderNode",
    "NetworkNode",
    "NetworkGraph",
    "RouteLegGeometry",
    "WorkdayRouteGeometry",
]
