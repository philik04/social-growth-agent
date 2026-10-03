"""Publishing and performance data."""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from social_growth_agent.models.base import DomainModel, new_id, utc_now


class PublishRequest(DomainModel):
    account_id: str
    candidate_id: str
    content: str = Field(min_length=1)


class PublishedPost(DomainModel):
    id: str = Field(default_factory=lambda: new_id("pub"))
    account_id: str
    candidate_id: str
    platform_post_id: str
    content: str
    published_at: datetime = Field(default_factory=utc_now)


class PublishStatus(StrEnum):
    NOT_STARTED = "not_started"
    PUBLISHED = "published"
    FAILED = "failed"


class PublishState(DomainModel):
    """Placeholder in Phase 1; the publish node arrives in Phase 5."""

    status: PublishStatus = PublishStatus.NOT_STARTED
    post: PublishedPost | None = None


class PostMetrics(DomainModel):
    """A metrics snapshot. Optional fields are not always exposed by the X API tier."""

    platform_post_id: str
    collected_at: datetime = Field(default_factory=utc_now)
    impressions: int = Field(ge=0)
    likes: int = Field(ge=0)
    replies: int = Field(ge=0)
    reposts: int = Field(ge=0)
    bookmarks: int | None = Field(default=None, ge=0)
    profile_visits: int | None = Field(default=None, ge=0)
    link_clicks: int | None = Field(default=None, ge=0)
    follower_change: int | None = None
