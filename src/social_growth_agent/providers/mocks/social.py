"""Deterministic in-memory social platform mocks."""

import re
import threading
from collections.abc import Callable, Iterable, Sequence

from social_growth_agent.errors import (
    PublishRejectedError,
    TransientProviderError,
)
from social_growth_agent.models import (
    FetchOutcome,
    PublishFailureCategory,
    PublishRequest,
    PublishResult,
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


type PublishScriptStep = BaseException | Callable[[PublishRequest], None] | None
"""One scripted call: an exception to raise, a hook to run first (e.g. to simulate a
crash or inspect the database mid-call), or ``None`` for an ordinary success."""


class MockPublisher:
    """Deterministic in-memory publisher (the default). Never touches the network.

    Returns sequential numeric post ids, enforces the post length, and can be scripted
    per call to raise publish errors, so every publication state is reachable offline.
    """

    platform = "x"
    provider_name = "mock_publisher"

    def __init__(
        self, script: Iterable[PublishScriptStep] = (), *, max_post_length: int = 280
    ) -> None:
        self._max_post_length = max_post_length
        self._script = list(script)
        self._lock = threading.Lock()
        self.calls: list[PublishRequest] = []
        self.published: list[PublishResult] = []

    def publish(self, request: PublishRequest) -> PublishResult:
        with self._lock:
            self.calls.append(request)
            step = self._script.pop(0) if self._script else None
        if isinstance(step, BaseException):
            raise step
        if step is not None:
            step(request)
        if len(request.content) > self._max_post_length:
            raise PublishRejectedError(
                f"content exceeds {self._max_post_length} characters (HTTP 400)",
                failure_category=PublishFailureCategory.BAD_REQUEST,
                http_status=400,
            )
        with self._lock:
            post_id = str(1_900_000_000_000_000_000 + len(self.published) + 1)
            result = PublishResult(
                provider_post_id=post_id,
                provider_post_url=f"https://x.invalid/mock/status/{post_id}",
                http_status=201,
            )
            self.published.append(result)
        return result
