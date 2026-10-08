"""Publishing and performance data.

Publication state (Phase 5) lives in the ``publications`` table, not in graph state:
a run ends at content approval, and publishing is a separate, explicit request
executed by the publisher worker.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from social_growth_agent.models.base import DomainModel, new_id, utc_now


class PublicationStatus(StrEnum):
    """Lifecycle of one publication intent. Separate from the run status: a run stays
    ``approved`` while its publication moves through these states."""

    SCHEDULED = "scheduled"
    """Requested for a future time; the worker claims it once due."""
    READY = "ready"
    """Due now; waiting for a worker."""
    PUBLISHING = "publishing"
    """A worker committed an attempt and is (or was) calling the platform."""
    PUBLISHED = "published"
    FAILED = "failed"
    """Definitely not published (the platform rejected it, or nothing was sent)."""
    UNKNOWN = "unknown"
    """The request may have reached the platform but there is no definite answer.
    Never retried automatically; a human resolves it."""
    CANCELLED = "cancelled"


ACTIVE_PUBLICATION_STATUSES = frozenset(
    {PublicationStatus.SCHEDULED, PublicationStatus.READY, PublicationStatus.PUBLISHING}
)


class PublishFailureCategory(StrEnum):
    """Why a publish attempt did not end in ``published``. Drives retry rules."""

    AUTH = "auth"
    """401/403: credentials, app permissions or account restrictions."""
    BAD_REQUEST = "bad_request"
    """400 and other definite 4xx rejections of the request or content."""
    DUPLICATE_CONTENT = "duplicate_content"
    """X refused identical text (403 with a duplicate-content detail)."""
    RATE_LIMITED = "rate_limited"
    NOT_SENT = "not_sent"
    """The connection failed before any byte of the request was sent."""
    TIMEOUT = "timeout"
    """Sent, but no answer in time: outcome unknown."""
    SERVER_ERROR = "server_error"
    """5xx: the platform may or may not have created the post."""
    MALFORMED_RESPONSE = "malformed_response"
    """A success status without a usable post id."""
    TRANSPORT_ERROR = "transport_error"
    """The connection broke after the request was sent."""
    LEASE_EXPIRED = "lease_expired"
    """The worker died (or stalled) while the attempt was in flight."""
    POLICY = "policy"
    """The deterministic publish policy refused it when the worker re-checked."""
    INTERNAL = "internal"
    """An unexpected error inside the publisher; treated as an unknown outcome."""


class AttemptOutcome(StrEnum):
    STARTED = "started"
    """Committed before the call; stays this way if the process dies mid-call."""
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    NOT_SENT = "not_sent"
    UNKNOWN = "unknown"


class PublishRequest(DomainModel):
    account_id: str
    candidate_id: str
    content: str = Field(min_length=1)
    idempotency_key: str | None = Field(
        default=None,
        description="Stable key of the publication intent. Used for tracing and by "
        "platforms that support idempotent creates; X API v2 has no such header.",
    )


class PublishResult(DomainModel):
    """What a publisher returns for a created post. No raw provider payload."""

    provider_post_id: str = Field(min_length=1)
    provider_post_url: str | None = None
    http_status: int | None = None
    latency_ms: float = Field(default=0.0, ge=0.0)
    rate_limit_remaining: int | None = None
    rate_limit_reset_at: datetime | None = None


class PublishedPost(DomainModel):
    """Phase 1 placeholder, kept for ``PublishState`` (checkpoint compatibility)."""

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
    """Deprecated and never written. Kept because ``GraphState`` forbids unknown fields
    and every stored checkpoint contains it. Publication state lives in the database."""

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
