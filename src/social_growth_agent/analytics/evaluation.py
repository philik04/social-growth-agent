"""Deterministic performance evaluation (Phase 7). Pure: no I/O, no LLM, no platform calls.

Input: every published post in scope with (at most) its snapshot at one age. Output: one
``Observation`` per post (eligible, or excluded with a reason) and an
``EvaluationSummary`` of descriptive statistics.

Rules that keep it honest:

- **Age matching.** One snapshot age per evaluation. A snapshot is eligible only when its
  actual age (``captured_at - provider_created_at``, both from the snapshot itself) is on
  target. Late and early snapshots are listed with their reason, never used.
- **Missing stays missing.** A metric the platform did not report shrinks ``n`` for that
  metric only; it never becomes 0. Rates need impressions > 0.
- **Descriptive only.** Mean, median, quartiles and (with enough data) the standard
  deviation. No significance tests, no winner. A comparison exists only between groups
  that both reach ``min_compare_n``, and only when the whole evaluation reaches
  ``min_total_n``. Every comparison names its confounders.
- **Reproducible.** Inputs are immutable once the cutoff is fixed, arithmetic is exact
  (``Fraction``) and every number is serialized with fixed precision, so the same request
  always produces the same digest.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction
from itertools import combinations
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from social_growth_agent.analytics.derived import total_engagement
from social_growth_agent.models.strategy import (
    RATE_METRICS,
    SYNTHETIC_METRIC_PROVIDERS,
    Comparison,
    Dimension,
    EvaluationStatus,
    EvaluationSummary,
    EvidenceLevel,
    ExclusionReason,
    GroupStats,
    Metric,
    MetricDifference,
    MetricStats,
    Observation,
)
from social_growth_agent.policies.analytics import is_on_target

CONFIG_VERSION: Literal["eval-v1"] = "eval-v1"
PRECISION = Decimal("0.000001")
MIN_FLOORS = {"min_group_n": 3, "min_compare_n": 5, "min_total_n": 10}
"""A request may raise the thresholds, never lower them below these."""

COMPARED_METRICS = (
    Metric.LIKES,
    Metric.ENGAGEMENT_COUNT,
    Metric.ENGAGEMENT_RATE,
    Metric.REPLY_RATE,
    Metric.REPOST_RATE,
)

FIXED_CONFOUNDERS = (
    "time trend: follower count and reach change over time",
    "topic and news cycle differ between posts",
    "strategy versions are used in sequence, so they coincide with calendar time",
    "hook types are declared by the Content Agent and not verified",
    "posts edited by a human keep the hook label of the draft they started from",
)
LIMITATIONS = (
    "descriptive statistics only: no significance tests and no winner",
    "observational data: differences between groups do not show causes",
    "public metrics only, as reported by the platform at capture time",
    "post length is counted in characters (code points), not the weighted length X uses",
    "groups below the minimum sample size are shown but never compared",
)


type HourBlock = Literal[1, 2, 3, 4, 6, 8, 12, 24]


class EvaluationConfig(BaseModel):
    """Everything that changes the result; stored with (and hashed into) each evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_version: Literal["eval-v1"] = CONFIG_VERSION
    snapshot_age: str
    snapshot_age_seconds: int = Field(ge=1)
    on_target_min_seconds: int = Field(default=120, ge=0)
    min_group_n: int = Field(default=5, ge=MIN_FLOORS["min_group_n"], le=1000)
    min_compare_n: int = Field(default=10, ge=MIN_FLOORS["min_compare_n"], le=1000)
    min_total_n: int = Field(default=20, ge=MIN_FLOORS["min_total_n"], le=10_000)
    min_dispersion_n: int = Field(default=5, ge=2, le=1000)
    hour_block_hours: HourBlock = 4
    length_bucket_edges: tuple[int, ...] = (100, 200)
    synthetic: bool = Field(
        default=False,
        description="Evaluate synthetic (mock) snapshots instead of real ones. The two are "
        "never mixed.",
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.min_compare_n < self.min_group_n:
            raise ValueError("min_compare_n must be at least min_group_n")
        edges = self.length_bucket_edges
        if any(e < 1 for e in edges) or list(edges) != sorted(set(edges)):
            raise ValueError("length_bucket_edges must be positive and strictly increasing")
        if len(edges) > 10:
            raise ValueError("at most 10 length bucket edges")
        return self


@dataclass(frozen=True)
class SnapshotInput:
    post_metric_id: str
    provider: str
    captured_at: datetime
    provider_created_at: datetime | None
    likes: int | None
    replies: int | None
    reposts: int | None
    quotes: int | None
    bookmarks: int | None
    impressions: int | None


@dataclass(frozen=True)
class PostInput:
    """A published post in scope. ``snapshot`` is its snapshot at the evaluated age."""

    publication_id: str
    run_id: str
    candidate_id: str
    content: str
    published_at: datetime
    hook_type: str
    origin: str
    format: str
    strategy_id: str
    strategy_version: int
    strategy_digest: str
    snapshot: SnapshotInput | None


@dataclass(frozen=True)
class EvaluationResult:
    observations: list[Observation]
    summary: EvaluationSummary


# --- classification ---------------------------------------------------------------------


def evaluate(
    posts: Sequence[PostInput], config: EvaluationConfig, cutoff: datetime
) -> EvaluationResult:
    groups = _strategy_groups(posts)
    observations = sorted(
        (classify(p, config, cutoff, groups[p.publication_id]) for p in posts),
        key=lambda o: o.publication_id,
    )
    return EvaluationResult(observations=observations, summary=summarize(observations, config))


def classify(
    post: PostInput, config: EvaluationConfig, cutoff: datetime, strategy_group: str
) -> Observation:
    snap = post.snapshot
    if snap is not None and snap.captured_at > cutoff:
        snap = None  # captured after the cutoff: as if it did not exist yet
    reason: ExclusionReason | None = None
    actual_age: int | None = None
    if snap is None:
        due = post.published_at + timedelta(seconds=config.snapshot_age_seconds)
        reason = ExclusionReason.NOT_YET_DUE if due > cutoff else ExclusionReason.NO_SNAPSHOT
    else:
        synthetic = snap.provider in SYNTHETIC_METRIC_PROVIDERS
        if synthetic and not config.synthetic:
            reason = ExclusionReason.SYNTHETIC_METRICS
        elif config.synthetic and not synthetic:
            reason = ExclusionReason.REAL_METRICS_IN_SYNTHETIC_EVALUATION
        elif snap.provider_created_at is None:
            reason = ExclusionReason.NO_PROVIDER_CREATED_AT
        if snap.provider_created_at is not None:
            actual_age = int((snap.captured_at - snap.provider_created_at).total_seconds())
            on_target = is_on_target(
                actual_age, config.snapshot_age_seconds, config.on_target_min_seconds
            )
            if reason is None and not on_target:
                late = actual_age > config.snapshot_age_seconds
                reason = (
                    ExclusionReason.OFF_TARGET_LATE if late else ExclusionReason.OFF_TARGET_EARLY
                )
    created = snap.provider_created_at if snap is not None else None
    hour = created.astimezone(UTC).hour if created is not None else None
    length = len(post.content)
    return Observation(
        publication_id=post.publication_id,
        run_id=post.run_id,
        candidate_id=post.candidate_id,
        post_metric_id=snap.post_metric_id if snap is not None else None,
        eligible=reason is None,
        exclusion_reason=reason,
        metrics_provider=snap.provider if snap is not None else None,
        actual_age_seconds=actual_age,
        hook_type=post.hook_type,
        origin=post.origin,
        format=post.format,
        strategy_id=post.strategy_id,
        strategy_version=post.strategy_version,
        strategy_digest=post.strategy_digest,
        strategy_group=strategy_group,
        length_chars=length,
        length_bucket=length_bucket(length, config.length_bucket_edges),
        hour_utc=hour,
        hour_block_utc=None if hour is None else hour_block(hour, config.hour_block_hours),
        likes=snap.likes if snap is not None else None,
        replies=snap.replies if snap is not None else None,
        reposts=snap.reposts if snap is not None else None,
        quotes=snap.quotes if snap is not None else None,
        bookmarks=snap.bookmarks if snap is not None else None,
        impressions=snap.impressions if snap is not None else None,
    )


def _strategy_groups(posts: Sequence[PostInput]) -> dict[str, str]:
    """``id@vN``; when one (id, version) carries different bodies in this population, each
    body becomes its own group (``id@vN#digest8``) instead of being silently merged."""
    bodies: dict[tuple[str, int], set[str]] = defaultdict(set)
    for p in posts:
        bodies[(p.strategy_id, p.strategy_version)].add(p.strategy_digest)
    out: dict[str, str] = {}
    for p in posts:
        key = f"{p.strategy_id}@v{p.strategy_version}"
        if len(bodies[(p.strategy_id, p.strategy_version)]) > 1:
            key += f"#{p.strategy_digest[:8]}"
        out[p.publication_id] = key
    return out


def length_bucket(length: int, edges: Sequence[int]) -> str:
    lower = 1
    for edge in edges:
        if length <= edge:
            return f"{lower}-{edge}"
        lower = edge + 1
    return f"{lower}+"


def hour_block(hour: int, width: int) -> str:
    start = (hour // width) * width
    return f"{start:02d}-{start + width - 1:02d}"


# --- statistics ---------------------------------------------------------------------------


def metric_value(o: Observation, metric: Metric) -> Fraction | None:
    """The value of one metric for one observation; None when not computable."""
    engagement = total_engagement(o.likes, o.reposts, o.replies, o.quotes)
    numerators: dict[Metric, int | None] = {
        Metric.ENGAGEMENT_RATE: engagement,
        Metric.REPLY_RATE: o.replies,
        Metric.REPOST_RATE: o.reposts,
    }
    if metric in RATE_METRICS:
        num = numerators[metric]
        if num is None or o.impressions is None or o.impressions <= 0:
            return None
        return Fraction(num, o.impressions)
    counts: dict[Metric, int | None] = {
        Metric.LIKES: o.likes,
        Metric.REPLIES: o.replies,
        Metric.REPOSTS: o.reposts,
        Metric.QUOTES: o.quotes,
        Metric.BOOKMARKS: o.bookmarks,
        Metric.IMPRESSIONS: o.impressions,
        Metric.ENGAGEMENT_COUNT: engagement,
    }
    value = counts[metric]
    return None if value is None else Fraction(value)


def _rate_parts(o: Observation, metric: Metric) -> tuple[int, int] | None:
    value = metric_value(o, metric)
    if value is None or o.impressions is None:
        return None
    return round(value * o.impressions), o.impressions


def fmt(value: Fraction | None) -> str | None:
    if value is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 60
        exact = Decimal(value.numerator) / Decimal(value.denominator)
        return str(exact.quantize(PRECISION, rounding=ROUND_HALF_EVEN))


def median(values: Sequence[Fraction]) -> Fraction:
    xs = sorted(values)
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def quantile(values: Sequence[Fraction], q: Fraction) -> Fraction:
    """Linear interpolation between closest ranks (inclusive method)."""
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo = int(pos)
    if lo + 1 >= len(xs):
        return xs[-1]
    return xs[lo] + (xs[lo + 1] - xs[lo]) * (pos - lo)


def stdev(values: Sequence[Fraction]) -> str:
    mean = sum(values, Fraction(0)) / len(values)
    variance = sum(((v - mean) ** 2 for v in values), Fraction(0)) / len(values)
    with localcontext() as ctx:
        ctx.prec = 60
        root = (Decimal(variance.numerator) / Decimal(variance.denominator)).sqrt()
        return str(root.quantize(PRECISION, rounding=ROUND_HALF_EVEN))


def metric_stats(
    members: Sequence[Observation], metric: Metric, config: EvaluationConfig
) -> MetricStats:
    values = [v for v in (metric_value(o, metric) for o in members) if v is not None]
    n = len(values)
    pooled: dict[str, Any] = {}
    if metric in RATE_METRICS:
        parts = [p for p in (_rate_parts(o, metric) for o in members) if p is not None]
        total_impressions = sum(d for _, d in parts)
        pooled = {
            "pooled_rate": fmt(Fraction(sum(num for num, _ in parts), total_impressions))
            if total_impressions > 0
            else None,
            "denominator_n": len(parts),
            "impressions_sum": total_impressions,
        }
    dispersed = n >= config.min_dispersion_n
    return MetricStats(
        metric=metric,
        n=n,
        mean=fmt(sum(values, Fraction(0)) / n) if n else None,
        median=fmt(median(values)) if n else None,
        p25=fmt(quantile(values, Fraction(1, 4))) if dispersed else None,
        p75=fmt(quantile(values, Fraction(3, 4))) if dispersed else None,
        min=fmt(min(values)) if n else None,
        max=fmt(max(values)) if n else None,
        stdev=stdev(values) if dispersed else None,
        **pooled,
    )


def evidence_level(n: int, config: EvaluationConfig) -> EvidenceLevel:
    if n >= config.min_compare_n:
        return EvidenceLevel.COMPARABLE
    if n >= config.min_group_n:
        return EvidenceLevel.DESCRIPTIVE
    return EvidenceLevel.INSUFFICIENT


DIMENSION_VALUE: dict[Dimension, Callable[[Observation], str | None]] = {
    Dimension.HOOK_TYPE: lambda o: o.hook_type,
    Dimension.STRATEGY_VERSION: lambda o: o.strategy_group,
    Dimension.LENGTH_BUCKET: lambda o: o.length_bucket,
    Dimension.HOUR_BLOCK_UTC: lambda o: o.hour_block_utc,
    Dimension.ORIGIN: lambda o: o.origin,
    Dimension.FORMAT: lambda o: o.format,
}


def group_id(dimension: Dimension | None, value: str) -> str:
    return "grp:all" if dimension is None else f"grp:{dimension.value}:{value}"


def summarize(observations: Sequence[Observation], config: EvaluationConfig) -> EvaluationSummary:
    eligible = [o for o in observations if o.eligible]
    excluded = [o for o in observations if not o.eligible]
    by_reason: dict[str, int] = defaultdict(int)
    for o in excluded:
        assert o.exclusion_reason is not None
        by_reason[o.exclusion_reason.value] += 1
    sufficient = len(eligible) >= config.min_total_n
    groups = [_group(None, "all", eligible, config)]
    comparisons: list[Comparison] = []
    for dimension, members_by_value in _dimension_groups(eligible).items():
        dimension_groups = [_group(dimension, v, m, config) for v, m in members_by_value.items()]
        groups.extend(dimension_groups)
        if not sufficient:
            continue
        comparable = [g for g in dimension_groups if g.evidence_level is EvidenceLevel.COMPARABLE]
        for a, b in combinations(comparable, 2):
            comparisons.append(_compare(dimension, a.value, b.value, members_by_value, config))
    return EvaluationSummary(
        status=EvaluationStatus.SUFFICIENT
        if sufficient
        else EvaluationStatus.INSUFFICIENT_EVIDENCE,
        snapshot_age=config.snapshot_age,
        eligible_count=len(eligible),
        excluded_count=len(excluded),
        excluded_by_reason=dict(sorted(by_reason.items())),
        groups=groups,
        comparisons=comparisons,
        confounders=list(FIXED_CONFOUNDERS),
        limitations=[*LIMITATIONS, f"snapshot age {config.snapshot_age} only"],
    )


def _dimension_groups(
    eligible: Iterable[Observation],
) -> dict[Dimension, dict[str, list[Observation]]]:
    out: dict[Dimension, dict[str, list[Observation]]] = {}
    for dimension, value_of in DIMENSION_VALUE.items():
        buckets: dict[str, list[Observation]] = defaultdict(list)
        for o in eligible:
            value = value_of(o)
            if value is not None:
                buckets[value].append(o)
        if dimension is Dimension.FORMAT and len(buckets) < 2:
            continue  # only shown when it varies
        out[dimension] = dict(sorted(buckets.items()))
    return out


def _group(
    dimension: Dimension | None,
    value: str,
    members: Sequence[Observation],
    config: EvaluationConfig,
) -> GroupStats:
    return GroupStats(
        group_id=group_id(dimension, value),
        dimension=dimension,
        value=value,
        n_eligible=len(members),
        evidence_level=evidence_level(len(members), config),
        metrics=[metric_stats(members, m, config) for m in Metric],
    )


def comparison_id(dimension: Dimension, a: str, b: str) -> str:
    return f"cmp:{dimension.value}:{a}:vs:{b}"


def _compare(
    dimension: Dimension,
    a: str,
    b: str,
    members_by_value: dict[str, list[Observation]],
    config: EvaluationConfig,
) -> Comparison:
    ma, mb = members_by_value[a], members_by_value[b]
    differences = []
    for metric in COMPARED_METRICS:
        va = [v for v in (metric_value(o, metric) for o in ma) if v is not None]
        vb = [v for v in (metric_value(o, metric) for o in mb) if v is not None]
        med_a = median(va) if va else None
        med_b = median(vb) if vb else None
        pooled: dict[str, str | None] = {}
        if metric in RATE_METRICS:
            pa, pb = _pooled(ma, metric), _pooled(mb, metric)
            pooled = {
                "pooled_a": fmt(pa),
                "pooled_b": fmt(pb),
                "pooled_difference": fmt(pa - pb) if pa is not None and pb is not None else None,
            }
        differences.append(
            MetricDifference(
                metric=metric,
                n_a=len(va),
                n_b=len(vb),
                median_a=fmt(med_a),
                median_b=fmt(med_b),
                median_difference=(
                    fmt(med_a - med_b) if med_a is not None and med_b is not None else None
                ),
                **pooled,
            )
        )
    return Comparison(
        comparison_id=comparison_id(dimension, a, b),
        dimension=dimension,
        group_a=a,
        group_b=b,
        n_a=len(ma),
        n_b=len(mb),
        differences=differences,
        confounders=_confounders(dimension, a, b, ma, mb),
    )


def _pooled(members: Sequence[Observation], metric: Metric) -> Fraction | None:
    parts = [p for p in (_rate_parts(o, metric) for o in members) if p is not None]
    total = sum(d for _, d in parts)
    return Fraction(sum(n for n, _ in parts), total) if total > 0 else None


def _confounders(
    dimension: Dimension,
    a: str,
    b: str,
    ma: Sequence[Observation],
    mb: Sequence[Observation],
) -> list[str]:
    """Data-specific confounders: another dimension that separates the two groups
    completely, so its effect cannot be told apart from this one's."""
    notes = []
    for other, value_of in DIMENSION_VALUE.items():
        if other is dimension:
            continue
        va = {value_of(o) for o in ma}
        vb = {value_of(o) for o in mb}
        if len(va) == 1 and len(vb) == 1 and va != vb:
            notes.append(
                f"every {a} post has {other.value} {va.pop()} and every {b} post has "
                f"{other.value} {vb.pop()}"
            )
        elif va.isdisjoint(vb) and None not in va | vb:
            notes.append(f"{a} and {b} posts share no {other.value} value")
    notes.append("small samples: medians can move a lot with a few posts")
    return notes


# --- digests ---------------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def evaluation_digest(
    request: dict[str, Any], config: EvaluationConfig, result: EvaluationResult
) -> str:
    return sha256_json(
        {
            "request": request,
            "config": config.model_dump(mode="json"),
            "observations": [o.model_dump(mode="json") for o in result.observations],
            "summary": result.summary.model_dump(mode="json"),
        }
    )
