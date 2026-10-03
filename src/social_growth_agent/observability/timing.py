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


@contextmanager
def timed() -> Iterator[Timer]:
    timer = Timer()
    start = time.perf_counter()
    try:
        yield timer
    finally:
        timer.duration_ms = (time.perf_counter() - start) * 1000
