"""
Builds street-following polylines for a workday plan's planned route stops.

Uses the existing public `CostMatrix.path_between` API to reconstruct the
optimal node sequence between consecutive stops, then maps each intermediate
graph node to its (latitude, longitude) for Leaflet rendering. Prefer the
live simulation session's already-patched cost matrix when one is running, so
paths after traffic closures reflect the diverted street network rather than
the pristine baseline.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

import networkx as nx

from ...db.models import Order, RouteStop
from ...topology.matrix import CostMatrix, NodeNotInGraphError
from ..schemas.geometry import RouteLegGeometry, WorkdayRouteGeometry
from . import network_provider
from .live_simulation import live_simulation_manager
from .solver_bridge import build_cost_matrix_for_orders
from .workday_service import get_workday_plan


def _node_coordinates(street_network_graph: nx.MultiDiGraph, node_id: int) -> tuple[float, float] | None:
    """Return `(latitude, longitude)` for a graph node, or `None` if the node is missing."""
    node_data = street_network_graph.nodes.get(node_id)
    if node_data is None:
        return None
    return float(node_data["y"]), float(node_data["x"])


def _build_legs_from_cost_matrix(
    workday_plan_id: int,
    route_stops: list[RouteStop],
    cost_matrix: CostMatrix,
) -> WorkdayRouteGeometry:
    """Expand consecutive route stops into street-network polylines via `path_between`."""
    street_network_graph = cost_matrix.street_network_graph
    stops_by_vehicle: dict[int, list[RouteStop]] = defaultdict(list)
    for stop in route_stops:
        stops_by_vehicle[stop.vehicle_id].append(stop)

    legs: list[RouteLegGeometry] = []
    for vehicle_id, vehicle_stops in stops_by_vehicle.items():
        ordered_stops = sorted(vehicle_stops, key=lambda stop: stop.sequence_order)
        for index in range(len(ordered_stops) - 1):
            from_stop = ordered_stops[index]
            to_stop = ordered_stops[index + 1]
            try:
                path_node_ids = cost_matrix.path_between(from_stop.node_id, to_stop.node_id)
            except NodeNotInGraphError:
                # Stop node absent from this matrix (e.g. stale stop after a partial rewrite): skip.
                continue
            if len(path_node_ids) < 2:
                continue

            coordinates: list[tuple[float, float]] = []
            for node_id in path_node_ids:
                coordinate = _node_coordinates(street_network_graph, node_id)
                if coordinate is None:
                    coordinates = []
                    break
                coordinates.append(coordinate)
            if len(coordinates) < 2:
                continue

            legs.append(
                RouteLegGeometry(
                    vehicle_id=vehicle_id,
                    from_sequence_order=from_stop.sequence_order,
                    to_sequence_order=to_stop.sequence_order,
                    from_node_id=from_stop.node_id,
                    to_node_id=to_stop.node_id,
                    coordinates=coordinates,
                )
            )

    return WorkdayRouteGeometry(workday_plan_id=workday_plan_id, legs=legs)


def _build_offline_cost_matrix(orders: list[Order]) -> CostMatrix | None:
    """
    Build a fresh cost matrix covering the plan's orders.

    Returns `None` when there are no customer nodes outside the depot, since
    `build_cost_matrix` requires at least one distinct customer node.
    """
    # Load the graph before resolving the depot so a cold cache never hits
    # the historical `get_depot_node` → `get_street_network_graph` lock re-entry.
    street_network_graph = network_provider.get_street_network_graph()
    depot_node = network_provider.get_depot_node()
    distinct_customer_nodes = {order.node_id for order in orders if order.node_id != depot_node}
    if not distinct_customer_nodes:
        return None
    return build_cost_matrix_for_orders(street_network_graph, depot_node, orders)


async def build_workday_route_geometry(session, workday_plan_id: int) -> WorkdayRouteGeometry:
    """
    Return street-following polylines for every consecutive pair of route stops.

    Prefer the live session's patched `CostMatrix` when a simulation is
    already running for this plan; otherwise build a matrix from the plan's
    orders off the event loop via `asyncio.to_thread`.
    """
    workday_plan = await get_workday_plan(session, workday_plan_id)
    route_stops = list(workday_plan.route_stops)
    if not route_stops:
        return WorkdayRouteGeometry(workday_plan_id=workday_plan_id, legs=[])

    live_session = live_simulation_manager.get_session(workday_plan_id)
    if live_session is not None:
        async with live_session.lock:
            cost_matrix = live_session.simulator.cost_matrix
            return _build_legs_from_cost_matrix(workday_plan_id, route_stops, cost_matrix)

    orders = list(workday_plan.orders)

    def build_offline() -> WorkdayRouteGeometry:
        cost_matrix = _build_offline_cost_matrix(orders)
        if cost_matrix is None:
            return WorkdayRouteGeometry(workday_plan_id=workday_plan_id, legs=[])
        return _build_legs_from_cost_matrix(workday_plan_id, route_stops, cost_matrix)

    return await asyncio.to_thread(build_offline)
