"""Social platform ports. Business logic depends on these, never on the X API directly."""

from collections.abc import Sequence
from typing import Protocol

from social_growth_agent.models import (
    AnalyticsFetch,
    MetricsScope,
    PublishRequest,
    PublishResult,
    ResearchQuery,
    SearchResult,
)


class SocialResearchProvider(Protocol):
    """Retrieval only: fetch and normalize posts. Never calls an LLM, interprets
    trends, generates content or decides routing."""

    source_name: str
    """Recorded on every research brief so findings are traceable to their source."""
    synthetic: bool
    """True when the material is fixture or generated data rather than live platform data."""
    max_requests_per_run: int | None
    """Upper bound on platform requests per run (cost control); None means unbounded.
    The graph caps retrieval retries at this value."""

    def search(self, query: ResearchQuery) -> SearchResult:
        """Return normalized posts relevant to the query plus fetch metadata.

        Must not mutate ``query``. Raises ``ProviderError`` subclasses on failure; a
        real failure is never replaced with fake data.
        """
        ...


class SocialPublisher(Protocol):
    """Creates one post. External I/O only: no policy, no persistence, no retries.

    Implementations raise ``PublishError`` subclasses that say what is known about the
    outcome: ``PublishRejectedError`` (definitely not created), ``PublishNotSentError``
    (nothing reached the platform) or ``PublishOutcomeUnknownError`` (it may exist).
    Errors never contain credentials, auth headers or raw provider responses.
    """

    platform: str
    """The platform posts go to, e.g. ``"x"``."""
    provider_name: str
    """Recorded on every attempt, e.g. ``"x"`` or ``"mock_publisher"``."""

    def publish(self, request: PublishRequest) -> PublishResult: ...


class SocialAnalyticsProvider(Protocol):
    """Reads metrics of published posts by platform post id. External I/O only: no
    scheduling, no persistence, no retries, no interpretation.

    Returns one ``AnalyticsFetch`` per call: a ``PostMetricRecord`` for every post the
    platform returned and a ``PostMetricMiss`` for every id it answered without metrics
    (deleted, protected). Raises ``AnalyticsError`` when the whole request failed.
    Missing metric fields are ``None``, never 0. Errors never contain credentials, auth
    headers or raw provider responses.

    Phase 6 implementations read public metrics only (``metrics_scope == "public"``).
    A provider for private, user-context metrics would declare another scope; the
    snapshot rows record which scope produced them.
    """

    platform: str
    provider_name: str
    """Recorded on every request and snapshot, e.g. ``"x"`` or ``"mock_analytics"``."""
    metrics_scope: MetricsScope
    max_batch_size: int
    """Most post ids one request may carry."""

    def fetch_post_metrics(self, post_ids: Sequence[str]) -> AnalyticsFetch: ...
