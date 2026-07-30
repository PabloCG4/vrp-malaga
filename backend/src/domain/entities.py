"""
Core domain entities for the VRP solver.

This module defines the vocabulary the solver reasons about: the customers to
serve, the vehicles available to serve them, the routes a single vehicle
drives, and the workday problem instance and candidate solution built from
these building blocks. It has no dependency on the topology package: a
`Route` stores only the customer identifiers it visits, and the depot is
deliberately treated as a property of the street network (the node occupying
index 0 of a `CostMatrix`) rather than of the domain model, so this module can
be tested and reasoned about in complete isolation from OSMnx, NetworkX or
NumPy.

The model extends the standard Capacitated VRP with four realistic
operational features that the route clock simulator in
`backend/src/solver/evaluator.py` is responsible for enforcing:

- Soft delivery time windows (`Customer.time_window`), which penalize but do
  not forbid a late arrival.
- Per-stop service time (`Customer.service_time_seconds`), which advances the
  route clock before the vehicle may depart towards its next stop.
- European Union driver rest regulations (`Vehicle.max_workday_seconds`,
  `Vehicle.max_continuous_driving_seconds`, `Vehicle.mandatory_break_seconds`),
  modelled after Regulation (EC) No 561/2006: a driver may not drive for more
  than 4.5 continuous hours without a 45-minute break, and an 8-hour workday
  is treated as the vehicle's hard operating budget for the day.
- Mid-day pickup-and-delivery pairs (`Customer.is_pickup_stop`,
  `Customer.paired_customer_id`), used by Phase 3's dynamic simulation to
  model an urgent order: a same-day parcel is first picked up at the depot
  and then carried to its delivery customer, and the pickup must be visited
  before its paired delivery, on the same route.

`Customer.customer_id` is this model's identity for a stop, deliberately
distinct from `Customer.node_id`, its physical location. Every stop used to
assume one customer per node, which a pickup-and-delivery pair breaks
immediately: every pickup stop of every urgent order is physically located at
the same depot node, so `node_id` alone can no longer serve as a unique key.
An ordinary, single-stop customer never has to think about this distinction,
since `customer_id` defaults to `str(node_id)` when omitted.

Every entity is an immutable, frozen dataclass. This is the design decision
that makes cloning a `VRPState` during neighborhood search both fast and
memory-efficient: a local move that only changes one route can build a new
state by replacing a single element of the `routes` tuple, while every other
`Route` object is shared, by reference, with the previous state. No search
operator can corrupt a state another part of the search still holds, since
nothing in this module can be mutated in place after construction.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Mapping


class DomainError(Exception):
    """Base class for every error raised by the VRP domain layer."""


class DuplicateCustomerIdentifierError(DomainError):
    """Raised when two customers of the same workday share a customer identifier."""


class DuplicateVehicleIdentifierError(DomainError):
    """Raised when two vehicles of the same fleet share a vehicle identifier."""


class UnknownVehicleError(DomainError):
    """Raised when a route references a vehicle absent from the workday fleet."""


class UnknownCustomerError(DomainError):
    """Raised when a route visits a customer that is not part of the workday."""


class InvalidPickupDeliveryPairingError(DomainError):
    """Raised when a pickup/delivery pairing between two customers is inconsistent.

    This covers a `paired_customer_id` referencing an unknown customer, a
    pairing that is not mutual (each side must point back at the other), and
    a pairing where the two sides do not disagree on `is_pickup_stop` (a pair
    must have exactly one pickup and one delivery side).
    """


class DuplicateCustomerAssignmentError(DomainError):
    """Raised when a state assigns the same customer to a route more than once."""


class IncompleteCoverageError(DomainError):
    """Raised when a state does not assign every workday customer to a route."""


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """
    A soft delivery time window, in seconds elapsed since the workday start.

    The window is soft by design: arriving before `start_seconds` merely
    forces the vehicle to wait, and arriving after `end_seconds` is legal but
    incurs a penalty proportional to the lateness, applied by the evaluator.
    A customer with no real restriction can be modelled with
    `TimeWindow(0.0, math.inf)`.

    Attributes
    ----------
    start_seconds:
        Earliest time, relative to workday start, at which service may begin.
    end_seconds:
        Latest time, relative to workday start, at which the vehicle should
        ideally have arrived. Arriving later remains possible but is penalized.
    """

    start_seconds: float
    end_seconds: float

    def __post_init__(self) -> None:
        if self.start_seconds < 0.0:
            message = f"Time window start ({self.start_seconds}) cannot be negative."
            raise ValueError(message)
        if self.end_seconds < self.start_seconds:
            message = (
                f"Time window end ({self.end_seconds}) cannot precede its start "
                f"({self.start_seconds})."
            )
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class Customer:
    """
    A single stop that must be visited exactly once during the workday.

    Attributes
    ----------
    node_id:
        Identifier of the street network node this stop is located at, as
        used by the graph produced by the topology package and by the
        `CostMatrix` built from it. Not necessarily unique: every pickup stop
        of a mid-day urgent order shares the depot's node identifier.
    demand:
        Capacity units this stop consumes from the vehicle that serves it,
        for example kilograms of weight or cubic meters of volume. The unit is
        a modelling choice of the caller and must stay consistent with
        `Vehicle.max_capacity`. A pickup stop typically carries zero demand
        itself, since the load it adds is realized at its paired delivery.
    service_time_seconds:
        Time the vehicle must spend at this stop performing the handover
        itself (unloading, handover, paperwork), before it may depart
        towards its next stop. Does not include any waiting time incurred by
        arriving before `time_window.start_seconds`.
    time_window:
        Soft delivery time window for this stop, relative to workday start.
        Defaults to an unrestricted window covering the whole day.
    customer_id:
        Unique identifier of this stop within a `WorkdayInstance`, distinct
        from `node_id`. Defaults to `str(node_id)` when omitted, which keeps
        every ordinary, single-stop-per-node customer unchanged: this field
        only needs to be supplied explicitly when two stops share a node, as
        happens with pickup-and-delivery pairs.
    is_pickup_stop:
        Whether this stop is the pickup side of a pickup-and-delivery pair.
        `False` for an ordinary customer and for the delivery side of a pair.
    paired_customer_id:
        For the delivery side of a pair, the `customer_id` of its required
        pickup stop; for the pickup side, the `customer_id` of its delivery
        stop. `None` for an ordinary customer with no pairing requirement.
    """

    node_id: int
    demand: float
    service_time_seconds: float = 0.0
    time_window: TimeWindow = field(default_factory=lambda: TimeWindow(0.0, math.inf))
    customer_id: str | None = None
    is_pickup_stop: bool = False
    paired_customer_id: str | None = None

    def __post_init__(self) -> None:
        if self.customer_id is None:
            object.__setattr__(self, "customer_id", str(self.node_id))
        if self.demand < 0.0:
            message = f"Customer '{self.customer_id}' has a negative demand ({self.demand})."
            raise ValueError(message)
        if self.service_time_seconds < 0.0:
            message = (
                f"Customer '{self.customer_id}' has a negative service time "
                f"({self.service_time_seconds})."
            )
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class Vehicle:
    """
    A single vehicle available to the fleet for the workday.

    The three time-related attributes model the driving and rest limits of
    European Union Regulation (EC) No 561/2006: a driver must take a break of
    `mandatory_break_seconds` after at most `max_continuous_driving_seconds`
    of uninterrupted driving, and the vehicle's entire workday, driving plus
    waiting plus service plus breaks, is bounded by `max_workday_seconds`.

    Attributes
    ----------
    vehicle_id:
        Unique identifier of the vehicle within the fleet.
    max_capacity:
        Maximum load, expressed in the same capacity units as
        `Customer.demand`, that this vehicle may carry at once.
    max_workday_seconds:
        Maximum duration of the vehicle's entire workday, from departing the
        depot to returning to it, including driving, waiting, service and
        rest break time. Defaults to 28800.0 seconds (8 hours).
    max_continuous_driving_seconds:
        Maximum driving time the vehicle may accumulate before a mandatory
        rest break resets the counter. Defaults to 16200.0 seconds (4.5
        hours), the EU regulatory limit.
    mandatory_break_seconds:
        Duration of the rest break the driver must take once
        `max_continuous_driving_seconds` would otherwise be exceeded.
        Defaults to 2700.0 seconds (45 minutes), the EU regulatory minimum.
    """

    vehicle_id: str
    max_capacity: float
    max_workday_seconds: float = 28800.0
    max_continuous_driving_seconds: float = 16200.0
    mandatory_break_seconds: float = 2700.0

    def __post_init__(self) -> None:
        if self.max_capacity <= 0.0:
            message = (
                f"Vehicle '{self.vehicle_id}' has a non-positive capacity ({self.max_capacity})."
            )
            raise ValueError(message)
        if self.max_workday_seconds <= 0.0:
            message = (
                f"Vehicle '{self.vehicle_id}' has a non-positive maximum workday duration "
                f"({self.max_workday_seconds})."
            )
            raise ValueError(message)
        if self.max_continuous_driving_seconds <= 0.0:
            message = (
                f"Vehicle '{self.vehicle_id}' has a non-positive maximum continuous driving "
                f"time ({self.max_continuous_driving_seconds})."
            )
            raise ValueError(message)
        if self.mandatory_break_seconds < 0.0:
            message = (
                f"Vehicle '{self.vehicle_id}' has a negative mandatory break duration "
                f"({self.mandatory_break_seconds})."
            )
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class Route:
    """
    Ordered sequence of customers assigned to a single vehicle for the workday.

    A route stores customer identifiers only; the depot is intentionally
    excluded from `customer_sequence`, since which node acts as the depot is a
    property of the `CostMatrix` the route is evaluated against, not of the
    route itself. `full_node_sequence` reattaches the depot on demand, at both
    ends, for cost evaluation or for streaming the vehicle's path to a client.

    Attributes
    ----------
    vehicle_id:
        Identifier of the vehicle that drives this route.
    customer_sequence:
        Ordered tuple of customer identifiers (`Customer.customer_id`, not
        `Customer.node_id`) visited by the vehicle. Empty when the vehicle is
        not dispatched during the workday.
    """

    vehicle_id: str
    customer_sequence: tuple[str, ...] = ()

    @property
    def is_dispatched(self) -> bool:
        """Return whether this route visits at least one customer."""
        return len(self.customer_sequence) > 0

    def full_node_sequence(
        self, depot_node: int, customers_by_id: Mapping[str, Customer]
    ) -> tuple[int, ...]:
        """
        Return the complete node sequence driven by the vehicle, depot included.

        Parameters
        ----------
        depot_node:
            Graph node identifier the vehicle departs from and returns to,
            normally read from `CostMatrix.depot_node`.
        customers_by_id:
            Lookup of every customer of the workday, keyed by customer
            identifier, normally `WorkdayInstance.customers_by_id`, used to
            resolve each stop in `customer_sequence` to its physical node.

        Returns
        -------
        tuple[int, ...]
            The depot, followed by every customer's node in visiting order,
            followed by the depot again. For an undispatched route, this
            collapses to the two-element round trip `(depot_node,
            depot_node)`, which a `CostMatrix` prices at exactly zero through
            its zero diagonal.

        Raises
        ------
        UnknownCustomerError
            If the route visits a customer identifier absent from
            `customers_by_id`.
        """
        try:
            visited_nodes = tuple(
                customers_by_id[customer_id].node_id for customer_id in self.customer_sequence
            )
        except KeyError as missing_customer_id:
            message = (
                f"Route of vehicle '{self.vehicle_id}' visits customer "
                f"'{missing_customer_id.args[0]}', which is not a customer of this workday."
            )
            raise UnknownCustomerError(message) from missing_customer_id
        return (depot_node, *visited_nodes, depot_node)

    def total_demand(self, customers_by_id: Mapping[str, Customer]) -> float:
        """
        Sum the demand of every customer visited by this route.

        Parameters
        ----------
        customers_by_id:
            Lookup of every customer of the workday, keyed by customer
            identifier, normally `WorkdayInstance.customers_by_id`.

        Returns
        -------
        float
            Total capacity units this route would load onto its vehicle.

        Raises
        ------
        UnknownCustomerError
            If the route visits a customer identifier absent from
            `customers_by_id`.
        """
        try:
            return sum(customers_by_id[customer_id].demand for customer_id in self.customer_sequence)
        except KeyError as missing_customer_id:
            message = (
                f"Route of vehicle '{self.vehicle_id}' visits customer "
                f"'{missing_customer_id.args[0]}', which is not a customer of this workday."
            )
            raise UnknownCustomerError(message) from missing_customer_id


@dataclass(frozen=True, slots=True)
class WorkdayInstance:
    """
    Immutable definition of one day's VRP problem: the fleet and its customers.

    A `WorkdayInstance` never changes while a metaheuristic explores candidate
    solutions; only the `VRPState`, the assignment of customers to routes,
    does. Separating the two means every candidate state can stay a
    lightweight object that references a single, shared `WorkdayInstance`
    instead of duplicating customer and vehicle data on every clone.

    Attributes
    ----------
    customers:
        Every customer that must be served during the workday.
    fleet:
        Every vehicle available to serve them.
    customers_by_id:
        Reverse lookup of `customers` by customer identifier, built once here
        so that the evaluator never has to scan `customers` linearly. Node
        identifiers may repeat across customers (every pickup stop of an
        urgent order shares the depot's node), so `customer_id` rather than
        `node_id` is the only safe key for this lookup.
    fleet_by_vehicle_id:
        Reverse lookup of `fleet` by vehicle identifier, built once here for
        the same reason.
    """

    customers: tuple[Customer, ...]
    fleet: tuple[Vehicle, ...]
    customers_by_id: dict[str, Customer] = field(init=False, repr=False)
    fleet_by_vehicle_id: dict[str, Vehicle] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        customers_by_id: dict[str, Customer] = {}
        for customer in self.customers:
            customer_id = customer.customer_id
            assert customer_id is not None  # Always populated by Customer.__post_init__.
            customers_by_id[customer_id] = customer
        if len(customers_by_id) != len(self.customers):
            message = "Two or more customers of this workday share the same customer identifier."
            raise DuplicateCustomerIdentifierError(message)

        fleet_by_vehicle_id = {vehicle.vehicle_id: vehicle for vehicle in self.fleet}
        if len(fleet_by_vehicle_id) != len(self.fleet):
            message = "Two or more vehicles of this fleet share the same vehicle identifier."
            raise DuplicateVehicleIdentifierError(message)

        # The dataclass is frozen, so these derived lookups are attached through
        # object.__setattr__ instead of plain attribute assignment.
        object.__setattr__(self, "customers_by_id", customers_by_id)
        object.__setattr__(self, "fleet_by_vehicle_id", fleet_by_vehicle_id)

        self._validate_pickup_delivery_pairings(customers_by_id)

    @staticmethod
    def _validate_pickup_delivery_pairings(customers_by_id: Mapping[str, Customer]) -> None:
        """
        Verify that every `paired_customer_id` reference forms a consistent pair.

        For every customer that declares a pairing, this checks that the
        referenced customer exists, that the pairing is mutual (each side
        points back at the other), and that exactly one side of the pair is
        marked as the pickup stop.

        Raises
        ------
        InvalidPickupDeliveryPairingError
            If any of the three conditions above is violated.
        """
        for customer_id, customer in customers_by_id.items():
            paired_id = customer.paired_customer_id
            if paired_id is None:
                continue

            paired_customer = customers_by_id.get(paired_id)
            if paired_customer is None:
                message = (
                    f"Customer '{customer_id}' is paired with unknown customer '{paired_id}'."
                )
                raise InvalidPickupDeliveryPairingError(message)

            if paired_customer.paired_customer_id != customer_id:
                message = (
                    f"Pairing between '{customer_id}' and '{paired_id}' is not mutual: "
                    f"'{paired_id}' points to '{paired_customer.paired_customer_id}' instead."
                )
                raise InvalidPickupDeliveryPairingError(message)

            if customer.is_pickup_stop == paired_customer.is_pickup_stop:
                message = (
                    f"Pair ('{customer_id}', '{paired_id}') must have exactly one pickup stop "
                    "and one delivery stop, but both sides agree on is_pickup_stop="
                    f"{customer.is_pickup_stop}."
                )
                raise InvalidPickupDeliveryPairingError(message)

    @property
    def customer_ids(self) -> frozenset[str]:
        """Return the set of every customer identifier of the workday."""
        return frozenset(self.customers_by_id)


@dataclass(frozen=True, slots=True)
class VRPState:
    """
    A complete candidate solution: every fleet route for the workday.

    Attributes
    ----------
    routes:
        One route per vehicle considered by the search. A vehicle that is not
        dispatched should still be represented by an empty route rather than
        omitted, so `routes` always has one entry per candidate vehicle.
    """

    routes: tuple[Route, ...]

    def with_route_replaced(self, route_index: int, new_route: Route) -> "VRPState":
        """
        Return a new state with a single route replaced, sharing the rest.

        This is the primary building block for neighborhood search moves: an
        operator that changes one route, for example by relocating a customer
        or reversing a segment, can produce its resulting state in time
        proportional to the size of `routes`, without copying or re-validating
        any route it did not touch.

        Parameters
        ----------
        route_index:
            Position, within `routes`, of the route to replace.
        new_route:
            Route to place at that position.

        Returns
        -------
        VRPState
            A new state whose `routes` tuple equals this one except at
            `route_index`. Every other `Route` object is the exact same
            instance as before, not a copy.
        """
        updated_routes = self.routes[:route_index] + (new_route,) + self.routes[route_index + 1 :]

        return VRPState(routes=updated_routes)

    def visited_customer_ids(self) -> list[str]:
        """
        Return every customer identifier visited across all routes, duplicates included.

        Returns
        -------
        list[str]
            Concatenation of every route's `customer_sequence`, in route
            order. Duplicates are preserved intentionally, so that callers can
            detect a customer visited more than once.
        """
        return [customer_id for route in self.routes for customer_id in route.customer_sequence]

    def validate_customer_coverage(self, workday: WorkdayInstance) -> None:
        """
        Verify that this state serves every workday customer exactly once.

        A `VRPState` is defined as a complete candidate solution, so a state
        that leaves a customer unserved, serves one twice, or visits an
        identifier that is not even a customer of the workday, does not
        represent a valid instance of this type; such a state indicates a bug
        in whatever constructed it, and is therefore reported by raising
        rather than by a soft, gradient-based penalty.

        Parameters
        ----------
        workday:
            Problem instance this state is meant to solve.

        Raises
        ------
        UnknownCustomerError
            If a route visits a customer identifier that is not part of the
            workday.
        DuplicateCustomerAssignmentError
            If a customer is assigned to more than one route, or twice within
            the same route.
        IncompleteCoverageError
            If a customer of the workday is not visited by any route.
        """
        visited_customer_ids = self.visited_customer_ids()

        unknown_customer_ids = set(visited_customer_ids) - workday.customer_ids
        if unknown_customer_ids:
            message = (
                "The state visits customers that are not workday customers: "
                f"{sorted(unknown_customer_ids)}."
            )
            raise UnknownCustomerError(message)

        visit_counts = Counter(visited_customer_ids)
        duplicated_customer_ids = [
            customer_id for customer_id, count in visit_counts.items() if count > 1
        ]
        if duplicated_customer_ids:
            message = (
                "The following customers are assigned to more than one route: "
                f"{sorted(duplicated_customer_ids)}."
            )
            raise DuplicateCustomerAssignmentError(message)

        unserved_customer_ids = workday.customer_ids - set(visited_customer_ids)
        if unserved_customer_ids:
            message = (
                f"The following customers are not served by any route: {sorted(unserved_customer_ids)}."
            )
            raise IncompleteCoverageError(message)
