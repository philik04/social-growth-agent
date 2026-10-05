"""Inputs and outputs of the research step.

Retrieval (a ``SocialResearchProvider``) produces ``SourcePost``s and a ``ResearchFetch``
record. Interpretation (the Research Agent) turns posts into ``ResearchFinding``s whose
``evidence_source_ids`` must all be ids of posts that were actually supplied.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field

from social_growth_agent.models.base import DomainModel, new_id, utc_now
from social_growth_agent.models.run import ProviderErrorCategory


class ResearchQuery(DomainModel):
    """A structured research request. Deliberately not a query language.

    ``text`` is the user's or configuration's query, kept verbatim for provenance.
    Providers derive their own effective query from it and never change this value.
    """

    text: str = Field(min_length=1, max_length=1024)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")
    max_results: int | None = Field(
        default=None, ge=10, le=100, description="None means the provider's configured cap."
    )
    author_username: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]{1,15}$")
    recency_hours: int | None = Field(
        default=None, ge=1, le=167, description="X recent search covers the last 7 days."
    )
    exclude_reposts: bool = True


class SourcePost(DomainModel):
    """A post retrieved from a platform (someone else's content), normalized.

    Metrics are ``None`` when the platform did not return them; they are never
    defaulted to zero, so "unknown" and "nobody engaged" stay distinguishable.
    """

    source_id: str = Field(min_length=1, description="Stable id used as evidence, e.g. x_123.")
    platform: Literal["x"] = "x"
    author_id: str
    author_username: str | None = None
    text: str
    created_at: datetime
    lang: str | None = None
    likes: int | None = Field(default=None, ge=0)
    reposts: int | None = Field(default=None, ge=0)
    replies: int | None = Field(default=None, ge=0)
    quotes: int | None = Field(default=None, ge=0)
    impressions: int | None = Field(default=None, ge=0)
    query: str = Field(description="The original query that retrieved this post.")
    retrieved_at: datetime

    def missing_metrics(self) -> list[str]:
        names = ("likes", "reposts", "replies", "quotes", "impressions")
        return [name for name in names if getattr(self, name) is None]


class FetchOutcome(StrEnum):
    SUCCESS = "success"
    EMPTY = "empty"
    ERROR = "error"


class ResearchFetch(DomainModel):
    """Operational record of one research retrieval: what was asked, what it consumed.

    Resource counts only. Pricing is deliberately absent; costs are derived elsewhere
    from these counts and a current price list.
    """

    provider: str
    query: str = Field(description="Original query, unmodified.")
    effective_query: str = Field(description="The query actually sent to the platform.")
    max_results: int = Field(ge=0)
    requests_made: int = Field(ge=0, description="HTTP requests sent to the platform.")
    posts_fetched: int = Field(ge=0, description="Post reads (the main billable resource).")
    users_fetched: int = Field(ge=0, description="User objects returned via expansions.")
    latency_ms: float = Field(ge=0.0)
    outcome: FetchOutcome
    error_category: ProviderErrorCategory | None = None
    rate_limit_remaining: int | None = None
    rate_limit_reset_at: datetime | None = None
    started_at: datetime = Field(default_factory=utc_now)


class SearchResult(DomainModel):
    """What a research provider returns: normalized posts plus fetch metadata."""

    posts: list[SourcePost]
    fetch: ResearchFetch


class ClaimType(StrEnum):
    """How strongly a finding is asserted. There is deliberately no 'causal' value."""

    OBSERVATION = "observation"
    HYPOTHESIS = "hypothesis"


class ResearchFinding(DomainModel):
    """A structured observation the Research Agent derived from source posts."""

    id: str = Field(default_factory=lambda: new_id("find"))
    theme: str
    summary: str
    claim_type: ClaimType = ClaimType.OBSERVATION
    evidence_source_ids: list[str] = Field(min_length=1)
    signal_strength: float = Field(ge=0.0, le=1.0)


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ContentOpportunity(DomainModel):
    angle: str
    rationale: str
    finding_ids: list[str] = Field(min_length=1)


class ResearchSource(DomainModel):
    """Where the research material came from. Set by the application, never the model."""

    provider: str
    source_ids: list[str]
    synthetic: bool
    query: str
    effective_query: str
    retrieved_at: datetime


class ResearchBrief(DomainModel):
    id: str = Field(default_factory=lambda: new_id("brief"))
    topic: str
    summary: str
    opportunities: list[ContentOpportunity]
    confidence: Confidence
    limitations: list[str]
    source: ResearchSource
