"""Injectable clocks keep freshness and replay behavior deterministic."""

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class UtcClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)
