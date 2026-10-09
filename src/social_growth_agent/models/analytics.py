"""Analytics (Phase 6): metric snapshots of published posts and the jobs that collect them.

Metrics are observations, not experiments. Every count is nullable: ``None`` means the
platform did not return that metric, and is never replaced by 0. Only a 0 the platform
reported is stored as 0.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AnalyticsJobStatus(StrEnum):
    SCHEDULED = "scheduled"
    COLLECTING = "collecting"
    COLLECTED = "collected"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AnalyticsFailureCategory(StrEnum):
    """Why one collection attempt produced no snapshot. Drives the retry policy."""

    AUTH = "auth"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    TRANSIENT_SERVER = "transient_server"
    NOT_FOUND = "not_found"
    BAD_REQUEST = "bad_request"
    MALFORMED_RESPONSE = "malformed_response"
    UNKNOWN = "unknown"
    LEASE_EXPIRED = "lease_expired"


class ScheduleBasis(StrEnum):
    """Which timestamp a job's ``scheduled_for`` was computed from.

    ``provider_created_at`` is the post's creation time as reported by the platform.
    ``recorded_published_at`` is when *this application* recorded the publication as
    published; for a publication resolved by hand that is the resolution time, which can
    be much later than the real post.
    """

    PROVIDER_CREATED_AT = "provider_created_at"
    RECORDED_PUBLISHED_AT = "recorded_published_at"


class AttemptResult(StrEnum):
    STARTED = "started"
    COLLECTED = "collected"
    NOT_FOUND = "not_found"
    FAILED = "failed"


class RequestOutcome(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    """The request succeeded but some ids came back as errors (e.g. deleted posts)."""
    FAILED = "failed"


MetricsScope = Literal["public"]
"""Phase 6 reads public metrics only. Private (user-context) metrics are a later scope."""


class SnapshotAge(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str = Field(pattern=r"^\d+[smhd]$")
    seconds: int = Field(ge=60)


class PostMetricRecord(BaseModel):
    """One post's metrics exactly as the provider reported them (normalized names)."""

    model_config = ConfigDict(frozen=True)

    provider_post_id: str
    provider_created_at: datetime | None = None
    likes: int | None = Field(default=None, ge=0)
    reposts: int | None = Field(default=None, ge=0)
    replies: int | None = Field(default=None, ge=0)
    quotes: int | None = Field(default=None, ge=0)
    bookmarks: int | None = Field(default=None, ge=0)
    impressions: int | None = Field(default=None, ge=0)


class PostMetricMiss(BaseModel):
    """An id the provider answered for, but without metrics (deleted, protected...)."""

    model_config = ConfigDict(frozen=True)

    provider_post_id: str
    category: AnalyticsFailureCategory
    detail: str = Field(max_length=300)


class AnalyticsFetch(BaseModel):
    """The result of one provider request for a batch of post ids."""

    model_config = ConfigDict(frozen=True)

    records: list[PostMetricRecord] = Field(default_factory=list)
    misses: list[PostMetricMiss] = Field(default_factory=list)
    metrics_scope: MetricsScope = "public"
    http_status: int | None = None
    latency_ms: float | None = None
    posts_returned: int = Field(default=0, ge=0)
    """Post objects in the response: what the platform bills as post reads."""
    rate_limit_remaining: int | None = None
    rate_limit_reset_at: datetime | None = None
