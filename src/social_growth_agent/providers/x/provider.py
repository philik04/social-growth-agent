"""``XResearchProvider``: retrieval of public posts from X recent search.

Retrieval only. It calls the X API once per ``search`` (no pagination, no retries,
no sleeping), normalizes the result, records what was consumed and translates
failures into domain errors. It never calls an LLM, interprets trends, generates
content or makes routing decisions.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from pydantic import SecretStr

from social_growth_agent.errors import (
    ConfigurationError,
    ProviderError,
    RateLimitedError,
    SocialGrowthError,
)
from social_growth_agent.models import (
    FetchOutcome,
    ProviderErrorCategory,
    ResearchFetch,
    ResearchQuery,
    SearchResult,
    utc_now,
)
from social_growth_agent.observability import get_logger
from social_growth_agent.providers.x.client import RateLimit, XApiClient
from social_growth_agent.providers.x.normalize import normalize_search_response
from social_growth_agent.providers.x.query import (
    X_MAX_QUERY_LENGTH,
    XSearchRequest,
    build_search_request,
)

SEARCH_RECENT_PATH = "/2/tweets/search/recent"
DEFAULT_MAX_RESULTS_PER_QUERY = 10
DEFAULT_MAX_QUERIES_PER_RUN = 1

_log = get_logger("providers.x")


class XResearchProvider:
    source_name = "x"
    synthetic = False

    def __init__(
        self,
        client: XApiClient,
        *,
        max_results_per_query: int = DEFAULT_MAX_RESULTS_PER_QUERY,
        max_queries_per_run: int = DEFAULT_MAX_QUERIES_PER_RUN,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not 10 <= max_results_per_query <= 100:
            raise ConfigurationError("X_MAX_RESULTS_PER_QUERY must be between 10 and 100")
        if max_queries_per_run < 1:
            raise ConfigurationError("X_MAX_QUERIES_PER_RUN must be at least 1")
        self._client = client
        self._max_results = max_results_per_query
        self.max_requests_per_run: int | None = max_queries_per_run
        self._clock = clock

    @classmethod
    def from_token(
        cls,
        bearer_token: SecretStr | None,
        *,
        timeout_seconds: float,
        max_results_per_query: int = DEFAULT_MAX_RESULTS_PER_QUERY,
        max_queries_per_run: int = DEFAULT_MAX_QUERIES_PER_RUN,
        transport: httpx.BaseTransport | None = None,
    ) -> "XResearchProvider":
        if bearer_token is None:
            raise ConfigurationError("X_BEARER_TOKEN is not set (required for RESEARCH_PROVIDER=x)")
        client = XApiClient(bearer_token, timeout_seconds=timeout_seconds, transport=transport)
        return cls(
            client,
            max_results_per_query=max_results_per_query,
            max_queries_per_run=max_queries_per_run,
        )

    def __repr__(self) -> str:
        return (
            f"XResearchProvider(max_results_per_query={self._max_results}, "
            f"max_queries_per_run={self.max_requests_per_run})"
        )

    def search(self, query: ResearchQuery) -> SearchResult:
        started_at = self._clock()
        request = build_search_request(query, max_results_cap=self._max_results, now=started_at)
        start = time.perf_counter()
        requests_made = 0
        try:
            if len(request.effective_query) > X_MAX_QUERY_LENGTH:
                raise ProviderError(
                    f"effective X query exceeds {X_MAX_QUERY_LENGTH} characters",
                    category=ProviderErrorCategory.BAD_REQUEST,
                )
            requests_made = 1
            response = self._client.get(SEARCH_RECENT_PATH, request.params)
            page = normalize_search_response(
                response.body, query=query.text, retrieved_at=started_at
            )
        except SocialGrowthError as exc:
            exc.research_fetch = self._fetch(
                request,
                started_at,
                start,
                requests_made=requests_made,
                outcome=FetchOutcome.ERROR,
                error=exc,
            )
            self._log(exc.research_fetch)
            raise
        fetch = self._fetch(
            request,
            started_at,
            start,
            requests_made=requests_made,
            outcome=FetchOutcome.SUCCESS if page.posts else FetchOutcome.EMPTY,
            posts=len(page.posts),
            users=page.users_returned,
            rate_limit=response.rate_limit,
        )
        self._log(fetch)
        return SearchResult(posts=page.posts, fetch=fetch)

    def _fetch(
        self,
        request: XSearchRequest,
        started_at: datetime,
        start: float,
        *,
        requests_made: int,
        outcome: FetchOutcome,
        posts: int = 0,
        users: int = 0,
        rate_limit: RateLimit | None = None,
        error: SocialGrowthError | None = None,
    ) -> ResearchFetch:
        category = error.category if isinstance(error, ProviderError) else None
        reset_at = rate_limit.reset_at if rate_limit else None
        remaining = rate_limit.remaining if rate_limit else None
        if isinstance(error, RateLimitedError):
            reset_at, remaining = error.reset_at, 0
        return ResearchFetch(
            provider=self.source_name,
            query=request.original_query,
            effective_query=request.effective_query,
            max_results=request.max_results,
            requests_made=requests_made,
            posts_fetched=posts,
            users_fetched=users,
            latency_ms=(time.perf_counter() - start) * 1000,
            outcome=outcome,
            error_category=category,
            rate_limit_remaining=remaining,
            rate_limit_reset_at=reset_at,
            started_at=started_at.astimezone(UTC),
        )

    @staticmethod
    def _log(fetch: ResearchFetch) -> None:
        level = _log.info if fetch.outcome is not FetchOutcome.ERROR else _log.warning
        level(
            "research fetch",
            extra={
                "provider": fetch.provider,
                "query": fetch.query,
                "effective_query": fetch.effective_query,
                "requests": fetch.requests_made,
                "posts_fetched": fetch.posts_fetched,
                "latency_ms": round(fetch.latency_ms, 1),
                "outcome": fetch.outcome.value,
                "error_category": fetch.error_category,
                "rate_limit_reset_at": fetch.rate_limit_reset_at,
            },
        )
