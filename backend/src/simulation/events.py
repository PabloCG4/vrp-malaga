"""
Dynamic events the simulation engine can inject during a workday.

Two kinds of disruption are modelled, matching the Phase 3 specification:

- `TrafficIncidentEvent`: a street becomes temporarily impassable, altering
  travel times for every route that depended on it.
- `UrgentOrderEvent`: a same-day delivery request arrives mid-day. Because
  the parcel is at the warehouse, serving it requires a genuine
  pickup-and-delivery workflow: a vehicle must first return to the depot to
  collect it before proceeding to the customer, modelled as two paired
  `Customer` stops (see `backend/src/domain/entities.py`).

Both event types are immutable and carry only plain data; all of the
behaviour they trigger (patching the `CostMatrix`, extending the
`WorkdayInstance`, invoking `run_tabu_search`) lives in
`simulation.engine.DynamicSimulator`, which keeps these dataclasses simple
enough to construct declaratively in a demonstration script or a test.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TrafficIncidentEvent:
    """
    A street closure triggered at a given simulated minute.

    A street connecting `first_node` and `second_node` is modelled, not a
    single directed edge, since a physical incident (an accident, roadworks)
    blocks a stretch of road regardless of the direction of travel, whereas
    the underlying graph represents each direction, and each parallel
    carriageway, as a separate directed edge. `DynamicSimulator` resolves the
    two nodes into the concrete set of directed edges to close via
    `backend.src.topology.matrix.find_street_edges`, which keeps this event
    agnostic of the graph's internal representation.

    Attributes
    ----------
    trigger_minute:
        Simulated minute, since workday start, at which the closure is
        applied.
    first_node, second_node:
        The two graph nodes bounding the closed street stretch.
    reopen_minute:
        Simulated minute at which the street is reopened, or `None` if the
        closure lasts for the remainder of the workday.
    description:
        Human-readable label used in telemetry and log output.
    """

    trigger_minute: int
    first_node: int
    second_node: int
    reopen_minute: int | None = None
    description: str = "Traffic incident"

    def __post_init__(self) -> None:
        if self.trigger_minute < 0:
            message = f"trigger_minute must be non-negative, got {self.trigger_minute}."
            raise ValueError(message)
        if self.reopen_minute is not None and self.reopen_minute <= self.trigger_minute:
            message = (
                f"reopen_minute ({self.reopen_minute}) must be strictly after trigger_minute "
                f"({self.trigger_minute})."
            )
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class UrgentOrderEvent:
    """
    A same-day delivery request requiring a depot pickup, arriving mid-day.

    The cargo for this order is not preloaded on any vehicle: it sits at the
    depot until a vehicle is routed back to collect it. `DynamicSimulator`
    therefore materializes this single event as two paired `Customer` stops,
    a depot pickup and a customer delivery, using `pickup_customer_id` and
    `delivery_customer_id` as their respective `Customer.customer_id` values.

    Attributes
    ----------
    trigger_minute:
        Simulated minute, since workday start, at which the order arrives.
    order_id:
        Unique identifier of this order, used to derive the customer
        identifiers of its two stops.
    delivery_node:
        Graph node identifier of the customer requesting the delivery.
    demand:
        Capacity units the parcel consumes.
    pickup_service_time_seconds:
        Handling time at the depot required to load the parcel.
    delivery_service_time_seconds:
        Handling time at the customer required to hand the parcel over.
    deadline_minutes_after_trigger:
        Soft delivery deadline, expressed as minutes elapsed after
        `trigger_minute`, from which the delivery stop's `TimeWindow` is
        derived once the order's arrival instant is known.
    description:
        Human-readable label used in telemetry and log output.
    """

    trigger_minute: int
    order_id: str
    delivery_node: int
    demand: float
    pickup_service_time_seconds: float = 180.0
    delivery_service_time_seconds: float = 300.0
    deadline_minutes_after_trigger: float = 90.0
    description: str = "Urgent order"

    def __post_init__(self) -> None:
        if self.trigger_minute < 0:
            message = f"trigger_minute must be non-negative, got {self.trigger_minute}."
            raise ValueError(message)
        if self.demand < 0.0:
            message = f"demand cannot be negative, got {self.demand}."
            raise ValueError(message)
        if self.pickup_service_time_seconds < 0.0 or self.delivery_service_time_seconds < 0.0:
            message = "Service times cannot be negative."
            raise ValueError(message)
        if self.deadline_minutes_after_trigger <= 0.0:
            message = (
                f"deadline_minutes_after_trigger must be positive, got "
                f"{self.deadline_minutes_after_trigger}."
            )
            raise ValueError(message)

    @property
    def pickup_customer_id(self) -> str:
        """Return the `Customer.customer_id` of this order's depot pickup stop."""
        return f"pickup-{self.order_id}"

    @property
    def delivery_customer_id(self) -> str:
        """Return the `Customer.customer_id` of this order's customer delivery stop."""
        return f"delivery-{self.order_id}"
