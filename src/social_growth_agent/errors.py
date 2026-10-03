"""Exception hierarchy shared across layers.

The split matters for control flow:
- ``TransientProviderError`` is retried by the graph's node retry policy. If retries
  are exhausted, the node's error handler records it and routes the run to ``failed``.
- Any other ``ProviderError`` is not retried; the error handler fails the run.
- ``RunAbortError`` subclasses (bad model output, no research signal) are not retried;
  the node wrapper records them and the run ends as failed.
- ``InvalidReviewError`` is raised back to the caller and leaves the run paused.
- ``ConfigurationError`` is raised at startup, before any run exists.

Errors raised around an LLM call carry ``llm_call`` metadata when available so the
failure can be recorded in the run trace.
"""

from social_growth_agent.models.run import LLMCall


class SocialGrowthError(Exception):
    """Base class for all project errors."""

    llm_call: LLMCall | None = None


class ConfigurationError(SocialGrowthError):
    """Required configuration (e.g. an API key) is missing or invalid."""


class ProviderError(SocialGrowthError):
    """An external provider (LLM, social platform) failed and retrying will not help."""


class TransientProviderError(ProviderError):
    """A provider failure that is worth retrying (rate limits, connection errors, 5xx)."""


class ProviderTimeoutError(TransientProviderError):
    """The provider did not answer within the configured timeout."""


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
