"""
Real-time vehicle state derivation for the dynamic simulation engine.

`FleetTracker` never re-derives vehicle motion independently: it reuses the
per-stop `RouteStopVisit` timeline that `evaluator.evaluate_route` already
computed for the route's current customer sequence (the exact same clock
simulation the metaheuristic itself relies on), and simply asks, for a given
simulated instant, which phase of that timeline the vehicle currently
occupies. This is what keeps the minute-by-minute simulation loop cheap: no
route is ever re-simulated solely to answer "where is this vehicle right
now", only to decide whether to re-optimize it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

from ..domain.entities import Route
from ..solver.evaluator import RouteEvaluation, RouteStopVisit


class VehicleStatus(str, Enum):
    """Operational status of a vehicle at a given simulated instant."""

    DRIVING = "driving"
    WAITING = "waiting"
    SERVING = "serving"
    ON_BREAK = "on_break"
    IDLE_AT_DEPOT = "idle_at_depot"
    FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class VehicleSnapshot:
    """
    Real-time operational state of a single vehicle at a given instant.

    Attributes
    ----------
    vehicle_id:
        Identifier of the vehicle this snapshot describes.
    status:
        Current operational status.
    current_node:
        Graph node the vehicle currently occupies (its position while
        waiting, serving or on a break) or is departing from (while driving).
    next_node:
        Graph node the vehicle is currently driving towards, or `None` when
        not driving.
    active_customer_id:
        Customer identifier the vehicle is currently serving, waiting for, or
        on a break at, or, while driving, the customer it is travelling
        towards. `None` at the depot or once the route has finished.
    locked_prefix_length:
        Number of leading stops of the route's `customer_sequence` whose
        incoming leg has already been departed for, and which must therefore
        remain untouched by any subsequent `run_tabu_search` re-optimization.
    completed_customer_ids:
        Customer identifiers occupying the first `locked_prefix_length`
        positions of the route, for telemetry.
    """

    vehicle_id: str
    status: VehicleStatus
    current_node: int
    next_node: int | None
    active_customer_id: str | None
    locked_prefix_length: int
    completed_customer_ids: tuple[str, ...]


class FleetTracker:
    """
    Derives real-time vehicle state from an already computed `RouteEvaluation`.

    Every method is a pure function of a route, its evaluation and a
    simulated instant: `FleetTracker` holds no state of its own, which lets
    `DynamicSimulator` call it as often as needed (for example every minute
    tick) without worrying about it drifting out of sync with the rest of the
    simulation.
    """

    @staticmethod
    def compute_locked_prefix_length(
        stop_schedule: Sequence[RouteStopVisit], customer_count: int, current_time_seconds: float
    ) -> int:
        """
        Count the leading customer stops whose incoming leg has been departed.

        A stop is considered locked once the vehicle has already left the
        stop preceding it, since at that point the vehicle is committed to
        driving there next and no metaheuristic move could change that
        outcome without contradicting the vehicle's real, physical position.
        This mirrors the definition `run_tabu_search`'s own docstring already
        assumes for `locked_prefix_lengths`.

        Parameters
        ----------
        stop_schedule:
            Per-stop timeline of the route, depot included at both ends, as
            produced by `evaluator.evaluate_route`.
        customer_count:
            Number of customer stops on the route (`len(Route.customer_sequence)`).
        current_time_seconds:
            Current simulated instant, in seconds since workday start.

        Returns
        -------
        int
            Number of leading customer positions that must not be touched by
            a subsequent re-optimization.
        """
        locked_count = 0
        for position in range(customer_count):
            if stop_schedule[position].departure_time_seconds <= current_time_seconds:
                locked_count += 1
            else:
                break

        return locked_count

    @staticmethod
    def snapshot_vehicle(
        route: Route, route_evaluation: RouteEvaluation, current_time_seconds: float
    ) -> VehicleSnapshot:
        """
        Derive the real-time state of a single vehicle at `current_time_seconds`.

        Parameters
        ----------
        route:
            The vehicle's current route.
        route_evaluation:
            The route's `evaluator.evaluate_route` result, whose
            `stop_schedule` this method walks. Must have been computed with
            `build_schedule=True`.
        current_time_seconds:
            Current simulated instant, in seconds since workday start.

        Returns
        -------
        VehicleSnapshot
            The vehicle's derived operational state.
        """
        customer_count = len(route.customer_sequence)
        locked_prefix_length = FleetTracker.compute_locked_prefix_length(
            route_evaluation.stop_schedule, customer_count, current_time_seconds
        )
        completed_customer_ids = route.customer_sequence[:locked_prefix_length]

        if not route.is_dispatched:
            depot_node = route_evaluation.stop_schedule[0].node_id
            return VehicleSnapshot(
                vehicle_id=route.vehicle_id,
                status=VehicleStatus.IDLE_AT_DEPOT,
                current_node=depot_node,
                next_node=None,
                active_customer_id=None,
                locked_prefix_length=0,
                completed_customer_ids=(),
            )

        schedule = route_evaluation.stop_schedule
        for index, stop in enumerate(schedule):
            is_last_stop = index == len(schedule) - 1
            waiting_end_seconds = stop.arrival_time_seconds + stop.waiting_seconds
            service_end_seconds = waiting_end_seconds + stop.service_time_seconds

            if current_time_seconds < waiting_end_seconds:
                return VehicleSnapshot(
                    vehicle_id=route.vehicle_id,
                    status=VehicleStatus.WAITING,
                    current_node=stop.node_id,
                    next_node=None,
                    active_customer_id=stop.customer_id,
                    locked_prefix_length=locked_prefix_length,
                    completed_customer_ids=completed_customer_ids,
                )
            if current_time_seconds < service_end_seconds:
                return VehicleSnapshot(
                    vehicle_id=route.vehicle_id,
                    status=VehicleStatus.SERVING,
                    current_node=stop.node_id,
                    next_node=None,
                    active_customer_id=stop.customer_id,
                    locked_prefix_length=locked_prefix_length,
                    completed_customer_ids=completed_customer_ids,
                )
            if current_time_seconds < stop.departure_time_seconds:
                return VehicleSnapshot(
                    vehicle_id=route.vehicle_id,
                    status=VehicleStatus.ON_BREAK,
                    current_node=stop.node_id,
                    next_node=None,
                    active_customer_id=stop.customer_id,
                    locked_prefix_length=locked_prefix_length,
                    completed_customer_ids=completed_customer_ids,
                )

            if is_last_stop:
                return VehicleSnapshot(
                    vehicle_id=route.vehicle_id,
                    status=VehicleStatus.FINISHED,
                    current_node=stop.node_id,
                    next_node=None,
                    active_customer_id=None,
                    locked_prefix_length=locked_prefix_length,
                    completed_customer_ids=completed_customer_ids,
                )

            next_stop = schedule[index + 1]
            if current_time_seconds < next_stop.arrival_time_seconds:
                return VehicleSnapshot(
                    vehicle_id=route.vehicle_id,
                    status=VehicleStatus.DRIVING,
                    current_node=stop.node_id,
                    next_node=next_stop.node_id,
                    active_customer_id=next_stop.customer_id,
                    locked_prefix_length=locked_prefix_length,
                    completed_customer_ids=completed_customer_ids,
                )
            # The vehicle has already reached and departed the next stop too;
            # continue the scan forward from there.

        # Defensive fallback: a monotonically advancing clock should always be
        # resolved by the loop above, but a finished route at the exact final
        # timestamp falls through here rather than raising.
        final_stop = schedule[-1]
        return VehicleSnapshot(
            vehicle_id=route.vehicle_id,
            status=VehicleStatus.FINISHED,
            current_node=final_stop.node_id,
            next_node=None,
            active_customer_id=None,
            locked_prefix_length=locked_prefix_length,
            completed_customer_ids=completed_customer_ids,
        )

    @classmethod
    def snapshot_fleet(
        cls,
        routes: Sequence[Route],
        route_evaluations_by_vehicle_id: Mapping[str, RouteEvaluation],
        current_time_seconds: float,
    ) -> dict[str, VehicleSnapshot]:
        """Return a `VehicleSnapshot` for every route, keyed by vehicle identifier."""
        return {
            route.vehicle_id: cls.snapshot_vehicle(
                route, route_evaluations_by_vehicle_id[route.vehicle_id], current_time_seconds
            )
            for route in routes
        }

    @classmethod
    def compute_locked_prefix_lengths(
        cls,
        routes: Sequence[Route],
        route_evaluations_by_vehicle_id: Mapping[str, RouteEvaluation],
        current_time_seconds: float,
    ) -> dict[str, int]:
        """Return the `run_tabu_search`-ready `locked_prefix_lengths` mapping for every route."""
        return {
            route.vehicle_id: cls.compute_locked_prefix_length(
                route_evaluations_by_vehicle_id[route.vehicle_id].stop_schedule,
                len(route.customer_sequence),
                current_time_seconds,
            )
            for route in routes
        }
