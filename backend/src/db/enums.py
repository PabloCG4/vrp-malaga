"""
Enumerations shared by the persistence layer's ORM models and API schemas.

Defining these enumerations independently of `backend/src/db/models.py`
allows `backend/src/api/schemas/` to import the same vocabulary without
depending on SQLAlchemy, keeping the Pydantic DTOs a pure data-validation
layer.
"""

from __future__ import annotations

import enum


class WorkdayStatus(str, enum.Enum):
    """Lifecycle status of a `WorkdayPlan`."""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"


class RouteStopType(str, enum.Enum):
    """Role a `RouteStop` plays within its vehicle's visiting sequence."""

    DEPOT_START = "DEPOT_START"
    DEPOT_PICKUP = "DEPOT_PICKUP"
    CUSTOMER_DELIVERY = "CUSTOMER_DELIVERY"
    DEPOT_END = "DEPOT_END"


class SimulationEventType(str, enum.Enum):
    """Kind of dynamic disruption recorded by a `SimulationEvent`."""

    TRAFFIC_INCIDENT = "TRAFFIC_INCIDENT"
    URGENT_ORDER = "URGENT_ORDER"
