"""Minimal timing helper used to build NodeEvents (future: OpenTelemetry spans)."""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from social_growth_agent.models import utc_now


@dataclass
class Timer:
    started_at: datetime = field(default_factory=utc_now)
    duration_ms: float = 0.0
    _start: float = field(default_factory=time.perf_counter, repr=False)

    def elapsed_ms(self) -> float:
        """Milliseconds since the timer started (usable inside the ``timed`` block)."""
        return (time.perf_counter() - self._start) * 1000


@contextmanager
def timed() -> Iterator[Timer]:
    timer = Timer()
    try:
        yield timer
    finally:
        timer.duration_ms = timer.elapsed_ms()
