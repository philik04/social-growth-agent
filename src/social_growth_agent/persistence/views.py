"""Read models: what the API returns about runs. Built from the domain tables only."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from social_growth_agent.accounting import CostEstimate, UsageCounts
from social_growth_agent.models import RunStatus


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
    note: str | None
    reviewer: str
    decided_at: datetime


class ReviewView(View):
    awaiting_review: bool
    candidate_ids: list[str]
    edits_remaining: int
    regenerations_remaining: int
    decisions: list[DecisionView]


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
