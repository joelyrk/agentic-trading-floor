"""A monotonic, explicitly controlled clock for deterministic replay."""

from datetime import datetime


class SimulationClock:
    def __init__(self, current: datetime):
        self._current = self._aware(current)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("simulation time must be timezone-aware")
        return value

    def now(self) -> datetime:
        return self._current

    def advance_to(self, value: datetime) -> None:
        value = self._aware(value)
        if value < self._current:
            raise ValueError("simulation clock cannot move backwards")
        self._current = value

    def reset_to(self, value: datetime) -> None:
        """Start an independent scenario. Runners, not strategies, own this operation."""
        self._current = self._aware(value)
