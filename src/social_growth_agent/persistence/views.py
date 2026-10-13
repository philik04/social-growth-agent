"""Read models: what the API returns about runs. Built from the domain tables only."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from social_growth_agent.accounting import CostEstimate, UsageCounts
from social_growth_agent.analytics import ObservedRates
from social_growth_agent.models import (
    AnalyticsJobStatus,
    ContentStrategy,
    PublicationStatus,
    RunStatus,
)
from social_growth_agent.models.discovery import OpportunityEvidence
from social_growth_agent.models.experiment import (
    EvidenceClass,
    ExperimentDesign,
    ExperimentEventKind,
    ExperimentFactor,
    ExperimentStatus,
    Feasibility,
    ResultFlag,
    StopReason,
    Treatment,
    Verdict,
)
from social_growth_agent.models.strategy import (
    DecisionKind,
    EvaluationStatus,
    EvaluationSummary,
    ProposalOutcome,
    ProposalStatus,
    StrategyVersionOrigin,
    ValidatedProposal,
)


class View(BaseModel):
    model_config = ConfigDict(frozen=True)


class RunFailure(View):
    node: str | None
    error_type: str | None
    message: str | None


class RunSummary(View):
    id: str
    status: RunStatus
    current_node: str | None
    research_query: str
    strategy_id: str
    strategy_version: int
    strategy_version_ref: str | None = None
    """The registered strategy version this run used (Phase 7); None for unregistered
    inline strategies and older runs."""
    research_attempts: int
    generation_attempts: int
    regeneration_rounds: int
    pricing_id: str | None
    created_at: datetime
    updated_at: datetime
    failure: RunFailure | None
    opportunity_id: str | None = None
    """The discovery opportunity this run was started from (Phase 9)."""


class FetchView(View):
    id: str
    provider: str
    query: str
    effective_query: str
    outcome: str
    error_category: str | None
    requests_made: int
    posts_fetched: int
    users_fetched: int
    rate_limit_reset_at: datetime | None
    started_at: datetime


class PostView(View):
    source_id: str
    author_username: str | None
    text: str
    created_at: datetime
    likes: int | None
    reposts: int | None
    replies: int | None
    quotes: int | None
    impressions: int | None
    query: str


class FindingView(View):
    id: str
    theme: str
    summary: str
    claim_type: str
    signal_strength: float
    evidence_source_ids: list[str]
    claim_kind: str | None = None
    """Phase 9 discovery runs: the claim kind after validation against the evidence."""
    verification: str | None = None


class ResearchView(View):
    query: str
    effective_query: str
    broadened_queries: list[str]
    provider: str
    synthetic: bool
    topic: str
    summary: str
    confidence: str
    limitations: list[str]
    opportunities: list[dict[str, Any]]
    findings: list[FindingView]


class CritiqueView(View):
    id: str
    generation_attempt: int
    verdict: str
    recommended_verdict: str
    score: float
    factual_risk: str
    originality_risk: str
    tone_match: str
    issues: list[dict[str, Any]]
    policy_violations: list[dict[str, Any]]
    suggested_revision: str | None


class CandidateView(View):
    id: str
    generation_attempt: int
    origin: str
    content: str
    topic: str
    hook_type: str
    format: str
    revises_candidate_id: str | None
    research_finding_ids: list[str]
    critiques: list[CritiqueView]
    """Oldest first; the last one is the critique of record."""
    content_intent: str | None = None
    """Phase 9: the run's content intent (None for runs without one)."""
    manual_only: bool = False
    """Phase 9: the text mentions an account, so only a person may post it (export it)."""


class DecisionView(View):
    id: str
    action: str
    candidate_id: str | None
    resulting_candidate_id: str | None
    edited_content: str | None
    reviewed_candidate_ids: list[str]
    note: str | None
    reviewer: str
    decided_at: datetime


class ReviewView(View):
    awaiting_review: bool
    candidate_ids: list[str]
    edits_remaining: int
    regenerations_remaining: int
    decisions: list[DecisionView]


class PublicationAttemptView(View):
    """One publish attempt. ``finished_at`` is None while it is (or was) in flight."""

    attempt: int
    provider: str
    outcome: str
    started_at: datetime
    finished_at: datetime | None
    latency_ms: float | None
    http_status: int | None
    failure_category: str | None
    failure_message: str | None
    """Sanitized: status, category and the platform's short error title only."""
    rate_limit_reset_at: datetime | None
    provider_post_id: str | None


class PublicationView(View):
    id: str
    run_id: str
    candidate_id: str
    platform: str
    status: PublicationStatus
    content: str
    scheduled_for: datetime | None
    requested_by: str | None
    attempt_count: int
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    provider: str | None
    provider_post_id: str | None
    provider_post_url: str | None
    started_at: datetime | None
    published_at: datetime | None
    failure_category: str | None
    failure_message: str | None
    rate_limit_reset_at: datetime | None
    retry_not_before: datetime | None
    """Set while a rate-limited publication waits in ``ready`` for its reset."""
    provider_created_at: datetime | None = None
    """The platform's creation time for the post, once a metrics read reported it.
    ``published_at`` is when this application recorded the publication."""
    resolved_by: str | None
    resolution_note: str | None
    created_at: datetime
    updated_at: datetime
    attempts: list[PublicationAttemptView] = []
    analytics: "AnalyticsSummary | None" = None
    """Snapshot jobs and the latest observation; None before any job exists."""


class AnalyticsAttemptView(View):
    """One job's part in one provider request."""

    attempt: int
    request_id: str
    outcome: str
    failure_category: str | None
    started_at: datetime
    finished_at: datetime | None


class AnalyticsJobView(View):
    id: str
    publication_id: str
    snapshot_age: str
    age_seconds: int
    schedule_basis: str
    """``provider_created_at`` (the platform's post time) or ``recorded_published_at``
    (when this application recorded the publication)."""
    basis_at: datetime
    scheduled_for: datetime
    original_scheduled_for: datetime | None
    """Set when reconciliation moved the job to the verified creation time."""
    origin: str
    status: AnalyticsJobStatus
    attempt_count: int
    retry_not_before: datetime | None
    failure_category: str | None
    failure_message: str | None
    rate_limit_reset_at: datetime | None
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    collected_at: datetime | None
    created_at: datetime
    updated_at: datetime
    attempts: list[AnalyticsAttemptView] = []


class MetricSnapshotView(View):
    """One observation. Counts are exactly what the platform reported (None = not
    reported). Timing fields say when it was really taken: a snapshot is labelled with
    its *target* age, and ``on_target`` says whether its actual age was close to it."""

    id: str
    publication_id: str
    platform: str
    provider: str
    metrics_scope: str
    provider_post_id: str
    snapshot_age: str
    target_age_seconds: int
    scheduled_for: datetime
    captured_at: datetime
    capture_delay_seconds: float
    """captured_at - scheduled_for."""
    provider_created_at: datetime | None
    recorded_published_at: datetime | None
    age_basis: str
    """What ``actual_age_seconds`` is measured from: ``provider_created_at`` when the
    platform reported it, otherwise ``recorded_published_at`` (less exact)."""
    actual_age_seconds: float | None
    on_target: bool | None
    """Whether the actual age is within tolerance of the target age; None if unknown."""
    likes: int | None
    reposts: int | None
    replies: int | None
    quotes: int | None
    bookmarks: int | None
    impressions: int | None
    derived: ObservedRates


class AnalyticsSummary(View):
    jobs_by_status: dict[str, int]
    snapshot_count: int
    provider_created_at: datetime | None
    latest: MetricSnapshotView | None
    """The snapshot with the largest target age collected so far."""


class PublishedPostView(View):
    """A published post with its latest observation (``GET /posts``)."""

    publication_id: str
    run_id: str
    candidate_id: str
    account_id: str | None
    platform: str
    provider_post_id: str | None
    provider_post_url: str | None
    recorded_published_at: datetime | None
    provider_created_at: datetime | None
    content: str
    hook_type: str | None
    strategy_id: str | None
    strategy_version: int | None
    analytics: AnalyticsSummary


class DiscoveryLineage(View):
    opportunity_id: str | None
    session_id: str | None
    evidence: list[OpportunityEvidence]
    manual_export_id: str | None
    manual_publication_id: str | None
    verified_author_id: str | None


class LineageView(View):
    """From one publication back to everything that produced it."""

    publication: PublicationView
    candidate: CandidateView
    revision_chain: list[str]
    """The candidate and the candidates it revised, newest first."""
    approval: DecisionView | None
    findings: list[FindingView]
    source_posts: list[PostView]
    research_query: str | None
    research_provider: str | None
    strategy_id: str
    strategy_version: int
    strategy: dict[str, Any]
    account: dict[str, Any]
    metrics: list[MetricSnapshotView]
    discovery: DiscoveryLineage | None = None
    """Phase 9: the opportunity and evidence the run started from, and the manual
    export/registration when the post was published by hand."""


class RunDetail(View):
    run: RunSummary
    research_fetches: list[FetchView]
    source_posts: list[PostView]
    research: ResearchView | None
    candidates: list[CandidateView]
    review: ReviewView
    usage: UsageCounts
    cost: CostEstimate
    """Estimated with the price list stored for this run."""
    publications: list[PublicationView]
    """Publication intents for this run; empty until someone requests publishing. Each
    carries its analytics summary once snapshot jobs exist."""


class PendingReview(View):
    run_id: str
    created_at: datetime
    edits_remaining: int
    regenerations_remaining: int
    candidates: list[CandidateView]


class UsageReport(View):
    run_id: str
    usage: UsageCounts
    at_run_pricing: CostEstimate
    """Estimated with the price list stored when the run was created (reproducible)."""
    at_current_pricing: CostEstimate
    """Re-estimated with the price list configured now."""
    note: str = (
        "Costs are estimates from canonical usage counts and a configured price list; "
        "they are not invoices."
    )


# --- strategy evaluation (Phase 7) ----------------------------------------------------------


class EvaluationUsageView(View):
    """What the proposals made from one evaluation spent. The evaluation itself never
    calls a model or a platform. Every proposal counts, including failed and abandoned
    ones: they spent tokens too. Each proposal snapshots its own price list, so the
    proposal-pricing figure is a sum of per-proposal totals, not one estimate."""

    proposals: int
    usage: UsageCounts
    at_current_pricing: CostEstimate
    at_proposal_pricing_total: Decimal | None
    """Sum of each proposal's estimate at its own pricing; None unless all are known."""
    proposal_pricing_missing: list[str]
    note: str = "Estimates from the reported token counts; not invoices."


class EvaluationView(View):
    id: str
    account_id: str
    strategy_id: str | None
    snapshot_age: str
    published_since: datetime | None
    published_until: datetime | None
    data_cutoff: datetime
    synthetic: bool
    config: dict[str, Any]
    config_version: str
    input_digest: str
    digest: str
    status: EvaluationStatus
    eligible_count: int
    excluded_count: int
    posts_considered: int
    snapshots_used: int
    platform_reads: int = 0
    """Evaluations read only stored snapshots: never a platform call."""
    llm_calls: int = 0
    """Evaluations never call a model; proposals are separate, explicit requests."""
    summary: EvaluationSummary
    created_by: str | None
    created_at: datetime
    proposal_ids: list[str]
    usage: EvaluationUsageView


class EvaluationCreated(View):
    created: bool
    """False when an identical request already produced this evaluation."""
    evaluation: EvaluationView


class StrategyVersionView(View):
    id: str
    strategy_id: str
    account_id: str
    version: int
    content: ContentStrategy
    content_sha256: str
    parent_version_id: str | None
    origin: StrategyVersionOrigin
    created_by: str | None
    auth_method: str | None
    note: str | None
    created_at: datetime
    active: bool
    source_experiment_result_id: str | None = None
    """Phase 8: the concluded experiment result this version was adopted from."""


class ActivationView(View):
    id: str
    account_id: str
    seq: int
    strategy_version_id: str
    previous_version_id: str | None
    activated_by: str
    auth_method: str
    note: str | None
    activated_at: datetime


class StrategyHistoryView(View):
    strategy_id: str
    versions: list[StrategyVersionView]
    activations: list[ActivationView]


class ActiveStrategyView(View):
    account_id: str
    version: StrategyVersionView
    activation: ActivationView


class ProposalDecisionView(View):
    id: str
    decision: DecisionKind
    reviewer: str
    auth_method: str
    summary: str
    reviewed_proposal_sha256: str | None
    reviewed_evaluation_digest: str | None
    resulting_version_id: str | None
    decided_at: datetime


class ProposalUsageView(View):
    usage: UsageCounts
    at_proposal_pricing: CostEstimate
    at_current_pricing: CostEstimate
    note: str = "Estimates from the reported token counts; not invoices."


class ProposalView(View):
    id: str
    evaluation_id: str
    evaluation_digest: str
    account_id: str
    base_version_id: str
    status: ProposalStatus
    stale: bool
    """A ``generating`` proposal older than twice the model timeout: its process most
    likely died. Shown here only; an explicit retry closes it."""
    outcome: ProposalOutcome | None
    proposer: str
    provider: str
    model: str
    prompt_version: str
    proposal: ValidatedProposal | None
    proposal_sha256: str | None
    validation_errors: list[str] | None
    error_type: str | None
    error_message: str | None
    retry_of: str | None
    requested_by: str | None
    started_at: datetime
    finished_at: datetime | None
    latency_ms: float | None
    decision: ProposalDecisionView | None
    usage: ProposalUsageView
    note: str = (
        "Proposed changes are hypotheses from observational data. Approving creates a new "
        "strategy version; it does not activate it, publish anything or start a run."
    )


# --- experiments (Phase 8) ----------------------------------------------------------------


class ExperimentArmView(View):
    id: str
    label: str
    position: int
    is_control: bool
    treatment: Treatment
    description: str


class ExperimentEventView(View):
    id: str
    kind: ExperimentEventKind
    actor: str
    auth_method: str
    reason: str | None
    detail: dict[str, Any] | None
    created_at: datetime


class ExperimentResultView(View):
    """The immutable conclusion. ``adoptable`` is false unless the verdict is a
    confirmatory ``favours_variant`` on real data; ``adoption_blockers`` says why not."""

    id: str
    verdict: Verdict
    evidence_class: EvidenceClass
    peeked: bool
    cutoff: datetime
    analysis_version: str
    input_digest: str
    digest: str
    flags: list[ResultFlag]
    concluded_by: str
    auth_method: str
    created_at: datetime
    adoptable: bool
    adoption_blockers: list[str]
    adopted_version_id: str | None


class ExperimentView(View):
    id: str
    account_id: str
    strategy_id: str
    base_version_id: str
    factor: ExperimentFactor
    synthetic: bool
    design: ExperimentDesign
    design_sha256: str
    snapshot_age_seconds: int
    status: ExperimentStatus
    peeked: bool
    stop_reason: StopReason | None
    created_by: str
    auth_method: str
    created_at: datetime
    approved_at: datetime | None
    started_at: datetime | None
    stopped_at: datetime | None
    unblinded_at: datetime | None
    concluded_at: datetime | None
    cancelled_at: datetime | None
    planned_end_at: datetime | None
    """``started_at`` plus the pre-registered maximum duration."""
    arms: list[ExperimentArmView]
    assigned_per_arm: dict[str, int]
    """Assignment counts only (not outcomes): needed to run the experiment."""
    runs_cap_per_arm: int
    feasibility: Feasibility
    result: ExperimentResultView | None
    events: list[ExperimentEventView]
    note: str = (
        "Experiments never publish, adopt or activate anything on their own. Outcome "
        "summaries are masked while the experiment is open; see the analysis for exactly "
        "which fields, and for what remains visible through other endpoints."
    )


class AssignmentView(View):
    id: str
    arm: str
    run_id: str
    block_no: int
    position: int
    assigned_by: str
    auth_method: str
    assigned_at: datetime


class ExperimentRunCreated(View):
    assignment: AssignmentView
    run: RunSummary


class LineageEntryView(View):
    """experiment -> arm -> run -> candidates -> publications -> snapshots (ids only)."""

    assignment_id: str
    arm: str
    run_id: str
    candidate_ids: list[str]
    publication_ids: list[str]
    post_metric_ids: list[str]


class ExperimentLineageView(View):
    experiment_id: str
    base_version_id: str
    entries: list[LineageEntryView]
    adopted_version_id: str | None


class ArmUsageView(View):
    arm: str
    runs: int
    usage: UsageCounts
    at_run_pricing_total: Decimal | None
    """Sum of each run's estimate at its own stored pricing; None unless all are known."""
    run_pricing_missing: list[str]


class ProjectedCostView(View):
    remaining_runs: int
    """Up to the assignment cap of every arm."""
    runs_with_usage: int
    mean_cost_per_run: Decimal | None
    projected_remaining: Decimal | None
    missing_prices: list[str]
    basis: str = (
        "Mean of this experiment's finished runs at current pricing, times the runs still "
        "allowed by the cap. None when no run has finished or any price is unknown."
    )


class ExperimentUsageView(View):
    experiment_id: str
    per_arm: list[ArmUsageView]
    usage: UsageCounts
    at_current_pricing: CostEstimate
    projected: ProjectedCostView
    note: str = (
        "Estimates from canonical usage counts and the configured price lists; not invoices. "
        "Unknown prices are listed under missing_prices and never assumed to be zero. "
        "Analytics request counts are an upper bound when summed across runs."
    )


PublicationView.model_rebuild()
