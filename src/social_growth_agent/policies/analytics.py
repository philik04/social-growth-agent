"""Deterministic analytics rules: snapshot ages, retry classification, capture timing.

No I/O and no LLM. The analytics worker asks these functions what to do; it never
decides on its own whether a snapshot happens or whether a failure is retried.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from social_growth_agent.models import AnalyticsFailureCategory, AnalyticsJobStatus, SnapshotAge
from social_growth_agent.policies.retry import Backoff

_AGE = re.compile(r"^(\d+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86_400}
MIN_AGE_SECONDS = 60
MAX_AGE_SECONDS = 30 * 86_400
MAX_AGES = 10

AUTO_RETRY = frozenset(
    {
        AnalyticsFailureCategory.RATE_LIMITED,
        AnalyticsFailureCategory.TIMEOUT,
        AnalyticsFailureCategory.TRANSIENT_SERVER,
        AnalyticsFailureCategory.MALFORMED_RESPONSE,
        AnalyticsFailureCategory.UNKNOWN,
        AnalyticsFailureCategory.LEASE_EXPIRED,
    }
)
"""Retried automatically, bounded, after a persisted wait. A metrics read creates
nothing, so repeating it is safe (it may cost one more post read)."""
TERMINAL = frozenset({AnalyticsFailureCategory.BAD_REQUEST, AnalyticsFailureCategory.NOT_FOUND})
"""Never retried: repeating the same request cannot succeed."""
MANUAL_RETRY = frozenset(AnalyticsFailureCategory) - TERMINAL
"""A ``failed`` job in these categories may be queued again by hand (``auth`` after the
credentials are fixed, or any retryable category after its attempts ran out)."""


def parse_snapshot_ages(text: str) -> tuple[SnapshotAge, ...]:
    """``"1h,24h,72h"`` -> ages sorted by duration. Raises ``ValueError``."""
    labels = [part.strip().lower() for part in text.split(",") if part.strip()]
    if not labels:
        raise ValueError("ANALYTICS_SNAPSHOT_AGES needs at least one age, e.g. '1h,24h,72h'")
    if len(labels) > MAX_AGES:
        raise ValueError(f"ANALYTICS_SNAPSHOT_AGES allows at most {MAX_AGES} ages")
    ages: dict[int, SnapshotAge] = {}
    for label in labels:
        match = _AGE.match(label)
        if match is None:
            raise ValueError(f"invalid snapshot age {label!r}: use a number and s, m, h or d")
        seconds = int(match.group(1)) * _UNIT_SECONDS[match.group(2)]
        if not MIN_AGE_SECONDS <= seconds <= MAX_AGE_SECONDS:
            raise ValueError(f"snapshot age {label!r} must be between 1 minute and 30 days")
        if seconds in ages:
            raise ValueError(f"snapshot age {label!r} duplicates {ages[seconds].label!r}")
        ages[seconds] = SnapshotAge(label=label, seconds=seconds)
    return tuple(ages[s] for s in sorted(ages))


@dataclass(frozen=True)
class RetryDecision:
    status: AnalyticsJobStatus
    retry_not_before: datetime | None
    exhausted: bool = False


def decide_after_failure(
    category: AnalyticsFailureCategory,
    *,
    attempt: int,
    max_attempts: int,
    now: datetime,
    backoff: Backoff,
    rate_limit_reset_at: datetime | None = None,
) -> RetryDecision:
    """What happens to a job whose ``attempt``-th collection failed with ``category``.

    - ``auth``: ``failed``; a human retries once the credentials are fixed.
    - ``bad_request``, ``not_found``: ``failed``, terminal.
    - a rate limit with a known reset: ``scheduled`` again, not before the reset (or the
      backoff, if that is later). Without a reset time: the backoff.
    - every other retryable category: ``scheduled`` again after the backoff.
    - retryable but out of attempts: ``failed`` (a manual retry is allowed).
    """
    if category not in AUTO_RETRY:
        return RetryDecision(AnalyticsJobStatus.FAILED, None)
    if attempt >= max_attempts:
        return RetryDecision(AnalyticsJobStatus.FAILED, None, exhausted=True)
    not_before = backoff.not_before(attempt, now)
    if category is AnalyticsFailureCategory.RATE_LIMITED and rate_limit_reset_at is not None:
        not_before = max(not_before, rate_limit_reset_at)
    return RetryDecision(AnalyticsJobStatus.SCHEDULED, not_before)


def scheduled_for(basis_at: datetime, age: SnapshotAge) -> datetime:
    return basis_at + timedelta(seconds=age.seconds)


def on_target_tolerance(target_age_seconds: int, minimum_seconds: int) -> int:
    """How far an actual age may be from its target and still count as that age."""
    return max(minimum_seconds, target_age_seconds // 10)


def is_on_target(actual_age_seconds: float, target_age_seconds: int, minimum_seconds: int) -> bool:
    tolerance = on_target_tolerance(target_age_seconds, minimum_seconds)
    return abs(actual_age_seconds - target_age_seconds) <= tolerance
