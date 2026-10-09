"""Exception hierarchy shared across layers.

The split matters for control flow:
- ``TransientProviderError`` is retried by the graph's node retry policy (for research
  retrieval, only within the provider's per-run request budget). If retries are
  exhausted, the node's error handler records it and routes the run to ``failed``.
- ``RateLimitedError`` is deliberately *not* transient: nothing sleeps until the reset
  or schedules a retry. The reset time is recorded and the run fails.
- Any other ``ProviderError`` is not retried; the error handler fails the run.
- ``RunAbortError`` subclasses (bad model output, no research signal) are not retried;
  the node wrapper records them and the run ends as failed.
- ``InvalidReviewError`` is raised back to the caller and leaves the run paused.
- ``ConfigurationError`` is raised at startup, before any run exists.
- ``InvalidRunStateError`` (HTTP 409) and ``DatabaseUnavailableError`` (HTTP 503) come
  from the run service (Phase 4).
- ``PublishError`` subclasses (Phase 5) are raised by publishers and classify what is
  known about an external post-create call: definitely rejected, definitely not sent,
  or outcome unknown. The publisher worker turns them into publication states; none of
  them is retried by the graph (publishing does not run in the graph).
- ``AnalyticsError`` (Phase 6) is raised by analytics providers for a whole failed
  metrics request. Reads create nothing, so the analytics worker may retry them,
  bounded and with a persisted backoff; nothing sleeps.

Errors raised around an LLM call carry ``llm_call`` metadata, and errors raised during
research carry ``research_fetch`` metadata, so the failure can be recorded in the run
trace. Messages never contain credentials or request headers.
"""

from datetime import datetime

from social_growth_agent.models.analytics import AnalyticsFailureCategory
from social_growth_agent.models.publishing import PublishFailureCategory
from social_growth_agent.models.research import ResearchFetch
from social_growth_agent.models.run import LLMCall, ProviderErrorCategory


class SocialGrowthError(Exception):
    """Base class for all project errors."""

    llm_call: LLMCall | None = None
    research_fetch: ResearchFetch | None = None


class ConfigurationError(SocialGrowthError):
    """Required configuration (e.g. an API key) is missing or invalid."""


class ProviderError(SocialGrowthError):
    """An external provider (LLM, social platform) failed and retrying will not help."""

    def __init__(self, message: str, *, category: ProviderErrorCategory | None = None) -> None:
        super().__init__(message)
        self.category = category


class TransientProviderError(ProviderError):
    """A provider failure that is worth retrying (rate limits, connection errors, 5xx)."""


class ProviderTimeoutError(TransientProviderError):
    """The provider did not answer within the configured timeout."""


class RateLimitedError(ProviderError):
    """The provider rejected the request for rate or usage limits (HTTP 429).

    Not a ``TransientProviderError``: Phase 3 never waits for the reset or retries
    on its own. ``reset_at`` is the provider-reported reset time, when known.
    """

    def __init__(self, message: str, *, reset_at: datetime | None = None) -> None:
        super().__init__(message, category=ProviderErrorCategory.RATE_LIMITED)
        self.reset_at = reset_at


class RunAbortError(SocialGrowthError):
    """A non-retryable failure; the node records it and the run ends as failed."""


class AgentOutputError(RunAbortError):
    """An agent produced output that violates its contract."""


class InvalidModelOutputError(AgentOutputError):
    """The model's output could not be parsed or validated (or was truncated/filtered)."""


class EmptyModelOutputError(InvalidModelOutputError):
    """The model returned no structured result."""


class ModelRefusalError(AgentOutputError):
    """The model refused to produce the requested output."""


class InsufficientSignalError(RunAbortError):
    """Research found nothing to build content on."""


class InvalidReviewError(SocialGrowthError):
    """A human review decision does not match the pending review request."""


class RunNotFoundError(SocialGrowthError):
    """No graph run exists for the given id."""


class InvalidRunStateError(SocialGrowthError):
    """The run exists but is not in a state that allows the requested operation."""


class DatabaseUnavailableError(SocialGrowthError):
    """The database could not be reached. Never carries the connection URL."""


class ResourceNotFoundError(SocialGrowthError):
    """A publication or candidate does not exist (HTTP 404)."""


class PublicationNotFoundError(ResourceNotFoundError):
    pass


class CandidateNotFoundError(ResourceNotFoundError):
    pass


class NotPublishableError(InvalidRunStateError):
    """The deterministic publish policy refused the request (HTTP 409)."""


class PublicationExistsError(InvalidRunStateError):
    """A publication intent for this run and candidate already exists (HTTP 409)."""

    def __init__(self, message: str, *, publication_id: str, status: str) -> None:
        super().__init__(message)
        self.publication_id = publication_id
        self.status = status


class InvalidScheduleError(SocialGrowthError):
    """``scheduled_for`` is outside the allowed window (HTTP 422)."""


# --- publishing (Phase 5) ---------------------------------------------------------------


class PublishError(ProviderError):
    """Base for publisher failures. ``failure_category`` drives the publication state;
    messages are sanitized (status, category, the platform's short error title)."""

    def __init__(
        self,
        message: str,
        *,
        failure_category: PublishFailureCategory,
        http_status: int | None = None,
        latency_ms: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.failure_category = failure_category
        self.http_status = http_status
        self.latency_ms = latency_ms


class PublishRejectedError(PublishError):
    """The platform definitely did not create the post (4xx). Not retried automatically."""

    def __init__(
        self,
        message: str,
        *,
        failure_category: PublishFailureCategory,
        http_status: int | None = None,
        latency_ms: float = 0.0,
        rate_limit_reset_at: datetime | None = None,
    ) -> None:
        super().__init__(
            message,
            failure_category=failure_category,
            http_status=http_status,
            latency_ms=latency_ms,
        )
        self.rate_limit_reset_at = rate_limit_reset_at


class PublishNotSentError(PublishError):
    """The connection failed before the request was sent: safe to retry (bounded)."""


class PublishOutcomeUnknownError(PublishError):
    """The request may have reached the platform; whether a post exists is unknown.
    Never retried automatically."""


# --- analytics (Phase 6) ----------------------------------------------------------------


class AnalyticsError(ProviderError):
    """A whole analytics request failed. Reads have no external side effect, so the
    category alone decides whether the job is retried; messages are sanitized."""

    def __init__(
        self,
        message: str,
        *,
        failure_category: AnalyticsFailureCategory,
        http_status: int | None = None,
        latency_ms: float = 0.0,
        rate_limit_reset_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_category = failure_category
        self.http_status = http_status
        self.latency_ms = latency_ms
        self.rate_limit_reset_at = rate_limit_reset_at


class AnalyticsJobNotFoundError(ResourceNotFoundError):
    pass


class NotSchedulableError(InvalidRunStateError):
    """Analytics cannot be scheduled for this publication (not published, no post id,
    or no publication timestamp)."""
