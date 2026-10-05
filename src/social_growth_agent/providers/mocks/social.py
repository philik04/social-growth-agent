"""Deterministic in-memory social platform mocks."""

import hashlib
import re
from collections.abc import Sequence

from social_growth_agent.errors import ProviderError, TransientProviderError
from social_growth_agent.models import (
    FetchOutcome,
    PostMetrics,
    PublishedPost,
    PublishRequest,
    ResearchFetch,
    ResearchQuery,
    SearchResult,
    SourcePost,
)
from social_growth_agent.providers.mocks.fixtures import FIXTURE_POSTS

_DEFAULT_LIMIT = 20
_QUOTED = re.compile(r'"([^"]+)"')


def query_terms(text: str) -> list[str]:
    """Quoted phrases if the query has any, else the whole query (mock matching only)."""
    return _QUOTED.findall(text) or [text]


def mentions(text: str, term: str) -> bool:
    """Case- and space-insensitive containment, so "agent engineering" matches
    "#AgentEngineering"."""

    def squash(s: str) -> str:
        return re.sub(r"\s+", "", s).lower()

    return squash(term) in squash(text)


class MockResearchProvider:
    """Filters fixture posts by query phrase and ranks them by engagement.

    Synthetic and explicit: it is used only when configured (or in tests), never as
    a fallback for a failing real provider. It makes no platform requests.
    ``fail_first`` makes the first N calls raise ``TransientProviderError`` so
    retry behaviour can be tested deterministically.
    """

    def __init__(self, posts: Sequence[SourcePost] = FIXTURE_POSTS, *, fail_first: int = 0) -> None:
        self._posts = tuple(posts)
        self.source_name = "mock_fixtures"
        self.synthetic = True
        self.max_requests_per_run: int | None = None
        self._failures_left = fail_first
        self.calls: list[ResearchQuery] = []

    def search(self, query: ResearchQuery) -> SearchResult:
        self.calls.append(query)
        if self._failures_left > 0:
            self._failures_left -= 1
            raise TransientProviderError("mock research provider unavailable")
        terms = query_terms(query.text)
        matches = [p for p in self._posts if any(mentions(p.text, t) for t in terms)]
        matches.sort(key=lambda p: (-_engagement(p), p.source_id))
        limit = query.max_results or _DEFAULT_LIMIT
        posts = [p.model_copy(update={"query": query.text}) for p in matches[:limit]]
        fetch = ResearchFetch(
            provider=self.source_name,
            query=query.text,
            effective_query=query.text,
            max_results=limit,
            requests_made=0,
            posts_fetched=len(posts),
            users_fetched=0,
            latency_ms=0.0,
            outcome=FetchOutcome.SUCCESS if posts else FetchOutcome.EMPTY,
        )
        return SearchResult(posts=posts, fetch=fetch)


def _engagement(post: SourcePost) -> int:
    return (post.likes or 0) + 2 * (post.reposts or 0) + (post.replies or 0)


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
