"""
Core domain entities for the VRP solver.

This module defines the vocabulary the solver reasons about: the customers to
serve, the vehicles available to serve them, the routes a single vehicle
drives, and the workday problem instance and candidate solution built from
these building blocks. It has no dependency on the topology package: a
`Route` stores only the customer node identifiers it visits, and the depot is
deliberately treated as a property of the street network (the node occupying
index 0 of a `CostMatrix`) rather than of the domain model, so this module can
be tested and reasoned about in complete isolation from OSMnx, NetworkX or
NumPy.

Every entity is an immutable, frozen dataclass. This is the design decision
that makes cloning a `VRPState` during neighborhood search both fast and
memory-efficient: a local move that only changes one route can build a new
state by replacing a single element of the `routes` tuple, while every other
`Route` object is shared, by reference, with the previous state. No search
operator can corrupt a state another part of the search still holds, since
nothing in this module can be mutated in place after construction.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Mapping


class DomainError(Exception):
    """Base class for every error raised by the VRP domain layer."""


class DuplicateNodeIdentifierError(DomainError):
    """Raised when two customers of the same workday share a node identifier."""


class DuplicateVehicleIdentifierError(DomainError):
    """Raised when two vehicles of the same fleet share a vehicle identifier."""


class UnknownVehicleError(DomainError):
    """Raised when a route references a vehicle absent from the workday fleet."""


class UnknownCustomerError(DomainError):
    """Raised when a route visits a node that is not a customer of the workday."""


class DuplicateCustomerAssignmentError(DomainError):
    """Raised when a state assigns the same customer to a route more than once."""


class IncompleteCoverageError(DomainError):
    """Raised when a state does not assign every workday customer to a route."""


@dataclass(frozen=True, slots=True)
class Customer:
    """
    A single delivery point that must be visited exactly once during the workday.

    Attributes
    ----------
    node_id:
        Identifier of the street network node this customer is located at, as
        used by the graph produced by the topology package and by the
        `CostMatrix` built from it.
    demand:
        Capacity units this customer consumes from the vehicle that serves it,
        for example kilograms of weight or cubic meters of volume. The unit is
        a modelling choice of the caller and must stay consistent with
        `Vehicle.max_capacity`.
    """

    node_id: int
    demand: float

    def __post_init__(self) -> None:
        if self.demand < 0.0:
            message = f"Customer at node {self.node_id} has a negative demand ({self.demand})."
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class Vehicle:
    """
    A single vehicle available to the fleet for the workday.

    Attributes
    ----------
    vehicle_id:
        Unique identifier of the vehicle within the fleet.
    max_capacity:
        Maximum load, expressed in the same capacity units as
        `Customer.demand`, that this vehicle may carry at once.
    """

    vehicle_id: str
    max_capacity: float

    def __post_init__(self) -> None:
        if self.max_capacity <= 0.0:
            message = (
                f"Vehicle '{self.vehicle_id}' has a non-positive capacity ({self.max_capacity})."
            )
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class Route:
    """
    Ordered sequence of customers assigned to a single vehicle for the workday.

    A route stores customer node identifiers only; the depot is intentionally
    excluded from `customer_sequence`, since which node acts as the depot is a
    property of the `CostMatrix` the route is evaluated against, not of the
    route itself. `full_node_sequence` reattaches the depot on demand, at both
    ends, for cost evaluation or for streaming the vehicle's path to a client.

    Attributes
    ----------
    vehicle_id:
        Identifier of the vehicle that drives this route.
    customer_sequence:
        Ordered tuple of customer node identifiers visited by the vehicle.
        Empty when the vehicle is not dispatched during the workday.
    """

    vehicle_id: str
    customer_sequence: tuple[int, ...] = ()

    @property
    def is_dispatched(self) -> bool:
        """Return whether this route visits at least one customer."""
        return len(self.customer_sequence) > 0

    def full_node_sequence(self, depot_node: int) -> tuple[int, ...]:
        """
        Return the complete node sequence driven by the vehicle, depot included.

        Parameters
        ----------
        depot_node:
            Graph node identifier the vehicle departs from and returns to,
            normally read from `CostMatrix.depot_node`.

        Returns
        -------
        tuple[int, ...]
            The depot, followed by every customer in visiting order, followed
            by the depot again. For an undispatched route, this collapses to
            the two-element round trip `(depot_node, depot_node)`, which a
            `CostMatrix` prices at exactly zero through its zero diagonal.
        """
        return (depot_node, *self.customer_sequence, depot_node)

    def total_demand(self, customers_by_node_id: Mapping[int, Customer]) -> float:
        """
        Sum the demand of every customer visited by this route.

        Parameters
        ----------
        customers_by_node_id:
            Lookup of every customer of the workday, keyed by node identifier,
            normally `WorkdayInstance.customers_by_node_id`.

        Returns
        -------
        float
            Total capacity units this route would load onto its vehicle.

        Raises
        ------
        UnknownCustomerError
            If the route visits a node absent from `customers_by_node_id`.
        """
        try:
            return sum(customers_by_node_id[node_id].demand for node_id in self.customer_sequence)
        except KeyError as missing_customer_node:
            message = (
                f"Route of vehicle '{self.vehicle_id}' visits node {missing_customer_node.args[0]}, "
                "which is not a customer of this workday."
            )
            raise UnknownCustomerError(message) from missing_customer_node


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
    customers_by_node_id:
        Reverse lookup of `customers` by node identifier, built once here so
        that the evaluator never has to scan `customers` linearly.
    fleet_by_vehicle_id:
        Reverse lookup of `fleet` by vehicle identifier, built once here for
        the same reason.
    """

    customers: tuple[Customer, ...]
    fleet: tuple[Vehicle, ...]
    customers_by_node_id: dict[int, Customer] = field(init=False, repr=False)
    fleet_by_vehicle_id: dict[str, Vehicle] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        customers_by_node_id = {customer.node_id: customer for customer in self.customers}
        if len(customers_by_node_id) != len(self.customers):
            message = "Two or more customers of this workday share the same node identifier."
            raise DuplicateNodeIdentifierError(message)

        fleet_by_vehicle_id = {vehicle.vehicle_id: vehicle for vehicle in self.fleet}
        if len(fleet_by_vehicle_id) != len(self.fleet):
            message = "Two or more vehicles of this fleet share the same vehicle identifier."
            raise DuplicateVehicleIdentifierError(message)

        # The dataclass is frozen, so these derived lookups are attached through
        # object.__setattr__ instead of plain attribute assignment.
        object.__setattr__(self, "customers_by_node_id", customers_by_node_id)
        object.__setattr__(self, "fleet_by_vehicle_id", fleet_by_vehicle_id)

    @property
    def customer_node_ids(self) -> frozenset[int]:
        """Return the set of every customer node identifier of the workday."""
        return frozenset(self.customers_by_node_id)


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

    def visited_customer_node_ids(self) -> list[int]:
        """
        Return every customer node visited across all routes, duplicates included.

        Returns
        -------
        list[int]
            Concatenation of every route's `customer_sequence`, in route
            order. Duplicates are preserved intentionally, so that callers can
            detect a customer visited more than once.
        """
        return [node_id for route in self.routes for node_id in route.customer_sequence]

    def validate_customer_coverage(self, workday: WorkdayInstance) -> None:
        """
        Verify that this state serves every workday customer exactly once.

        A `VRPState` is defined as a complete candidate solution, so a state
        that leaves a customer unserved, serves one twice, or visits a node
        that is not even a customer of the workday, does not represent a valid
        instance of this type; such a state indicates a bug in whatever
        constructed it, and is therefore reported by raising rather than by a
        soft, gradient-based penalty.

        Parameters
        ----------
        workday:
            Problem instance this state is meant to solve.

        Raises
        ------
        UnknownCustomerError
            If a route visits a node that is not a customer of the workday.
        DuplicateCustomerAssignmentError
            If a customer is assigned to more than one route, or twice within
            the same route.
        IncompleteCoverageError
            If a customer of the workday is not visited by any route.
        """
        visited_node_ids = self.visited_customer_node_ids()

        unknown_node_ids = set(visited_node_ids) - workday.customer_node_ids
        if unknown_node_ids:
            message = (
                f"The state visits nodes that are not workday customers: {sorted(unknown_node_ids)}."
            )
            raise UnknownCustomerError(message)

        visit_counts = Counter(visited_node_ids)
        duplicated_node_ids = [node_id for node_id, count in visit_counts.items() if count > 1]
        if duplicated_node_ids:
            message = (
                "The following customers are assigned to more than one route: "
                f"{sorted(duplicated_node_ids)}."
            )
            raise DuplicateCustomerAssignmentError(message)

        unserved_node_ids = workday.customer_node_ids - set(visited_node_ids)
        if unserved_node_ids:
            message = f"The following customers are not served by any route: {sorted(unserved_node_ids)}."
            raise IncompleteCoverageError(message)
