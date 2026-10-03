"""Social platform ports. Business logic depends on these, never on the X API directly."""

from typing import Protocol

from social_growth_agent.models import (
    PostMetrics,
    PublishedPost,
    PublishRequest,
    ResearchQuery,
    SourcePost,
)


class SocialResearchProvider(Protocol):
    source_name: str
    """Recorded on every research brief so findings are traceable to their source."""
    synthetic: bool
    """True when the material is fixture or generated data rather than live platform data."""

    def search(self, query: ResearchQuery) -> list[SourcePost]:
        """Return recent posts relevant to the query, most relevant first."""
        ...


class SocialPublisher(Protocol):
    def publish(self, request: PublishRequest) -> PublishedPost: ...


class SocialAnalyticsProvider(Protocol):
    def fetch_metrics(self, platform_post_ids: list[str]) -> list[PostMetrics]: ...
