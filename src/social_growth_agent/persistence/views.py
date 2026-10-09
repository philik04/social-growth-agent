"""Read models: what the API returns about runs. Built from the domain tables only."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from social_growth_agent.accounting import CostEstimate, UsageCounts
from social_growth_agent.analytics import ObservedRates
from social_growth_agent.models import AnalyticsJobStatus, PublicationStatus, RunStatus


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
    research_attempts: int
    generation_attempts: int
    regeneration_rounds: int
    pricing_id: str | None
    created_at: datetime
    updated_at: datetime
    failure: RunFailure | None


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


PublicationView.model_rebuild()
