"""
Pydantic v2 schemas (DTOs) for the live simulation REST event-injection endpoints.

The WebSocket telemetry stream itself (`GET /api/v1/workdays/{id}/live`) is
intentionally not modelled with a Pydantic response schema: FastAPI has no
first-class notion of a "WebSocket response model", and the message shapes
sent over that stream are documented instead in
`api/services/live_simulation.py`, next to the code that actually builds them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ...db.enums import SimulationEventType


class TrafficIncidentInjectionRequest(BaseModel):
    """Payload to inject a real-time traffic incident into a live simulation."""

    first_node: int = Field(..., description="First Malaga street network node bounding the closed street.")
    second_node: int = Field(..., description="Second Malaga street network node bounding the closed street.")
    reopen_after_minutes: int | None = Field(
        default=None,
        gt=0,
        description="Simulated minutes after which the street reopens, or None to keep it closed all day.",
    )
    description: str = Field(default="Traffic incident", min_length=1, max_length=200)


class UrgentOrderInjectionRequest(BaseModel):
    """Payload to inject a real-time, same-day urgent VRPPD order into a live simulation."""

    delivery_node: int = Field(..., description="Malaga street network node of the customer requesting delivery.")
    demand: float = Field(..., gt=0.0, description="Capacity units, in kilograms, the parcel consumes.")
    order_id: str | None = Field(
        default=None, description="Unique identifier for this order; generated automatically when omitted."
    )
    pickup_service_time_seconds: float = Field(
        default=180.0, ge=0.0, description="Handling time at the depot required to load the parcel."
    )
    delivery_service_time_seconds: float = Field(
        default=300.0, ge=0.0, description="Handling time at the customer required to hand the parcel over."
    )
    deadline_minutes_after_trigger: float = Field(
        default=90.0, gt=0.0, description="Soft delivery deadline, in minutes elapsed after the order arrives."
    )
    description: str = Field(default="Urgent order", min_length=1, max_length=200)


class EligibleUrgentOrderNode(BaseModel):
    """One node a live session's `POST .../events/urgent-order` call may legally target."""

    node_id: int = Field(..., description="Malaga street network node identifier.")
    latitude: float = Field(..., description="Node latitude, for placing it on a dispatcher map.")
    longitude: float = Field(..., description="Node longitude, for placing it on a dispatcher map.")


class EventInjectionAck(BaseModel):
    """Acknowledgement returned after a disruption has been dispatched into a live simulation."""

    workday_plan_id: int = Field(..., description="Workday plan the disruption was injected into.")
    event_type: SimulationEventType = Field(..., description="Kind of disruption that was dispatched.")
    trigger_minute: int = Field(..., ge=0, description="Simulated minute at which the disruption was applied.")
    order_id: str | None = Field(
        default=None, description="Identifier of the newly created urgent order, for urgent-order injections only."
    )
    message: str = Field(..., description="Human-readable summary of the outcome.")
