"""Deterministic retry timing. Persisted as ``retry_not_before``; nothing ever sleeps.

``delay(attempt) = min(base * 2 ** (attempt - 1), max)``: attempt 1 waits ``base``,
attempt 2 twice that, and so on, capped. No jitter: the same inputs always give the
same time, which keeps tests and traces exact. Workers poll; a row whose
``retry_not_before`` is in the future is simply not due yet.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Backoff:
    base_seconds: int = 60
    max_seconds: int = 3600

    def __post_init__(self) -> None:
        if self.base_seconds < 1 or self.max_seconds < self.base_seconds:
            raise ValueError("backoff needs 1 <= base_seconds <= max_seconds")

    def delay(self, attempt: int) -> timedelta:
        """Wait after the ``attempt``-th failed attempt (1-based)."""
        exponent = max(attempt, 1) - 1
        seconds = self.base_seconds * (2 ** min(exponent, 32))
        return timedelta(seconds=min(seconds, self.max_seconds))

    def not_before(self, attempt: int, now: datetime) -> datetime:
        return now + self.delay(attempt)
