"""Phase 7 types: deterministic performance evaluations and human-reviewed strategy proposals.

Observation, statistics, AI interpretation and human decisions are separate types:

- ``Observation`` / ``EvaluationSummary``: what the stored metrics say, computed in code.
- ``StrategyChange`` / ``ValidatedProposal``: what a model suggested, after deterministic
  validation (the raw model output is never trusted).
- Decisions, versions and activations are database records written only by explicit
  human actions (``persistence.strategies``).

Every number in an evaluation is a fixed-precision string, so serializing the same data
on any machine gives the same bytes (and the same digest).
"""

from enum import StrEnum

from pydantic import Field, JsonValue

from social_growth_agent.models.account import ContentStrategy
from social_growth_agent.models.base import DomainModel

# --- evaluation ---------------------------------------------------------------------------

SYNTHETIC_METRIC_PROVIDERS = frozenset({"mock_analytics"})
"""Analytics providers whose snapshots are synthetic. They never enter a real evaluation,
and real snapshots never enter a synthetic one."""


class EvidenceLevel(StrEnum):
    INSUFFICIENT = "insufficient"
    """Fewer eligible posts than ``min_group_n``: shown, never compared."""
    DESCRIPTIVE = "descriptive"
    """Enough to describe, not enough to compare."""
    COMPARABLE = "comparable"
    """At least ``min_compare_n`` posts; may appear in a comparison."""


class EvaluationStatus(StrEnum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    """Fewer than ``min_total_n`` eligible posts: no comparison is emitted at all."""


class ExclusionReason(StrEnum):
    NO_SNAPSHOT = "no_snapshot"
    NOT_YET_DUE = "not_yet_due"
    OFF_TARGET_LATE = "off_target_late"
    OFF_TARGET_EARLY = "off_target_early"
    NO_PROVIDER_CREATED_AT = "no_provider_created_at"
    SYNTHETIC_METRICS = "synthetic_metrics"
    REAL_METRICS_IN_SYNTHETIC_EVALUATION = "real_metrics_in_synthetic_evaluation"


class Dimension(StrEnum):
    HOOK_TYPE = "hook_type"
    STRATEGY_VERSION = "strategy_version"
    LENGTH_BUCKET = "length_bucket"
    HOUR_BLOCK_UTC = "hour_block_utc"
    ORIGIN = "origin"
    FORMAT = "format"


class Metric(StrEnum):
    LIKES = "likes"
    REPLIES = "replies"
    REPOSTS = "reposts"
    QUOTES = "quotes"
    BOOKMARKS = "bookmarks"
    IMPRESSIONS = "impressions"
    ENGAGEMENT_COUNT = "engagement_count"
    """likes + reposts + replies + quotes (bookmarks excluded); None if any part is."""
    ENGAGEMENT_RATE = "engagement_rate"
    """engagement_count / impressions; only with impressions > 0."""
    REPLY_RATE = "reply_rate"
    REPOST_RATE = "repost_rate"


RATE_METRICS = frozenset({Metric.ENGAGEMENT_RATE, Metric.REPLY_RATE, Metric.REPOST_RATE})


class Observation(DomainModel):
    """One published post's part in one evaluation: eligible, or excluded with a reason.

    Counts are copied from the snapshot (small, immutable) so an evaluation can be
    verified on its own; content and lineage are reached through the foreign keys."""

    publication_id: str
    run_id: str
    candidate_id: str
    post_metric_id: str | None
    eligible: bool
    exclusion_reason: ExclusionReason | None
    metrics_provider: str | None
    actual_age_seconds: int | None
    hook_type: str
    origin: str
    format: str
    strategy_id: str
    strategy_version: int
    strategy_digest: str
    strategy_group: str
    length_chars: int
    length_bucket: str
    hour_utc: int | None
    hour_block_utc: str | None
    likes: int | None
    replies: int | None
    reposts: int | None
    quotes: int | None
    bookmarks: int | None
    impressions: int | None


class MetricStats(DomainModel):
    metric: Metric
    n: int = Field(description="Posts in the group that reported this metric.")
    mean: str | None
    median: str | None
    p25: str | None
    p75: str | None
    min: str | None
    max: str | None
    stdev: str | None = Field(description="Population standard deviation; only n >= minimum.")
    pooled_rate: str | None = Field(
        default=None, description="Rates only: sum of numerators / sum of impressions."
    )
    denominator_n: int | None = Field(default=None, description="Rates: posts with impressions.")
    impressions_sum: int | None = None


class GroupStats(DomainModel):
    group_id: str
    dimension: Dimension | None = Field(description="None for the overall group.")
    value: str
    n_eligible: int
    evidence_level: EvidenceLevel
    metrics: list[MetricStats]


class MetricDifference(DomainModel):
    metric: Metric
    n_a: int
    n_b: int
    median_a: str | None
    median_b: str | None
    median_difference: str | None = Field(description="median_a - median_b")
    pooled_a: str | None = None
    pooled_b: str | None = None
    pooled_difference: str | None = None


class Comparison(DomainModel):
    comparison_id: str
    dimension: Dimension
    group_a: str
    group_b: str
    n_a: int
    n_b: int
    differences: list[MetricDifference]
    interpretation: str = "observed difference between groups; not causal"
    confounders: list[str]


class EvaluationSummary(DomainModel):
    status: EvaluationStatus
    snapshot_age: str
    eligible_count: int
    excluded_count: int
    excluded_by_reason: dict[str, int]
    groups: list[GroupStats]
    comparisons: list[Comparison]
    confounders: list[str]
    limitations: list[str]


# --- proposals ------------------------------------------------------------------------------


class ProposableField(StrEnum):
    """The only strategy fields a proposal may change. Both are soft preferences: the
    content policy (length limit, formats) and human review decisions always win."""

    PREFERRED_HOOKS = "preferred_hooks"
    LENGTH_GUIDANCE = "length_guidance"


FIELD_EVIDENCE: dict[ProposableField, Dimension] = {
    ProposableField.PREFERRED_HOOKS: Dimension.HOOK_TYPE,
    ProposableField.LENGTH_GUIDANCE: Dimension.LENGTH_BUCKET,
}
"""A change may only cite comparisons along the dimension it is about."""


class ProposalOutcome(StrEnum):
    CHANGE = "change"
    NO_CHANGE = "no_change"


class ProposalConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"


class ExpectedDirection(StrEnum):
    INCREASE = "increase"
    DECREASE = "decrease"


class ProposalStatus(StrEnum):
    GENERATING = "generating"
    """Committed before the model call (the started ledger); finalized after it."""
    PENDING_REVIEW = "pending_review"
    FAILED = "failed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    """The active strategy changed after the proposal was made; it can no longer apply."""
    ABANDONED = "abandoned"
    """A stale ``generating`` proposal closed by an explicit retry."""


OPEN_PROPOSAL_STATUSES = (ProposalStatus.GENERATING, ProposalStatus.PENDING_REVIEW)
RETRYABLE_PROPOSAL_STATUSES = (ProposalStatus.FAILED, ProposalStatus.GENERATING)
"""``generating`` only once it is stale (older than twice the model timeout)."""


class Proposer(StrEnum):
    OPENAI = "openai"
    FAKE = "fake"
    DETERMINISTIC = "deterministic"
    """No model call: an insufficient-evidence evaluation can only produce no change."""


class DecisionKind(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ReviewAuthMethod(StrEnum):
    REVIEW_TOKEN = "review_token"
    """The caller presented STRATEGY_REVIEW_TOKEN. Proves possession, not identity."""
    UNAUTHENTICATED_LOCAL = "unauthenticated_local"
    """Development only (APP_ENVIRONMENT=development plus an explicit opt-in)."""


class StrategyVersionOrigin(StrEnum):
    REGISTERED = "registered"
    IMPORTED_FROM_RUN = "imported_from_run"
    PROPOSAL = "proposal"
    EXPERIMENT = "experiment"
    """Phase 8: adopted (as an inactive version) from a concluded, confirmatory experiment."""


class StrategyChange(DomainModel):
    """One validated change. ``current_value`` is filled in from the stored strategy,
    never taken from the model."""

    field: ProposableField
    current_value: JsonValue
    proposed_value: JsonValue
    evidence_refs: list[str]
    expected_metric: Metric
    expected_direction: ExpectedDirection
    hypothesis: str = Field(description="Always labelled as a hypothesis, never a prediction.")
    confidence: ProposalConfidence
    rationale: str
    limitations: list[str]
    confounders: list[str]


class ValidatedProposal(DomainModel):
    outcome: ProposalOutcome
    summary: str
    changes: list[StrategyChange]
    posting_time_note: str | None = Field(
        description="Advisory only: posting time is not a strategy field and nothing "
        "schedules from it."
    )
    posting_time_evidence_refs: list[str]
    proposed_strategy: ContentStrategy | None = Field(
        description="The full strategy the approval would create (version not yet set)."
    )
