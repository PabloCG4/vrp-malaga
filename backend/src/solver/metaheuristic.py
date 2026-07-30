"""
Tabu Search metaheuristic engine for the Rich VRP solver.

This module takes a valid `VRPState`, typically the output of
`solver.constructive.build_initial_state`, and iteratively improves it
against the multi-objective evaluation function f(S) defined in
`solver.evaluator`, using Relocate, Swap and 2-opt local search moves guided
by a tabu list with an aspiration criterion.

Why Tabu Search, and not Adaptive Large Neighborhood Search
-------------------------------------------------------------
The baseline architecture proposed for this engine, Tabu Search with a fixed
neighborhood of Relocate/Swap/2-opt moves, is used as specified rather than
replaced with ALNS or Guided Local Search, for three reasons specific to this
domain and this codebase:

1. ALNS earns its complexity on large-scale instances (hundreds to thousands
   of stops), where a fixed neighborhood becomes too restrictive to explore
   effectively and an adaptive mix of destroy/repair operators pays for
   itself. This project's workday instances are dozens of customers (the
   constructive heuristic was benchmarked at 30-100), well within the range
   where a full-neighborhood, best-improvement Tabu Search scan is affordable
   every iteration.
2. ALNS typically needs randomized destroy segment selection and adaptive
   operator-weight bookkeeping to work well. That randomness sits awkwardly
   next to this codebase's fully deterministic style (`evaluate_state` and
   `build_initial_state` are both deterministic for a given input). Tabu
   Search with a fixed move set and deterministic tie-breaking preserves that
   property, which matters for reproducibility.
3. The clock simulation this domain requires (EU rest breaks, soft time
   windows, per-stop service time) makes an exact incremental cost delta
   unavailable to *any* local search: any move can shift the arrival time of
   every customer visited afterwards, so validating a candidate always costs
   an O(route length) re-simulation, regardless of whether the move came from
   a Tabu Search neighborhood or an ALNS repair operator. ALNS's added
   machinery does not remove this cost, so it buys nothing here that a
   simpler, well-understood Tabu Search does not already provide.

Performance design
-------------------
Every candidate move is costed through `evaluator.evaluate_route_cost`, the
allocation-free fast path built on the same clock simulation core
`evaluate_route` uses, so no `RouteEvaluation` or per-stop `RouteStopVisit` is
ever built for a candidate that may be discarded a moment later. A candidate
move only ever rebuilds the plain `tuple[int, ...]` customer sequence of the
one or two routes it touches; a full `VRPState` (and the `Route` objects
inside it) is only constructed once per iteration, for the single move that
is actually committed, reusing `VRPState.with_route_replaced` for the same
structural-sharing benefit the domain layer was designed around.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Hashable, Iterator, Mapping

from .constructive import build_initial_state
from .evaluator import EvaluationWeights, StateEvaluation, evaluate_route_cost, evaluate_state
from ..domain.entities import Route, UnknownVehicleError, VRPState, WorkdayInstance
from ..topology.matrix import CostMatrix

# ---------------------------------------------------------------------------
# Local search moves
# ---------------------------------------------------------------------------
#
# Every move type exposes the same small surface, relied upon by the main
# loop below without needing to know which concrete move it is holding:
#
#   - touched_sequences(current_sequences): the new customer_sequence tuples
#     for the one or two routes the move would change, computed from the
#     current sequences without ever allocating a Route or a VRPState.
#   - candidate_tabu_key: the key looked up in the tabu list to decide
#     whether this specific move is currently forbidden.
#   - commit_tabu_key: the key inserted into the tabu list once this move is
#     applied, forbidding its reverse for the configured tenure.


@dataclass(frozen=True, slots=True)
class RelocateMove:
    """
    Remove one customer from its current route and reinsert it elsewhere.

    The target route may be the same as the source route (a within-route
    relocation) or a different one, including a currently empty route,
    which is how the search can activate an idle vehicle.

    Attributes
    ----------
    customer_node_id:
        Customer being relocated.
    source_vehicle_id:
        Vehicle whose route currently contains the customer.
    source_position:
        Position of the customer within the source route's sequence.
    target_vehicle_id:
        Vehicle whose route will receive the customer.
    target_position:
        Position, within the target route's sequence *after* the customer
        has been removed from the source route, at which it is reinserted.
    """

    customer_node_id: int
    source_vehicle_id: str
    source_position: int
    target_vehicle_id: str
    target_position: int

    def touched_sequences(
        self, current_sequences: Mapping[str, tuple[int, ...]]
    ) -> dict[str, tuple[int, ...]]:
        """Return the new sequence(s) of the route(s) this move changes."""
        source_sequence = list(current_sequences[self.source_vehicle_id])
        removed_customer_node_id = source_sequence.pop(self.source_position)

        if self.target_vehicle_id == self.source_vehicle_id:
            source_sequence.insert(self.target_position, removed_customer_node_id)
            return {self.source_vehicle_id: tuple(source_sequence)}

        target_sequence = list(current_sequences[self.target_vehicle_id])
        target_sequence.insert(self.target_position, removed_customer_node_id)
        return {
            self.source_vehicle_id: tuple(source_sequence),
            self.target_vehicle_id: tuple(target_sequence),
        }

    @property
    def candidate_tabu_key(self) -> Hashable:
        """Key identifying this exact relocation, checked before it is made."""
        return ("relocate", self.customer_node_id, self.source_vehicle_id, self.target_vehicle_id)

    @property
    def commit_tabu_key(self) -> Hashable:
        """Key forbidding the reverse relocation once this move is committed."""
        return ("relocate", self.customer_node_id, self.target_vehicle_id, self.source_vehicle_id)


@dataclass(frozen=True, slots=True)
class SwapMove:
    """
    Exchange the positions of two customers, within or across two routes.

    Attributes
    ----------
    first_customer_node_id, first_vehicle_id, first_position:
        Identity and location of the first customer.
    second_customer_node_id, second_vehicle_id, second_position:
        Identity and location of the second customer.
    """

    first_customer_node_id: int
    first_vehicle_id: str
    first_position: int
    second_customer_node_id: int
    second_vehicle_id: str
    second_position: int

    def touched_sequences(
        self, current_sequences: Mapping[str, tuple[int, ...]]
    ) -> dict[str, tuple[int, ...]]:
        """Return the new sequence(s) of the route(s) this move changes."""
        if self.first_vehicle_id == self.second_vehicle_id:
            sequence = list(current_sequences[self.first_vehicle_id])
            sequence[self.first_position], sequence[self.second_position] = (
                sequence[self.second_position],
                sequence[self.first_position],
            )
            return {self.first_vehicle_id: tuple(sequence)}

        first_sequence = list(current_sequences[self.first_vehicle_id])
        second_sequence = list(current_sequences[self.second_vehicle_id])
        first_sequence[self.first_position] = self.second_customer_node_id
        second_sequence[self.second_position] = self.first_customer_node_id
        return {
            self.first_vehicle_id: tuple(first_sequence),
            self.second_vehicle_id: tuple(second_sequence),
        }

    @property
    def candidate_tabu_key(self) -> Hashable:
        """Key identifying this exact swap; a swap is its own reverse."""
        return ("swap", *sorted((self.first_customer_node_id, self.second_customer_node_id)))

    @property
    def commit_tabu_key(self) -> Hashable:
        """A swap undoes itself if repeated, so its commit key equals its candidate key."""
        return self.candidate_tabu_key


@dataclass(frozen=True, slots=True)
class TwoOptMove:
    """
    Reverse a contiguous segment of one route's customer sequence.

    The tabu key is deliberately based on the customer node identifiers at
    the two ends of the reversed segment, not on their array positions:
    positions drift as Relocate and Swap moves change other routes' lengths
    across iterations, while the customers occupying a given segment stay a
    stable, well-defined identity for as long as that segment exists.

    Attributes
    ----------
    vehicle_id:
        Route whose sequence is reversed.
    segment_start_position, segment_end_position:
        Inclusive positions, within the route's sequence, bounding the
        segment to reverse (`segment_end_position > segment_start_position`).
    segment_start_customer_node_id, segment_end_customer_node_id:
        Customers occupying the segment's boundary positions at the time this
        move was generated, captured here so the tabu key never needs to look
        the sequence back up.
    """

    vehicle_id: str
    segment_start_position: int
    segment_end_position: int
    segment_start_customer_node_id: int
    segment_end_customer_node_id: int

    def touched_sequences(
        self, current_sequences: Mapping[str, tuple[int, ...]]
    ) -> dict[str, tuple[int, ...]]:
        """Return the new sequence of the one route this move changes."""
        sequence = list(current_sequences[self.vehicle_id])
        segment = sequence[self.segment_start_position : self.segment_end_position + 1]
        sequence[self.segment_start_position : self.segment_end_position + 1] = reversed(segment)
        return {self.vehicle_id: tuple(sequence)}

    @property
    def candidate_tabu_key(self) -> Hashable:
        """Key identifying this exact reversal; reversing twice restores the original order."""
        return (
            "two_opt",
            *sorted((self.segment_start_customer_node_id, self.segment_end_customer_node_id)),
        )

    @property
    def commit_tabu_key(self) -> Hashable:
        """A 2-opt reversal undoes itself if repeated, so its commit key equals its candidate key."""
        return self.candidate_tabu_key


Move = RelocateMove | SwapMove | TwoOptMove


def _locked_prefix_length(locked_prefix_lengths: Mapping[str, int], vehicle_id: str, sequence_length: int) -> int:
    """Return the number of leading, immutable positions of a route's sequence."""
    return min(locked_prefix_lengths.get(vehicle_id, 0), sequence_length)


def _generate_relocate_moves(
    current_sequences: Mapping[str, tuple[int, ...]],
    vehicle_ids: tuple[str, ...],
    locked_prefix_lengths: Mapping[str, int],
) -> Iterator[RelocateMove]:
    """Yield every Relocate move that respects each route's locked prefix."""
    for source_vehicle_id in vehicle_ids:
        source_sequence = current_sequences[source_vehicle_id]
        source_lock = _locked_prefix_length(locked_prefix_lengths, source_vehicle_id, len(source_sequence))

        for source_position in range(source_lock, len(source_sequence)):
            customer_node_id = source_sequence[source_position]

            for target_vehicle_id in vehicle_ids:
                if target_vehicle_id == source_vehicle_id:
                    sequence_length_after_removal = len(source_sequence) - 1
                else:
                    sequence_length_after_removal = len(current_sequences[target_vehicle_id])

                target_lock = _locked_prefix_length(
                    locked_prefix_lengths, target_vehicle_id, sequence_length_after_removal
                )

                for target_position in range(target_lock, sequence_length_after_removal + 1):
                    if target_vehicle_id == source_vehicle_id and target_position == source_position:
                        # Removing the customer and reinserting it at the same
                        # position reproduces the current route exactly.
                        continue

                    yield RelocateMove(
                        customer_node_id=customer_node_id,
                        source_vehicle_id=source_vehicle_id,
                        source_position=source_position,
                        target_vehicle_id=target_vehicle_id,
                        target_position=target_position,
                    )


def _generate_swap_moves(
    current_sequences: Mapping[str, tuple[int, ...]],
    vehicle_ids: tuple[str, ...],
    locked_prefix_lengths: Mapping[str, int],
) -> Iterator[SwapMove]:
    """Yield every Swap move between two distinct, unlocked customer slots."""
    unlocked_slots: list[tuple[str, int, int]] = []
    for vehicle_id in vehicle_ids:
        sequence = current_sequences[vehicle_id]
        lock = _locked_prefix_length(locked_prefix_lengths, vehicle_id, len(sequence))
        for position in range(lock, len(sequence)):
            unlocked_slots.append((vehicle_id, position, sequence[position]))

    for first_index in range(len(unlocked_slots)):
        first_vehicle_id, first_position, first_customer_node_id = unlocked_slots[first_index]
        for second_index in range(first_index + 1, len(unlocked_slots)):
            second_vehicle_id, second_position, second_customer_node_id = unlocked_slots[second_index]

            yield SwapMove(
                first_customer_node_id=first_customer_node_id,
                first_vehicle_id=first_vehicle_id,
                first_position=first_position,
                second_customer_node_id=second_customer_node_id,
                second_vehicle_id=second_vehicle_id,
                second_position=second_position,
            )


def _generate_two_opt_moves(
    current_sequences: Mapping[str, tuple[int, ...]],
    vehicle_ids: tuple[str, ...],
    locked_prefix_lengths: Mapping[str, int],
) -> Iterator[TwoOptMove]:
    """Yield every 2-opt segment reversal that stays entirely past the locked prefix."""
    for vehicle_id in vehicle_ids:
        sequence = current_sequences[vehicle_id]
        lock = _locked_prefix_length(locked_prefix_lengths, vehicle_id, len(sequence))

        for segment_start_position in range(lock, len(sequence) - 1):
            for segment_end_position in range(segment_start_position + 1, len(sequence)):
                yield TwoOptMove(
                    vehicle_id=vehicle_id,
                    segment_start_position=segment_start_position,
                    segment_end_position=segment_end_position,
                    segment_start_customer_node_id=sequence[segment_start_position],
                    segment_end_customer_node_id=sequence[segment_end_position],
                )


def _generate_all_moves(
    current_sequences: Mapping[str, tuple[int, ...]],
    vehicle_ids: tuple[str, ...],
    locked_prefix_lengths: Mapping[str, int],
) -> Iterator[Move]:
    """Yield the full Relocate, Swap and 2-opt neighborhood of the current state."""
    yield from _generate_relocate_moves(current_sequences, vehicle_ids, locked_prefix_lengths)
    yield from _generate_swap_moves(current_sequences, vehicle_ids, locked_prefix_lengths)
    yield from _generate_two_opt_moves(current_sequences, vehicle_ids, locked_prefix_lengths)


# ---------------------------------------------------------------------------
# Tabu list
# ---------------------------------------------------------------------------


def move_is_admissible(is_tabu: bool, candidate_total_cost: float, best_total_cost_ever: float) -> bool:
    """
    Decide whether a candidate move may be considered this iteration.

    A non-tabu move is always admissible. A tabu move is admissible only
    through the aspiration criterion: if committing it would strictly beat
    the best total cost ever observed by the search, its tabu status is
    overridden, since rejecting a move that leads to a new global best would
    only make the search strictly worse for no benefit.

    Parameters
    ----------
    is_tabu:
        Whether the move's key is currently forbidden by the tabu list.
    candidate_total_cost:
        Total state cost that would result from committing the move.
    best_total_cost_ever:
        Best total state cost the search has found so far.

    Returns
    -------
    bool
        True if the move may be committed this iteration.
    """
    return not is_tabu or candidate_total_cost < best_total_cost_ever


class TabuList:
    """
    Expiry-based tabu list mapping a move's forbidding key to its expiry iteration.

    Deliberately mutable, unlike the frozen entities of `domain.entities`:
    it is updated once per Tabu Search iteration and never shared or cloned,
    so mutation in place is both correct and the cheapest possible
    implementation.

    Attributes
    ----------
    tabu_tenure:
        Number of iterations a forbidden key remains forbidden for.
    """

    def __init__(self, tabu_tenure: int) -> None:
        self.tabu_tenure = tabu_tenure
        self._expiry_iteration_by_key: dict[Hashable, int] = {}

    def is_tabu(self, key: Hashable, current_iteration: int) -> bool:
        """Return whether `key` is still forbidden at `current_iteration`."""
        expiry_iteration = self._expiry_iteration_by_key.get(key)
        return expiry_iteration is not None and current_iteration < expiry_iteration

    def forbid(self, key: Hashable, current_iteration: int) -> None:
        """Forbid `key` for `tabu_tenure` iterations starting at `current_iteration`."""
        self._expiry_iteration_by_key[key] = current_iteration + self.tabu_tenure


# ---------------------------------------------------------------------------
# Search configuration, result and main loop
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TabuSearchConfig:
    """
    Stopping criteria and tuning parameters for `run_tabu_search`.

    The search stops as soon as any one of `max_iterations`,
    `max_iterations_without_improvement` or `time_limit_seconds` is reached,
    whichever comes first, which lets a caller bound the search by either a
    hard iteration budget, a convergence signal, or a wall-clock deadline
    suited to a real-time re-optimization context.

    Attributes
    ----------
    max_iterations:
        Hard cap on the number of iterations, regardless of progress.
    max_iterations_without_improvement:
        Stop once this many consecutive iterations have failed to beat the
        best-ever cost found so far.
    time_limit_seconds:
        Wall-clock budget for the whole search.
    tabu_tenure:
        Number of iterations a move's reverse stays forbidden for.
    evaluation_weights:
        Weights defining f(S), shared between every cost computation the
        search performs and the final `StateEvaluation` it returns.
    """

    max_iterations: int = 1000
    max_iterations_without_improvement: int = 200
    time_limit_seconds: float = 10.0
    tabu_tenure: int = 15
    evaluation_weights: EvaluationWeights = field(default_factory=EvaluationWeights)

    def __post_init__(self) -> None:
        if self.max_iterations <= 0:
            message = f"max_iterations must be positive, got {self.max_iterations}."
            raise ValueError(message)
        if self.max_iterations_without_improvement <= 0:
            message = (
                "max_iterations_without_improvement must be positive, got "
                f"{self.max_iterations_without_improvement}."
            )
            raise ValueError(message)
        if self.time_limit_seconds <= 0.0:
            message = f"time_limit_seconds must be positive, got {self.time_limit_seconds}."
            raise ValueError(message)
        if self.tabu_tenure <= 0:
            message = f"tabu_tenure must be positive, got {self.tabu_tenure}."
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class TabuSearchResult:
    """
    Outcome of a `run_tabu_search` call.

    Attributes
    ----------
    best_state:
        Best `VRPState` found during the search. Always covers every workday
        customer exactly once, since every move preserves coverage by
        construction.
    best_evaluation:
        Full `evaluator.evaluate_state` breakdown of `best_state`.
    iterations_completed:
        Number of iterations actually run before a stopping criterion was
        reached.
    elapsed_seconds:
        Wall-clock duration of the search.
    cost_history:
        Best-so-far total cost recorded after every iteration, starting with
        the initial state's cost, for convergence reporting.
    """

    best_state: VRPState
    best_evaluation: StateEvaluation
    iterations_completed: int
    elapsed_seconds: float
    cost_history: tuple[float, ...]


def _route_cost_cache(
    sequences: Mapping[str, tuple[int, ...]],
    vehicle_ids: tuple[str, ...],
    workday: WorkdayInstance,
    cost_matrix: CostMatrix,
    weights: EvaluationWeights,
) -> dict[str, float]:
    """Compute the weighted cost of every route in `sequences`, keyed by vehicle."""
    return {
        vehicle_id: evaluate_route_cost(
            sequences[vehicle_id], workday.fleet_by_vehicle_id[vehicle_id], workday, cost_matrix, weights
        )
        for vehicle_id in vehicle_ids
    }


def run_tabu_search(
    initial_state: VRPState,
    workday: WorkdayInstance,
    cost_matrix: CostMatrix,
    config: TabuSearchConfig = TabuSearchConfig(),
    locked_prefix_lengths: Mapping[str, int] | None = None,
) -> TabuSearchResult:
    """
    Improve a candidate `VRPState` with Tabu Search until a stopping criterion fires.

    Every iteration generates the full Relocate, Swap and 2-opt neighborhood
    of the current state, costs every candidate with `evaluate_route_cost`
    (an O(route length) re-simulation of only the one or two routes the move
    touches), and commits the cheapest candidate that is not currently tabu,
    unless a tabu candidate would beat the best-ever cost found so far
    (aspiration criterion), in which case it is allowed regardless. The best
    state ever observed, which may differ from the current one since Tabu
    Search deliberately accepts worsening moves to escape local optima, is
    what the search ultimately returns.

    Two usage modes are supported through the same entry point:

    - Static planning: call with `locked_prefix_lengths=None` (the default),
      letting every stop of every route be reconsidered.
    - Dynamic re-optimization: pass a `locked_prefix_lengths` mapping the
      vehicles already en route to the number of leading stops on their
      route that have already been completed and must not be touched. Every
      move generator skips those positions, so the search only rearranges
      the still-unvisited remainder of each route.

    Parameters
    ----------
    initial_state:
        Starting candidate solution, typically the output of
        `solver.constructive.build_initial_state`. Must already cover every
        workday customer exactly once.
    workday:
        Problem instance (fleet and customers) being solved.
    cost_matrix:
        Precomputed cost matrix whose nodes of interest match the workday's
        depot and customers.
    config:
        Stopping criteria and tuning parameters.
    locked_prefix_lengths:
        Optional, per-vehicle count of leading stops that are already
        completed and therefore immutable. Omit for static planning.

    Returns
    -------
    TabuSearchResult
        The best state found, its full evaluation, and search diagnostics.

    Raises
    ------
    UnknownVehicleError
        If a route of `initial_state` references a vehicle absent from the
        workday fleet.
    UnknownCustomerError
        If a route visits a node that is not a workday customer.
    DuplicateCustomerAssignmentError
        If a customer is assigned to more than one route.
    IncompleteCoverageError
        If a workday customer is not served by any route.
    """
    initial_state.validate_customer_coverage(workday)

    vehicle_ids = tuple(route.vehicle_id for route in initial_state.routes)
    for vehicle_id in vehicle_ids:
        if vehicle_id not in workday.fleet_by_vehicle_id:
            message = f"Route references vehicle '{vehicle_id}', which is not part of the fleet."
            raise UnknownVehicleError(message)

    weights = config.evaluation_weights
    locks: Mapping[str, int] = locked_prefix_lengths if locked_prefix_lengths is not None else {}

    current_sequences: dict[str, tuple[int, ...]] = {
        route.vehicle_id: route.customer_sequence for route in initial_state.routes
    }
    route_cost_by_vehicle_id = _route_cost_cache(current_sequences, vehicle_ids, workday, cost_matrix, weights)
    current_total_cost = sum(route_cost_by_vehicle_id.values())

    best_sequences = dict(current_sequences)
    best_total_cost = current_total_cost

    tabu_list = TabuList(config.tabu_tenure)
    cost_history: list[float] = [best_total_cost]

    iteration = 0
    iterations_without_improvement = 0
    search_started_at = time.perf_counter()

    while (
        iteration < config.max_iterations
        and iterations_without_improvement < config.max_iterations_without_improvement
        and (time.perf_counter() - search_started_at) < config.time_limit_seconds
    ):
        best_candidate_total_cost: float | None = None
        best_candidate_move: Move | None = None
        best_candidate_touched_sequences: dict[str, tuple[int, ...]] | None = None
        best_candidate_touched_costs: dict[str, float] | None = None

        for move in _generate_all_moves(current_sequences, vehicle_ids, locks):
            touched_sequences = move.touched_sequences(current_sequences)
            touched_costs = {
                vehicle_id: evaluate_route_cost(
                    sequence, workday.fleet_by_vehicle_id[vehicle_id], workday, cost_matrix, weights
                )
                for vehicle_id, sequence in touched_sequences.items()
            }
            candidate_total_cost = sum(touched_costs.values()) + sum(
                cost
                for vehicle_id, cost in route_cost_by_vehicle_id.items()
                if vehicle_id not in touched_costs
            )

            is_tabu = tabu_list.is_tabu(move.candidate_tabu_key, iteration)
            if not move_is_admissible(is_tabu, candidate_total_cost, best_total_cost):
                continue

            if best_candidate_total_cost is None or candidate_total_cost < best_candidate_total_cost:
                best_candidate_total_cost = candidate_total_cost
                best_candidate_move = move
                best_candidate_touched_sequences = touched_sequences
                best_candidate_touched_costs = touched_costs

        if (
            best_candidate_move is None
            or best_candidate_touched_sequences is None
            or best_candidate_touched_costs is None
            or best_candidate_total_cost is None
        ):
            # Every candidate this iteration was tabu and none satisfied the
            # aspiration criterion (or the neighborhood was empty, e.g. every
            # route has at most one unlocked customer); the search has
            # nothing admissible left to do and stops early.
            break

        tabu_list.forbid(best_candidate_move.commit_tabu_key, iteration)
        current_sequences.update(best_candidate_touched_sequences)
        route_cost_by_vehicle_id.update(best_candidate_touched_costs)
        current_total_cost = best_candidate_total_cost

        if current_total_cost < best_total_cost:
            best_total_cost = current_total_cost
            best_sequences = dict(current_sequences)
            iterations_without_improvement = 0
        else:
            iterations_without_improvement += 1

        iteration += 1
        cost_history.append(best_total_cost)

    elapsed_seconds = time.perf_counter() - search_started_at

    best_state = VRPState(
        routes=tuple(
            Route(vehicle_id=route.vehicle_id, customer_sequence=best_sequences[route.vehicle_id])
            for route in initial_state.routes
        )
    )
    best_evaluation = evaluate_state(best_state, workday, cost_matrix, weights)

    return TabuSearchResult(
        best_state=best_state,
        best_evaluation=best_evaluation,
        iterations_completed=iteration,
        elapsed_seconds=elapsed_seconds,
        cost_history=tuple(cost_history),
    )


if __name__ == "__main__":
    # This module is only meant to be run as `python -m backend.src.solver.metaheuristic`
    # from the repository root, for the same import resolution reason
    # documented in `evaluator.py`'s and `constructive.py`'s own __main__ blocks.
    from ..domain.entities import Customer, TimeWindow, Vehicle
    from ..topology.extractor import load_processed_graph
    from ..topology.matrix import build_cost_matrix, select_demonstration_nodes

    DEMONSTRATION_CUSTOMER_COUNT: int = 50
    DEMONSTRATION_VEHICLE_COUNT: int = 5

    print("Loading the preprocessed street network and building its cost matrix.")
    malaga_street_network = load_processed_graph()
    demonstration_depot, demonstration_customer_nodes = select_demonstration_nodes(
        malaga_street_network, DEMONSTRATION_CUSTOMER_COUNT
    )
    demonstration_cost_matrix = build_cost_matrix(
        malaga_street_network, demonstration_depot, demonstration_customer_nodes
    )

    demonstration_workday = WorkdayInstance(
        customers=tuple(
            Customer(
                node_id=node_id,
                demand=float(15 + (index % 4) * 5),
                service_time_seconds=180.0 + 30.0 * (index % 6),
                time_window=TimeWindow(start_seconds=200.0 * index, end_seconds=200.0 * index + 5400.0),
            )
            for index, node_id in enumerate(demonstration_customer_nodes)
        ),
        fleet=tuple(
            Vehicle(vehicle_id=f"VAN-{vehicle_index + 1}", max_capacity=400.0)
            for vehicle_index in range(DEMONSTRATION_VEHICLE_COUNT)
        ),
    )

    print("Building the initial state with the Phase 2.3 constructive heuristic.")
    initial_state = build_initial_state(demonstration_workday, demonstration_cost_matrix)
    initial_evaluation = evaluate_state(initial_state, demonstration_workday, demonstration_cost_matrix)
    print(f"  Initial cost: f(S) = {initial_evaluation.total_cost:.2f}, feasible={initial_evaluation.is_feasible}")

    print("\nRunning static-planning Tabu Search over the whole workday.")
    search_config = TabuSearchConfig(
        max_iterations=500, max_iterations_without_improvement=100, time_limit_seconds=8.0, tabu_tenure=15
    )
    search_result = run_tabu_search(initial_state, demonstration_workday, demonstration_cost_matrix, search_config)

    print(search_result)

    iterations_per_second = (
        search_result.iterations_completed / search_result.elapsed_seconds
        if search_result.elapsed_seconds > 0.0
        else float("inf")
    )
    print(
        f"  Completed {search_result.iterations_completed} iterations in "
        f"{search_result.elapsed_seconds * 1000.0:.1f} ms ({iterations_per_second:.1f} it/s)."
    )
    print(
        f"  Best cost: f(S) = {search_result.best_evaluation.total_cost:.2f}, "
        f"feasible={search_result.best_evaluation.is_feasible} "
        f"(improvement: {initial_evaluation.total_cost - search_result.best_evaluation.total_cost:.2f})"
    )

    print("\nDynamic re-optimization demo: locking the first two stops of every route.")
    locked_prefix_lengths = {
        route.vehicle_id: min(2, len(route.customer_sequence)) for route in search_result.best_state.routes
    }
    dynamic_config = TabuSearchConfig(
        max_iterations=200, max_iterations_without_improvement=50, time_limit_seconds=3.0, tabu_tenure=10
    )
    dynamic_result = run_tabu_search(
        search_result.best_state,
        demonstration_workday,
        demonstration_cost_matrix,
        dynamic_config,
        locked_prefix_lengths=locked_prefix_lengths,
    )

    locked_prefixes_preserved = all(
        dynamic_result.best_state.routes[route_index].customer_sequence[:prefix_length]
        == search_result.best_state.routes[route_index].customer_sequence[:prefix_length]
        for route_index, prefix_length in enumerate(
            locked_prefix_lengths[route.vehicle_id] for route in search_result.best_state.routes
        )
    )
    print(
        f"  Re-optimized in {dynamic_result.elapsed_seconds * 1000.0:.1f} ms over "
        f"{dynamic_result.iterations_completed} iterations; locked prefixes preserved: "
        f"{locked_prefixes_preserved}."
    )
    print(
        f"  Cost after re-optimizing the unvisited remainder: "
        f"f(S) = {dynamic_result.best_evaluation.total_cost:.2f}, feasible={dynamic_result.best_evaluation.is_feasible}"
    )
