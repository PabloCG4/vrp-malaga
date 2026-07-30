"""
Minute-by-minute accelerated workday clock for the dynamic simulation engine.

The Phase 3 dynamic simulation is specified to advance in discrete, one
minute steps over an accelerated workday (for example 08:00 to 16:00). This
module isolates that bookkeeping into a single, tiny, dependency-free class,
so `simulation.engine.DynamicSimulator` can focus exclusively on event
dispatching and re-optimization instead of on time arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SimulationClock:
    """
    Tracks the progression of an accelerated workday, one minute at a time.

    The clock itself has no notion of vehicles, routes or events: it merely
    counts elapsed simulated seconds since the workday started and exposes a
    human-readable wall-clock timestamp for logging, so it can be reused
    unchanged regardless of how many vehicles or events the simulation ends
    up managing.

    Attributes
    ----------
    workday_duration_seconds:
        Total duration of the simulated workday, in seconds. Defaults to
        28800.0 seconds (8 hours), matching `Vehicle.max_workday_seconds`.
    tick_duration_seconds:
        Number of seconds of simulated time each call to `advance` covers.
        Defaults to 60.0 seconds (one minute), the granularity requested for
        this engine.
    workday_start_hour:
        Wall-clock hour, on a 24-hour scale, at which the simulated workday
        begins. Used only to format `formatted_timestamp` for human-readable
        logging, and defaults to 8.0 (08:00 local time).
    current_time_seconds:
        Elapsed simulated time, in seconds, since the workday started.
    """

    workday_duration_seconds: float = 28800.0
    tick_duration_seconds: float = 60.0
    workday_start_hour: float = 8.0
    current_time_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.workday_duration_seconds <= 0.0:
            message = f"workday_duration_seconds must be positive, got {self.workday_duration_seconds}."
            raise ValueError(message)
        if self.tick_duration_seconds <= 0.0:
            message = f"tick_duration_seconds must be positive, got {self.tick_duration_seconds}."
            raise ValueError(message)
        if self.current_time_seconds < 0.0:
            message = f"current_time_seconds cannot be negative, got {self.current_time_seconds}."
            raise ValueError(message)

    @property
    def is_finished(self) -> bool:
        """Return whether the simulated workday has fully elapsed."""
        return self.current_time_seconds >= self.workday_duration_seconds

    @property
    def current_minute(self) -> int:
        """Return the current simulated minute, truncated, since workday start."""
        return int(self.current_time_seconds // 60.0)

    def advance(self) -> float:
        """
        Advance the clock by one tick, clamped to the end of the workday.

        Returns
        -------
        float
            The new `current_time_seconds` after advancing.
        """
        self.current_time_seconds = min(
            self.current_time_seconds + self.tick_duration_seconds, self.workday_duration_seconds
        )
        return self.current_time_seconds

    def formatted_timestamp(self) -> str:
        """
        Return the current simulated instant as an HH:MM wall-clock string.

        Returns
        -------
        str
            A zero-padded `HH:MM` timestamp, anchored at `workday_start_hour`
            and wrapped around a 24-hour day.
        """
        total_minutes = self.workday_start_hour * 60.0 + self.current_time_seconds / 60.0
        hour, minute = divmod(int(total_minutes), 60)
        hour %= 24
        return f"{hour:02d}:{minute:02d}"
