"""Inputs and outputs of the research step."""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from social_growth_agent.models.base import DomainModel, new_id


class ResearchQuery(DomainModel):
    account_id: str
    niche: str
    topics: list[str]
    limit: int = Field(default=20, ge=1, le=100)


class SourcePost(DomainModel):
    """A raw post returned by a research provider (someone else's content)."""

    id: str
    author_handle: str
    text: str
    topic: str
    posted_at: datetime
    impressions: int = Field(ge=0)
    likes: int = Field(ge=0)
    replies: int = Field(ge=0)
    reposts: int = Field(ge=0)


class ResearchFinding(DomainModel):
    """A structured observation the Research Agent derived from source posts."""

    id: str = Field(default_factory=lambda: new_id("find"))
    theme: str
    summary: str
    evidence_post_ids: list[str] = Field(min_length=1)
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
    post_ids: list[str]
    synthetic: bool


class ResearchBrief(DomainModel):
    id: str = Field(default_factory=lambda: new_id("brief"))
    topic: str
    summary: str
    opportunities: list[ContentOpportunity]
    confidence: Confidence
    limitations: list[str]
    source: ResearchSource
