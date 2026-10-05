"""Social platform ports. Business logic depends on these, never on the X API directly."""

from typing import Protocol

from social_growth_agent.models import (
    PostMetrics,
    PublishedPost,
    PublishRequest,
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
    def publish(self, request: PublishRequest) -> PublishedPost: ...


class SocialAnalyticsProvider(Protocol):
    def fetch_metrics(self, platform_post_ids: list[str]) -> list[PostMetrics]: ...
