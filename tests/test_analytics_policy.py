"""Phase 6 deterministic rules: snapshot ages, backoff, retry decisions, derived metrics."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from social_growth_agent.analytics import (
    engagement_rate_by_impressions,
    observed_rates,
    rate_per_impression,
    total_engagement,
)
from social_growth_agent.config import AppSettings
from social_growth_agent.models import AnalyticsFailureCategory as Cat
from social_growth_agent.models import AnalyticsJobStatus
from social_growth_agent.policies.analytics import (
    AUTO_RETRY,
    MANUAL_RETRY,
    TERMINAL,
    decide_after_failure,
    is_on_target,
    parse_snapshot_ages,
)
from social_growth_agent.policies.retry import Backoff

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
BACKOFF = Backoff(base_seconds=60, max_seconds=600)


# --- snapshot ages ---------------------------------------------------------------------


def test_default_ages_parse_sorted_with_labels_and_seconds():
    ages = parse_snapshot_ages("72h, 1h,24h")
    assert [(a.label, a.seconds) for a in ages] == [("1h", 3600), ("24h", 86_400), ("72h", 259_200)]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "at least one age"),
        ("1h,abc", "invalid snapshot age"),
        ("30s", "between 1 minute and 30 days"),
        ("31d", "between 1 minute and 30 days"),
        ("1h,60m", "duplicates"),
        (",".join(f"{n}h" for n in range(1, 12)), "at most 10"),
    ],
)
def test_invalid_ages_are_refused(text, message):
    with pytest.raises(ValueError, match=message):
        parse_snapshot_ages(text)


def test_settings_validate_analytics_configuration():
    assert AppSettings(_env_file=None).analytics_snapshot_ages == "1h,24h,72h"
    with pytest.raises(ValidationError, match="invalid snapshot age"):
        AppSettings(_env_file=None, analytics_snapshot_ages="soon")
    with pytest.raises(ValidationError, match="BACKOFF_MAX"):
        AppSettings(
            _env_file=None, analytics_backoff_base_seconds=600, analytics_backoff_max_seconds=60
        )
    with pytest.raises(ValidationError, match="ANALYTICS_LEASE_SECONDS"):
        AppSettings(_env_file=None, analytics_lease_seconds=10, x_timeout_seconds=10)
    with pytest.raises(ValidationError, match="PUBLISH_BACKOFF_MAX"):
        AppSettings(_env_file=None, publish_backoff_base_seconds=60, publish_backoff_max_seconds=1)
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, analytics_batch_size=101)


# --- backoff ---------------------------------------------------------------------------


def test_backoff_doubles_per_attempt_and_is_capped():
    delays = [BACKOFF.delay(n).total_seconds() for n in range(1, 7)]
    assert delays == [60, 120, 240, 480, 600, 600]
    assert BACKOFF.not_before(2, NOW) == NOW + timedelta(seconds=120)
    assert Backoff().delay(1000) == timedelta(seconds=3600)  # no overflow, still capped


def test_backoff_refuses_nonsense():
    with pytest.raises(ValueError):
        Backoff(base_seconds=0)
    with pytest.raises(ValueError):
        Backoff(base_seconds=10, max_seconds=5)


# --- retry classification --------------------------------------------------------------


def decide(category, attempt=1, max_attempts=3, reset=None):
    return decide_after_failure(
        category,
        attempt=attempt,
        max_attempts=max_attempts,
        now=NOW,
        backoff=BACKOFF,
        rate_limit_reset_at=reset,
    )


def test_categories_are_partitioned():
    assert AUTO_RETRY.isdisjoint(TERMINAL)
    assert Cat.AUTH not in AUTO_RETRY and Cat.AUTH not in TERMINAL
    assert Cat.AUTH in MANUAL_RETRY
    assert set(Cat) == AUTO_RETRY | TERMINAL | {Cat.AUTH}


def test_auth_fails_for_a_manual_retry():
    d = decide(Cat.AUTH)
    assert (d.status, d.retry_not_before, d.exhausted) == (AnalyticsJobStatus.FAILED, None, False)


@pytest.mark.parametrize("category", [Cat.BAD_REQUEST, Cat.NOT_FOUND])
def test_terminal_categories_fail_even_with_attempts_left(category):
    assert decide(category, attempt=1).status is AnalyticsJobStatus.FAILED


def test_rate_limit_waits_for_its_reset_and_never_less_than_the_backoff():
    reset = NOW + timedelta(minutes=15)
    d = decide(Cat.RATE_LIMITED, reset=reset)
    assert (d.status, d.retry_not_before) == (AnalyticsJobStatus.SCHEDULED, reset)
    early_reset = NOW + timedelta(seconds=5)
    assert decide(Cat.RATE_LIMITED, reset=early_reset).retry_not_before == NOW + timedelta(
        seconds=60
    )


def test_rate_limit_without_reset_uses_the_bounded_backoff():
    d = decide(Cat.RATE_LIMITED, attempt=2)
    assert (d.status, d.retry_not_before) == (
        AnalyticsJobStatus.SCHEDULED,
        NOW + timedelta(seconds=120),
    )
    assert decide(Cat.RATE_LIMITED, attempt=3).exhausted


@pytest.mark.parametrize(
    "category",
    [Cat.TIMEOUT, Cat.TRANSIENT_SERVER, Cat.MALFORMED_RESPONSE, Cat.UNKNOWN, Cat.LEASE_EXPIRED],
)
def test_retryable_categories_back_off_then_fail_when_attempts_run_out(category):
    assert decide(category, attempt=1).retry_not_before == NOW + timedelta(seconds=60)
    assert decide(category, attempt=2).retry_not_before == NOW + timedelta(seconds=120)
    last = decide(category, attempt=3)
    assert (last.status, last.retry_not_before, last.exhausted) == (
        AnalyticsJobStatus.FAILED,
        None,
        True,
    )


# --- derived metrics -------------------------------------------------------------------


def test_total_engagement_sums_reported_parts_and_never_guesses_missing_ones():
    assert total_engagement(10, 2, 3, 1) == 16
    assert total_engagement(0, 0, 0, 0) == 0  # an explicit 0 stays 0
    assert total_engagement(10, None, 3, 1) is None


def test_engagement_rate_requires_reported_positive_impressions():
    assert engagement_rate_by_impressions(10, 2, 3, 1, 1600) == pytest.approx(0.01)
    assert engagement_rate_by_impressions(10, 2, 3, 1, None) is None
    assert engagement_rate_by_impressions(10, 2, 3, 1, 0) is None
    assert engagement_rate_by_impressions(None, 2, 3, 1, 1600) is None
    assert rate_per_impression(4, 400) == pytest.approx(0.01)


def test_observed_rates_bundle_is_named_as_observation():
    rates = observed_rates(likes=8, reposts=1, replies=1, quotes=0, impressions=1000)
    assert rates.observed_engagement_count == 10
    assert rates.observed_engagement_rate_by_impressions == pytest.approx(0.01)
    assert rates.observed_reply_rate == pytest.approx(0.001)
    assert rates.observed_repost_rate == pytest.approx(0.001)
    assert (
        observed_rates(
            likes=8, reposts=1, replies=1, quotes=0, impressions=None
        ).observed_reply_rate
        is None
    )


def test_on_target_tolerance_is_the_larger_of_a_floor_and_ten_percent():
    assert is_on_target(3600 + 360, 3600, 120)  # 1h: max(120 s, 10% = 360 s)
    assert not is_on_target(3600 + 361, 3600, 120)
    assert is_on_target(600 + 120, 600, 120)  # 10 min: the 120 s floor wins
    assert not is_on_target(600 + 121, 600, 120)
    assert is_on_target(259_200 + 25_000, 259_200, 120)  # 72h: within 10%
    assert not is_on_target(30 * 3600, 3600, 120)  # a "1h" snapshot taken at 30h
