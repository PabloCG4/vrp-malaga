"""
Async SQLAlchemy 2.0 ORM models for the Rich VRP solver's Control Tower database.

This module is the persistence counterpart of the in-memory domain model of
`backend/src/domain/entities.py`: whereas that module models a single
workday's optimization problem as an immutable, in-memory object graph for
the solver to search over, this module models the durable record of every
workday ever planned, so that a dispatcher-facing Control Tower dashboard can
list past and current plans, inspect planned-versus-actual delivery
telemetry, and audit every dynamic event that triggered a re-optimization.

Entity-relationship overview
-----------------------------
- `Driver` and `Vehicle` describe the fleet. A `Vehicle` may name a `Driver`
  as its usual operator (`default_driver_id`), independent of which
  `WorkdayPlan` it is scheduled on.
- `WorkdayPlan` is the root aggregate for a single simulated day: it owns the
  `Order`s to be served, the `RouteStop`s of the resulting plan, and the
  `SimulationEvent`s that occurred while it ran, all removed automatically
  when the plan itself is deleted (`ondelete="CASCADE"`).
- `Order` supports both a standard, single-stop delivery and a mid-day
  urgent order modelled as a pickup-and-delivery (VRPPD) pair: the depot
  pickup leg and the customer delivery leg are each their own `Order` row,
  linked through `paired_order_id`, mirroring
  `domain.entities.Customer.paired_customer_id`.
- `RouteStop` records the planned sequence of every vehicle, depot legs
  included (`order_id` is `NULL` for `DEPOT_START`/`DEPOT_END`), and is later
  filled in with `actual_arrival_seconds`/`departure_seconds` as the live
  simulation or field telemetry progresses, which is what makes
  planned-versus-actual reporting possible.
- `SimulationEvent` is an append-only audit log of every dynamic disruption
  (`simulation.events.TrafficIncidentEvent`, `simulation.events.UrgentOrderEvent`)
  injected into a workday, keeping the whole run reproducible.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, TimestampWithUpdateMixin
from .enums import RouteStopType, SimulationEventType, WorkdayStatus

# A portable JSON column: rendered as native JSONB on PostgreSQL (indexable,
# binary-stored) and falling back to the ordinary JSON type understood by
# SQLite, so `simulation_events.payload_json` works identically on both the
# production database and the local/testing backend.
PortableJSON = JSON().with_variant(JSONB(), "postgresql")

# European Union Regulation (EC) No 561/2006's continuous driving limit
# before a mandatory rest break, in seconds (4.5 hours), used as this
# schema's default for `Driver.max_continuous_driving_seconds`.
EU_MAX_CONTINUOUS_DRIVING_SECONDS: int = 16200

# Default soft delivery time window, in seconds elapsed since the workday
# starts at 08:00, matching `domain.entities.Vehicle`'s default 8-hour
# workday budget.
DEFAULT_WORKDAY_DURATION_SECONDS: int = 28800
DEFAULT_ORDER_SERVICE_TIME_SECONDS: int = 300


class Driver(Base, TimestampMixin):
    """
    A person licensed to operate a vehicle of the fleet.

    `max_continuous_driving_seconds` records this driver's applicable EU
    Regulation (EC) No 561/2006 continuous-driving limit, mirroring
    `domain.entities.Vehicle.max_continuous_driving_seconds`, which the
    solver enforces during route simulation.
    """

    __tablename__ = "drivers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    license_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    max_continuous_driving_seconds: Mapped[int] = mapped_column(
        Integer, default=EU_MAX_CONTINUOUS_DRIVING_SECONDS, server_default=str(EU_MAX_CONTINUOUS_DRIVING_SECONDS), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", nullable=False)

    default_vehicles: Mapped[list["Vehicle"]] = relationship(
        "Vehicle", back_populates="default_driver", foreign_keys="Vehicle.default_driver_id"
    )

    def __repr__(self) -> str:
        return f"Driver(id={self.id!r}, full_name={self.full_name!r}, license_number={self.license_number!r})"


class Vehicle(Base, TimestampMixin):
    """A single van or truck available to the fleet."""

    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    license_plate: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    capacity_kg: Mapped[float] = mapped_column(Float, nullable=False)
    default_driver_id: Mapped[int | None] = mapped_column(
        ForeignKey("drivers.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", nullable=False)

    default_driver: Mapped[Driver | None] = relationship(
        "Driver", back_populates="default_vehicles", foreign_keys=[default_driver_id]
    )
    route_stops: Mapped[list["RouteStop"]] = relationship("RouteStop", back_populates="vehicle")

    def __repr__(self) -> str:
        return f"Vehicle(id={self.id!r}, license_plate={self.license_plate!r}, capacity_kg={self.capacity_kg!r})"


class WorkdayPlan(Base, TimestampWithUpdateMixin):
    """
    The root aggregate of a single simulated workday.

    `total_cost`, `total_distance_km` and `execution_time_ms` cache the final
    `evaluator.StateEvaluation`/`metaheuristic.TabuSearchResult` figures of
    the plan that was actually committed for the day, so the Control Tower
    dashboard can list historical plans without re-running the solver.
    """

    __tablename__ = "workday_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workday_date: Mapped[date] = mapped_column(Date, unique=True, nullable=False, index=True)
    status: Mapped[WorkdayStatus] = mapped_column(
        Enum(WorkdayStatus, name="workday_status", native_enum=True),
        default=WorkdayStatus.DRAFT,
        server_default=WorkdayStatus.DRAFT.value,
        nullable=False,
    )
    total_cost: Mapped[float] = mapped_column(Float, default=0.0, server_default="0.0", nullable=False)
    total_distance_km: Mapped[float] = mapped_column(Float, default=0.0, server_default="0.0", nullable=False)
    execution_time_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    orders: Mapped[list["Order"]] = relationship(
        "Order", back_populates="workday_plan", cascade="all, delete-orphan", passive_deletes=True
    )
    route_stops: Mapped[list["RouteStop"]] = relationship(
        "RouteStop", back_populates="workday_plan", cascade="all, delete-orphan", passive_deletes=True
    )
    simulation_events: Mapped[list["SimulationEvent"]] = relationship(
        "SimulationEvent", back_populates="workday_plan", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"WorkdayPlan(id={self.id!r}, workday_date={self.workday_date!r}, status={self.status!r})"


class Order(Base, TimestampMixin):
    """
    A delivery request belonging to a `WorkdayPlan`.

    A standard delivery is a single `Order` row with `is_urgent=False` and
    `paired_order_id=NULL`. A mid-day urgent order arriving while the
    workday is already underway is instead modelled as a genuine
    pickup-and-delivery (VRPPD) pair, exactly like
    `domain.entities.Customer.is_pickup_stop`/`paired_customer_id`: one
    `Order` row with `is_pickup_stop=True` located at the depot node, and one
    delivery `Order` row at the customer's node, each referencing the other
    through `paired_order_id`.
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workday_plan_id: Mapped[int] = mapped_column(
        ForeignKey("workday_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    node_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    demand_kg: Mapped[float] = mapped_column(Float, default=0.0, server_default="0.0", nullable=False)
    service_time_seconds: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_ORDER_SERVICE_TIME_SECONDS, server_default=str(DEFAULT_ORDER_SERVICE_TIME_SECONDS), nullable=False
    )
    time_window_start_seconds: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    time_window_end_seconds: Mapped[int] = mapped_column(
        Integer,
        default=DEFAULT_WORKDAY_DURATION_SECONDS,
        server_default=str(DEFAULT_WORKDAY_DURATION_SECONDS),
        nullable=False,
    )
    is_urgent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    is_pickup_stop: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    paired_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )

    workday_plan: Mapped[WorkdayPlan] = relationship("WorkdayPlan", back_populates="orders")
    paired_order: Mapped["Order | None"] = relationship(
        "Order", remote_side="Order.id", foreign_keys=[paired_order_id], uselist=False
    )
    route_stops: Mapped[list["RouteStop"]] = relationship("RouteStop", back_populates="order")

    def __repr__(self) -> str:
        return (
            f"Order(id={self.id!r}, customer_name={self.customer_name!r}, node_id={self.node_id!r}, "
            f"is_urgent={self.is_urgent!r}, is_pickup_stop={self.is_pickup_stop!r})"
        )


class RouteStop(Base):
    """
    A single stop of a vehicle's planned sequence within a `WorkdayPlan`.

    `planned_arrival_seconds` is filled in as soon as the plan is optimized,
    from `evaluator.RouteStopVisit.arrival_time_seconds`.
    `actual_arrival_seconds`/`departure_seconds` remain `NULL` until the live
    simulation or field telemetry reports the vehicle actually reaching this
    stop, which is what lets the Control Tower dashboard compare planned
    against real execution.
    """

    __tablename__ = "route_stops"
    __table_args__ = (
        UniqueConstraint(
            "workday_plan_id", "vehicle_id", "sequence_order", name="uq_route_stops_plan_vehicle_sequence"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workday_plan_id: Mapped[int] = mapped_column(
        ForeignKey("workday_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"), nullable=False, index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    sequence_order: Mapped[int] = mapped_column(Integer, nullable=False)
    stop_type: Mapped[RouteStopType] = mapped_column(
        Enum(RouteStopType, name="route_stop_type", native_enum=True), nullable=False
    )
    node_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    planned_arrival_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_arrival_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    departure_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    workday_plan: Mapped[WorkdayPlan] = relationship("WorkdayPlan", back_populates="route_stops")
    vehicle: Mapped[Vehicle] = relationship("Vehicle", back_populates="route_stops")
    order: Mapped[Order | None] = relationship("Order", back_populates="route_stops")

    def __repr__(self) -> str:
        return (
            f"RouteStop(id={self.id!r}, vehicle_id={self.vehicle_id!r}, "
            f"sequence_order={self.sequence_order!r}, stop_type={self.stop_type!r})"
        )


class SimulationEvent(Base, TimestampMixin):
    """
    Append-only audit log entry of a dynamic disruption injected into a workday.

    `payload_json` stores the event-specific metadata (for example, the two
    street nodes and reopen minute of a `TrafficIncidentEvent`, or the order
    identifier, delivery node and demand of an `UrgentOrderEvent`), keeping
    this table's structure stable regardless of which event type it records.
    """

    __tablename__ = "simulation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workday_plan_id: Mapped[int] = mapped_column(
        ForeignKey("workday_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[SimulationEventType] = mapped_column(
        Enum(SimulationEventType, name="simulation_event_type", native_enum=True), nullable=False
    )
    trigger_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[dict] = mapped_column(PortableJSON, nullable=False)

    workday_plan: Mapped[WorkdayPlan] = relationship("WorkdayPlan", back_populates="simulation_events")

    def __repr__(self) -> str:
        return (
            f"SimulationEvent(id={self.id!r}, event_type={self.event_type!r}, "
            f"trigger_minute={self.trigger_minute!r})"
        )
