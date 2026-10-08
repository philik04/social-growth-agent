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

Errors raised around an LLM call carry ``llm_call`` metadata, and errors raised during
research carry ``research_fetch`` metadata, so the failure can be recorded in the run
trace. Messages never contain credentials or request headers.
"""

from datetime import datetime

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
