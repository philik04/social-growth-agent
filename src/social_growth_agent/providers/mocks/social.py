"""Deterministic in-memory social platform mocks."""

import hashlib
from collections.abc import Sequence

from social_growth_agent.errors import ProviderError, TransientProviderError
from social_growth_agent.models import (
    PostMetrics,
    PublishedPost,
    PublishRequest,
    ResearchQuery,
    SourcePost,
)
from social_growth_agent.providers.mocks.fixtures import FIXTURE_POSTS


class MockResearchProvider:
    """Filters fixture posts by topic and ranks them by engagement.

    ``fail_first`` makes the first N calls raise ``TransientProviderError`` so
    retry behaviour can be tested deterministically.
    """

    def __init__(self, posts: Sequence[SourcePost] = FIXTURE_POSTS, *, fail_first: int = 0) -> None:
        self._posts = tuple(posts)
        self.source_name = "mock_fixtures"
        self.synthetic = True
        self._failures_left = fail_first
        self.calls: list[ResearchQuery] = []

    def search(self, query: ResearchQuery) -> list[SourcePost]:
        self.calls.append(query)
        if self._failures_left > 0:
            self._failures_left -= 1
            raise TransientProviderError("mock research provider unavailable")
        topics = {t.lower() for t in query.topics}
        matches = [p for p in self._posts if p.topic.lower() in topics]
        matches.sort(key=lambda p: (-(p.likes + p.reposts * 2 + p.replies), p.id))
        return matches[: query.limit]


class MockPublisher:
    """Records published posts and returns sequential platform ids."""

    def __init__(self, max_post_length: int = 280) -> None:
        self._max_post_length = max_post_length
        self.published: list[PublishedPost] = []

    def publish(self, request: PublishRequest) -> PublishedPost:
        if len(request.content) > self._max_post_length:
            raise ProviderError(f"content exceeds {self._max_post_length} characters")
        post = PublishedPost(
            account_id=request.account_id,
            candidate_id=request.candidate_id,
            platform_post_id=f"mock-x-{len(self.published) + 1:04d}",
            content=request.content,
        )
        self.published.append(post)
        return post


class MockAnalyticsProvider:
    """Derives stable pseudo-metrics from a hash of the post id."""

    def fetch_metrics(self, platform_post_ids: list[str]) -> list[PostMetrics]:
        return [self._metrics_for(post_id) for post_id in platform_post_ids]

    @staticmethod
    def _metrics_for(post_id: str) -> PostMetrics:
        seed = int.from_bytes(hashlib.sha256(post_id.encode()).digest()[:8], "big")
        impressions = 1_000 + seed % 20_000
        return PostMetrics(
            platform_post_id=post_id,
            impressions=impressions,
            likes=impressions // 40,
            replies=impressions // 400,
            reposts=impressions // 250,
            bookmarks=impressions // 300,
            profile_visits=impressions // 120,
            link_clicks=None,
            follower_change=seed % 7,
        )
