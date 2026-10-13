"""The compact evidence digest the Strategy Agent reads (Phase 8, prompt ``strategy-v2``).

Phase 7 sent the whole evaluation summary (every dimension, every metric, every
percentile, per-comparison confounders), around 19k input tokens on a realistic fixture.
The model may only change ``preferred_hooks`` and ``length_guidance`` and add a posting
time note, so the digest keeps only what those decisions can cite:

- groups along ``hook_type``, ``length_bucket`` and ``hour_block_utc`` that are at least
  descriptive, with the three rate metrics (n, median, pooled rate);
- comparisons along the same dimensions, with the rate differences only;
- the evaluation-wide confounders.

``digest_summary`` returns the reduced summary as an ``EvaluationSummary``, and the
proposal is validated against *that*: evidence references and every number the model
writes must appear in what it was shown. If the digest exceeds ``MAX_DIGEST_CHARS`` it is
reduced deterministically (descriptive groups first, then posting-time data, then the
comparisons with the smallest samples), and the omissions are listed in the digest.
"""

import json

from pydantic import JsonValue

from social_growth_agent.models.strategy import (
    RATE_METRICS,
    Comparison,
    Dimension,
    EvaluationSummary,
    EvidenceLevel,
    GroupStats,
    MetricStats,
)

DIGEST_VERSION = "digest-v1"
MAX_DIGEST_CHARS = 12_000
DIGEST_DIMENSIONS = (Dimension.HOOK_TYPE, Dimension.LENGTH_BUCKET, Dimension.HOUR_BLOCK_UTC)
_METRICS = tuple(sorted(RATE_METRICS, key=lambda m: m.value))
_LEGEND = {
    "comparison.id": "cmp:<dimension>:<a>:vs:<b>; cite these ids as evidence_refs",
    "group.rates": "metric -> [n, median, pooled_rate]",
    "comparison.rates": "metric -> [median_difference, pooled_difference], a minus b",
}


def digest_summary(
    summary: EvaluationSummary, *, max_chars: int = MAX_DIGEST_CHARS
) -> tuple[EvaluationSummary, list[str]]:
    """The reduced summary the model sees, and what was omitted to fit the budget."""
    groups = [
        _slim_group(g)
        for g in summary.groups
        if g.dimension in DIGEST_DIMENSIONS and g.evidence_level is not EvidenceLevel.INSUFFICIENT
    ]
    comparisons = [
        _slim_comparison(c) for c in summary.comparisons if c.dimension in DIGEST_DIMENSIONS
    ]
    omitted: list[str] = []

    def build() -> EvaluationSummary:
        return summary.model_copy(
            update={
                "groups": groups,
                "comparisons": comparisons,
                "excluded_by_reason": {},
                "limitations": [],
            }
        )

    reduced = build()
    if len(render(reduced, omitted)) > max_chars:
        cited = {c.group_a for c in comparisons} | {c.group_b for c in comparisons}
        groups = [
            g for g in groups if g.evidence_level is EvidenceLevel.COMPARABLE or g.value in cited
        ]
        omitted.append("descriptive-only groups")
        reduced = build()
    if len(render(reduced, omitted)) > max_chars:
        hours = Dimension.HOUR_BLOCK_UTC
        if any(g.dimension is hours for g in groups) or any(
            c.dimension is hours for c in comparisons
        ):
            groups = [g for g in groups if g.dimension is not hours]
            comparisons = [c for c in comparisons if c.dimension is not hours]
            omitted.append("posting-time (hour_block_utc) groups and comparisons")
            reduced = build()
    while len(render(reduced, omitted)) > max_chars and comparisons:
        smallest = min(comparisons, key=lambda c: (min(c.n_a, c.n_b), c.n_a + c.n_b))
        comparisons = [c for c in comparisons if c is not smallest]
        if "comparisons with the smallest samples" not in omitted:
            omitted.append("comparisons with the smallest samples")
        reduced = build()
    return reduced, omitted


def render(summary: EvaluationSummary, omitted: list[str]) -> str:
    """Compact JSON (no indentation) of a reduced summary."""
    return json.dumps(digest_payload(summary, omitted), separators=(",", ":"))


def digest_payload(summary: EvaluationSummary, omitted: list[str]) -> dict[str, JsonValue]:
    groups: list[JsonValue] = [
        {
            "dim": g.dimension.value if g.dimension else None,
            "value": g.value,
            "n": g.n_eligible,
            "level": g.evidence_level.value,
            "rates": {m.metric.value: [m.n, m.median, m.pooled_rate] for m in g.metrics},
        }
        for g in summary.groups
    ]
    comparisons: list[JsonValue] = [
        {
            "id": c.comparison_id,
            "a": c.group_a,
            "b": c.group_b,
            "n": [c.n_a, c.n_b],
            "rates": {
                d.metric.value: [d.median_difference, d.pooled_difference] for d in c.differences
            },
        }
        for c in summary.comparisons
    ]
    legend: dict[str, JsonValue] = dict(_LEGEND)
    return {
        "digest": DIGEST_VERSION,
        "status": summary.status.value,
        "snapshot_age": summary.snapshot_age,
        "eligible": summary.eligible_count,
        "excluded": summary.excluded_count,
        "legend": legend,
        "groups": groups,
        "comparisons": comparisons,
        "confounders": list(summary.confounders),
        "omitted": list(omitted),
    }


def _slim_group(g: GroupStats) -> GroupStats:
    return g.model_copy(
        update={
            "metrics": [
                MetricStats(
                    metric=m.metric,
                    n=m.n,
                    mean=None,
                    median=m.median,
                    p25=None,
                    p75=None,
                    min=None,
                    max=None,
                    stdev=None,
                    pooled_rate=m.pooled_rate,
                )
                for m in g.metrics
                if m.metric in _METRICS
            ]
        }
    )


def _slim_comparison(c: Comparison) -> Comparison:
    return c.model_copy(
        update={
            "differences": [
                d.model_copy(
                    update={"median_a": None, "median_b": None, "pooled_a": None, "pooled_b": None}
                )
                for d in c.differences
                if d.metric in _METRICS
            ],
            "confounders": [],
        }
    )


def size_report(summary: EvaluationSummary) -> dict[str, int]:
    """Characters of the Phase 7 evaluation payload (``strategy-v1``: the full summary,
    indented) and of this digest. Characters, not tokens: the live demo reports the
    tokens the provider actually counted."""
    reduced, omitted = digest_summary(summary)
    return {
        "full_summary_chars": len(json.dumps(summary.model_dump(mode="json"), indent=1)),
        "digest_chars": len(render(reduced, omitted)),
    }
