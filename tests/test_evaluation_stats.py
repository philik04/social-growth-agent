"""Deterministic evaluation (Phase 7), pure: eligibility, age matching, statistics, digest."""

import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from social_growth_agent.analytics.evaluation import (
    EvaluationConfig,
    PostInput,
    SnapshotInput,
    evaluate,
    evaluation_digest,
    hour_block,
    length_bucket,
    median,
    quantile,
)
from social_growth_agent.models.strategy import (
    Dimension,
    EvaluationStatus,
    EvidenceLevel,
    ExclusionReason,
    Metric,
)

T0 = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
CUTOFF = datetime(2026, 10, 12, tzinfo=UTC)
DAY = 86_400


def config(**overrides) -> EvaluationConfig:
    base = {
        "snapshot_age": "24h",
        "snapshot_age_seconds": DAY,
        "synthetic": True,
        "min_group_n": 3,
        "min_compare_n": 5,
        "min_total_n": 10,
    }
    return EvaluationConfig(**(base | overrides))


def post(
    i: int,
    *,
    hook: str = "number",
    metrics: dict[str, int | None] | None = None,
    offset: int = 30,
    published: datetime | None = None,
    provider: str = "mock_analytics",
    created_known: bool = True,
    length: int = 120,
    snapshot: bool = True,
    strategy_version: int = 1,
    strategy_digest: str = "a" * 64,
    origin: str = "generated",
) -> PostInput:
    published = published or T0 + timedelta(hours=i)
    created = published - timedelta(seconds=2)
    m: dict[str, int | None] = {
        "likes": 10,
        "replies": 2,
        "reposts": 1,
        "quotes": 1,
        "bookmarks": 0,
        "impressions": 1000,
    }
    m |= metrics or {}
    snap = SnapshotInput(
        post_metric_id=f"pm_{i}",
        provider=provider,
        captured_at=created + timedelta(seconds=DAY + offset),
        provider_created_at=created if created_known else None,
        **m,
    )
    return PostInput(
        publication_id=f"pub_{i:03d}",
        run_id=f"run_{i}",
        candidate_id=f"cand_{i}",
        content="x" * length,
        published_at=published,
        hook_type=hook,
        origin=origin,
        format="single",
        strategy_id="strat_a",
        strategy_version=strategy_version,
        strategy_digest=strategy_digest,
        snapshot=snap if snapshot else None,
    )


def obs(result, i):
    return next(o for o in result.observations if o.publication_id == f"pub_{i:03d}")


def group(result, dimension, value):
    return next(g for g in result.summary.groups if g.dimension == dimension and g.value == value)


def stat(g, metric):
    return next(m for m in g.metrics if m.metric is metric)


# --- 1. no eligible posts / 2. insufficient sample -------------------------------------------


def test_no_posts_is_an_insufficient_evaluation_with_no_comparisons():
    result = evaluate([], config(), CUTOFF)
    s = result.summary
    assert s.status is EvaluationStatus.INSUFFICIENT_EVIDENCE
    assert (s.eligible_count, s.excluded_count, s.comparisons) == (0, 0, [])
    overall = s.groups[0]
    assert overall.n_eligible == 0 and stat(overall, Metric.LIKES).median is None


def test_below_min_total_n_nothing_is_compared_even_if_groups_are_big_enough():
    posts = [post(i, hook="number") for i in range(5)] + [
        post(i, hook="how_to") for i in range(5, 9)
    ]
    result = evaluate(posts, config(min_total_n=10), CUTOFF)
    assert result.summary.status is EvaluationStatus.INSUFFICIENT_EVIDENCE
    assert result.summary.comparisons == []
    assert group(result, Dimension.HOOK_TYPE, "number").evidence_level is EvidenceLevel.COMPARABLE


def test_evidence_levels_follow_the_thresholds():
    posts = [post(i, hook="number") for i in range(5)]
    posts += [post(i, hook="how_to") for i in range(5, 8)]
    posts += [post(i, hook="story") for i in range(8, 10)]
    result = evaluate(posts, config(), CUTOFF)
    levels = {
        g.value: g.evidence_level
        for g in result.summary.groups
        if g.dimension is Dimension.HOOK_TYPE
    }
    assert levels == {
        "number": EvidenceLevel.COMPARABLE,
        "how_to": EvidenceLevel.DESCRIPTIVE,
        "story": EvidenceLevel.INSUFFICIENT,
    }
    # only comparable groups are compared: just one comparable hook group -> none
    assert [c for c in result.summary.comparisons if c.dimension is Dimension.HOOK_TYPE] == []


def test_thresholds_cannot_go_below_the_floors():
    with pytest.raises(ValidationError):
        config(min_group_n=2)
    with pytest.raises(ValidationError):
        config(min_total_n=9)
    with pytest.raises(ValidationError):
        config(min_group_n=6, min_compare_n=5)


# --- 3. age matching / 4. late exclusion / 14. cutoff ----------------------------------------


def test_age_matching_uses_the_snapshot_provider_creation_time():
    tolerance = DAY // 10  # max(120 s, 10 % of 24 h)
    posts = [
        post(1, offset=tolerance),
        post(2, offset=tolerance + 1),
        post(3, offset=-tolerance - 1),
        post(4, created_known=False),
    ]
    result = evaluate(posts, config(), CUTOFF)
    assert obs(result, 1).eligible and obs(result, 1).actual_age_seconds == DAY + tolerance
    assert obs(result, 2).exclusion_reason is ExclusionReason.OFF_TARGET_LATE
    assert obs(result, 3).exclusion_reason is ExclusionReason.OFF_TARGET_EARLY
    assert obs(result, 4).exclusion_reason is ExclusionReason.NO_PROVIDER_CREATED_AT


def test_late_snapshots_stay_listed_with_their_counts_but_never_enter_statistics():
    posts = [post(1, metrics={"likes": 999}, offset=4 * 3600), post(2, metrics={"likes": 10})]
    result = evaluate(posts, config(), CUTOFF)
    late = obs(result, 1)
    assert not late.eligible and late.likes == 999
    assert stat(result.summary.groups[0], Metric.LIKES).max == "10.000000"
    assert result.summary.excluded_by_reason == {"off_target_late": 1}


def test_cutoff_hides_later_captures_and_reports_not_yet_due():
    captured_late = post(1)  # captured on day 2
    cutoff = captured_late.published_at + timedelta(hours=12)
    result = evaluate([captured_late], config(), cutoff)
    assert obs(result, 1).exclusion_reason is ExclusionReason.NOT_YET_DUE
    assert obs(result, 1).likes is None  # nothing from after the cutoff leaks in
    old_without_snapshot = post(2, snapshot=False)
    result = evaluate([old_without_snapshot], config(), CUTOFF)
    assert obs(result, 2).exclusion_reason is ExclusionReason.NO_SNAPSHOT


def test_synthetic_and_real_snapshots_are_never_mixed():
    posts = [post(1, provider="mock_analytics"), post(2, provider="x")]
    synthetic = evaluate(posts, config(synthetic=True), CUTOFF)
    real = evaluate(posts, config(synthetic=False), CUTOFF)
    assert [o.eligible for o in synthetic.observations] == [True, False]
    assert (
        obs(synthetic, 2).exclusion_reason is ExclusionReason.REAL_METRICS_IN_SYNTHETIC_EVALUATION
    )
    assert [o.eligible for o in real.observations] == [False, True]
    assert obs(real, 1).exclusion_reason is ExclusionReason.SYNTHETIC_METRICS


# --- 5. missing stays missing / 6. zero impressions / 7. engagement math ---------------------


def test_missing_values_shrink_n_for_that_metric_only():
    posts = [post(1, metrics={"impressions": None}), post(2, metrics={"quotes": None})]
    result = evaluate(posts, config(), CUTOFF)
    overall = result.summary.groups[0]
    assert stat(overall, Metric.LIKES).n == 2
    assert stat(overall, Metric.IMPRESSIONS).n == 1
    # engagement needs all four parts: post 2 has no quotes, so no engagement count
    assert stat(overall, Metric.ENGAGEMENT_COUNT).n == 1
    # post 1 has no impressions and post 2 no engagement count: no engagement rate at all
    assert stat(overall, Metric.ENGAGEMENT_RATE).n == 0
    assert stat(overall, Metric.ENGAGEMENT_RATE).median is None
    assert obs(result, 1).impressions is None  # never stored as 0


def test_zero_impressions_never_divide():
    result = evaluate([post(1, metrics={"impressions": 0})], config(), CUTOFF)
    overall = result.summary.groups[0]
    rate = stat(overall, Metric.ENGAGEMENT_RATE)
    assert (rate.n, rate.median, rate.pooled_rate, rate.denominator_n) == (0, None, None, 0)
    assert stat(overall, Metric.IMPRESSIONS).median == "0.000000"


def test_engagement_is_likes_reposts_replies_quotes_without_bookmarks():
    p = post(
        1,
        metrics={
            "likes": 10,
            "reposts": 3,
            "replies": 2,
            "quotes": 1,
            "bookmarks": 50,
            "impressions": 400,
        },
    )
    overall = evaluate([p], config(), CUTOFF).summary.groups[0]
    assert stat(overall, Metric.ENGAGEMENT_COUNT).median == "16.000000"
    assert stat(overall, Metric.ENGAGEMENT_RATE).median == "0.040000"
    assert stat(overall, Metric.REPLY_RATE).median == "0.005000"
    assert stat(overall, Metric.REPOST_RATE).median == "0.007500"


def test_pooled_rate_is_sum_over_sum_not_mean_of_rates():
    posts = [
        post(1, metrics={"likes": 1, "replies": 0, "reposts": 0, "quotes": 0, "impressions": 10}),
        post(2, metrics={"likes": 1, "replies": 0, "reposts": 0, "quotes": 0, "impressions": 990}),
    ]
    rate = stat(evaluate(posts, config(), CUTOFF).summary.groups[0], Metric.ENGAGEMENT_RATE)
    assert rate.pooled_rate == "0.002000"  # 2 / 1000
    assert rate.mean == "0.050505"  # (0.1 + 0.00101...) / 2
    assert rate.impressions_sum == 1000


# --- 8. mean / median / dispersion -------------------------------------------------------------


def test_mean_median_quartiles_and_dispersion():
    likes = [1, 2, 3, 4, 100]
    posts = [post(i, metrics={"likes": v}) for i, v in enumerate(likes)]
    s = stat(evaluate(posts, config(min_dispersion_n=5), CUTOFF).summary.groups[0], Metric.LIKES)
    assert (s.mean, s.median, s.p25, s.p75) == ("22.000000", "3.000000", "2.000000", "4.000000")
    assert (s.min, s.max) == ("1.000000", "100.000000")
    assert s.stdev == "39.012818"  # population standard deviation
    few = stat(
        evaluate(posts[:4], config(min_dispersion_n=5), CUTOFF).summary.groups[0], Metric.LIKES
    )
    assert few.stdev is None and few.p25 is None and few.median == "2.500000"


def test_median_and_quantile_helpers_are_exact():
    assert median([Fraction(1), Fraction(2)]) == Fraction(3, 2)
    assert quantile([Fraction(v) for v in (1, 2, 3, 4)], Fraction(1, 4)) == Fraction(7, 4)


# --- 9. hook / 10. strategy version / 11. UTC hour / length -----------------------------------


def comparable_data():
    posts = [post(i, hook="number", metrics={"likes": 40}) for i in range(6)]
    posts += [post(i, hook="how_to", metrics={"likes": 20}) for i in range(6, 12)]
    return posts


def test_hook_comparison_reports_differences_and_is_labelled_not_causal():
    result = evaluate(comparable_data(), config(), CUTOFF)
    [c] = [c for c in result.summary.comparisons if c.dimension is Dimension.HOOK_TYPE]
    assert c.comparison_id == "cmp:hook_type:how_to:vs:number"
    assert (c.n_a, c.n_b) == (6, 6)
    likes = next(d for d in c.differences if d.metric is Metric.LIKES)
    assert (likes.median_a, likes.median_b, likes.median_difference) == (
        "20.000000",
        "40.000000",
        "-20.000000",
    )
    assert "not causal" in c.interpretation
    assert not hasattr(c, "winner")


def test_strategy_versions_are_separate_groups_and_conflicting_bodies_are_not_merged():
    posts = [post(i, strategy_version=1) for i in range(3)]
    posts += [post(i, strategy_version=2) for i in range(3, 6)]
    posts += [post(6, strategy_version=2, strategy_digest="b" * 64)]
    result = evaluate(posts, config(), CUTOFF)
    values = sorted(
        g.value for g in result.summary.groups if g.dimension is Dimension.STRATEGY_VERSION
    )
    assert values == ["strat_a@v1", "strat_a@v2#aaaaaaaa", "strat_a@v2#bbbbbbbb"]


def test_confounders_name_dimensions_that_separate_the_groups():
    posts = [post(i, hook="number", strategy_version=2) for i in range(6)]
    posts += [post(i, hook="how_to", strategy_version=1) for i in range(6, 12)]
    result = evaluate(posts, config(), CUTOFF)
    [c] = [c for c in result.summary.comparisons if c.dimension is Dimension.HOOK_TYPE]
    assert any("strategy_version" in note for note in c.confounders)
    assert result.summary.confounders and result.summary.limitations


def test_posting_hour_is_utc_from_the_snapshot_and_blocked():
    local = datetime(2026, 10, 3, 1, 30, tzinfo=timedelta_tz(+2))  # 23:30 UTC the day before
    result = evaluate([post(1, published=local)], config(hour_block_hours=4), CUTOFF)
    o = obs(result, 1)
    assert (o.hour_utc, o.hour_block_utc) == (23, "20-23")
    assert hour_block(0, 4) == "00-03" and hour_block(5, 1) == "05-05"


def timedelta_tz(hours: int):
    from datetime import timezone

    return timezone(timedelta(hours=hours))


def test_length_buckets_are_in_code_points():
    assert [length_bucket(n, (100, 200)) for n in (1, 100, 101, 200, 201)] == [
        "1-100",
        "1-100",
        "101-200",
        "101-200",
        "201+",
    ]
    result = evaluate([post(1, length=150)], config(), CUTOFF)
    assert obs(result, 1).length_bucket == "101-200"


def test_format_is_only_a_dimension_when_it_varies():
    result = evaluate(comparable_data(), config(), CUTOFF)
    assert all(g.dimension is not Dimension.FORMAT for g in result.summary.groups)


# --- 13. reproducibility -------------------------------------------------------------------------


def test_the_same_inputs_give_the_same_digest_in_any_order():
    posts = comparable_data()
    request = {"account_id": "acct", "data_cutoff": CUTOFF.isoformat()}
    a = evaluate(posts, config(), CUTOFF)
    b = evaluate(list(reversed(posts)), config(), CUTOFF)
    assert evaluation_digest(request, config(), a) == evaluation_digest(request, config(), b)
    changed = [replace(posts[0], snapshot=None), *posts[1:]]
    c = evaluate(changed, config(), CUTOFF)
    assert evaluation_digest(request, config(), c) != evaluation_digest(request, config(), a)
    assert evaluation_digest(request, config(min_group_n=4), a) != evaluation_digest(
        request, config(), a
    )


DIGEST_SCRIPT = """
from tests.test_evaluation_stats import CUTOFF, comparable_data, config
from social_growth_agent.analytics.evaluation import evaluate, evaluation_digest
request = {"account_id": "acct", "data_cutoff": CUTOFF.isoformat()}
print(evaluation_digest(request, config(), evaluate(comparable_data(), config(), CUTOFF)))
"""


def test_the_digest_is_identical_in_separate_processes():
    request = {"account_id": "acct", "data_cutoff": CUTOFF.isoformat()}
    here = evaluation_digest(request, config(), evaluate(comparable_data(), config(), CUTOFF))
    root = Path(__file__).resolve().parents[1]
    digests = {
        subprocess.run(
            [sys.executable, "-c", DIGEST_SCRIPT],
            cwd=root,
            env=os.environ | {"PYTHONHASHSEED": seed, "TZ": tz},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for seed, tz in (("1", "UTC"), ("2", "Asia/Tokyo"))
    }
    assert digests == {here}
