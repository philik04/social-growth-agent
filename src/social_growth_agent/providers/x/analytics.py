"""X public metrics for published posts: ``GET /2/tweets?ids=...`` with the app-only
bearer token.

The only code that knows the endpoint, the request fields, X's JSON shape and its error
mapping for analytics. Phase 6 reads ``public_metrics`` only (likes, reposts, replies,
quotes, bookmarks, impressions as X reports them) plus ``created_at``. Private metrics
(``non_public_metrics``, ``organic_metrics``) need user-context auth and are not
requested.

Authentication reuses ``XApiClient`` (bearer token in its headers only, never logged,
never chained onto an error). The publishing credentials are never used here.
"""

import contextlib
import time
from collections.abc import Sequence
from datetime import datetime

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from social_growth_agent.errors import (
    AnalyticsError,
    ConfigurationError,
    ProviderError,
    RateLimitedError,
)
from social_growth_agent.models import (
    AnalyticsFailureCategory,
    AnalyticsFetch,
    MetricsScope,
    PostMetricMiss,
    PostMetricRecord,
    ProviderErrorCategory,
)
from social_growth_agent.providers.x.client import XApiClient

LOOKUP_PATH = "/2/tweets"
POST_FIELDS = "created_at,public_metrics"
MAX_IDS_PER_REQUEST = 100
_NOT_FOUND = "resource-not-found"
_NOT_AUTHORIZED = "not-authorized-for-resource"
_MAX_DETAIL = 200

_CATEGORY = {
    ProviderErrorCategory.AUTH: AnalyticsFailureCategory.AUTH,
    ProviderErrorCategory.RATE_LIMITED: AnalyticsFailureCategory.RATE_LIMITED,
    ProviderErrorCategory.TIMEOUT: AnalyticsFailureCategory.TIMEOUT,
    ProviderErrorCategory.NETWORK: AnalyticsFailureCategory.TRANSIENT_SERVER,
    ProviderErrorCategory.SERVER_ERROR: AnalyticsFailureCategory.TRANSIENT_SERVER,
    ProviderErrorCategory.BAD_REQUEST: AnalyticsFailureCategory.BAD_REQUEST,
    ProviderErrorCategory.MALFORMED_RESPONSE: AnalyticsFailureCategory.MALFORMED_RESPONSE,
    ProviderErrorCategory.UNEXPECTED_STATUS: AnalyticsFailureCategory.UNKNOWN,
}


class _Raw(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _RawPublicMetrics(_Raw):
    like_count: int | None = None
    retweet_count: int | None = None
    reply_count: int | None = None
    quote_count: int | None = None
    bookmark_count: int | None = None
    impression_count: int | None = None


class _RawPost(_Raw):
    id: str
    created_at: datetime | None = None
    public_metrics: _RawPublicMetrics | None = None


class _RawProblem(_Raw):
    resource_id: str | None = None
    value: str | None = None
    type: str | None = None
    title: str | None = None
    detail: str | None = None


class _RawLookup(_Raw):
    data: list[_RawPost] = []
    errors: list[_RawProblem] = []


class XAnalyticsProvider:
    platform = "x"
    provider_name = "x"
    metrics_scope: MetricsScope = "public"
    max_batch_size = MAX_IDS_PER_REQUEST

    def __init__(self, client: XApiClient) -> None:
        self._client = client

    @classmethod
    def from_token(
        cls,
        bearer_token: SecretStr | None,
        *,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> "XAnalyticsProvider":
        if bearer_token is None:
            raise ConfigurationError(
                "X_BEARER_TOKEN is not set (required for ANALYTICS_PROVIDER=x; the app-only "
                "token reads public metrics, the publishing keys are never used)"
            )
        return cls(XApiClient(bearer_token, timeout_seconds=timeout_seconds, transport=transport))

    def __repr__(self) -> str:
        return f"XAnalyticsProvider(scope={self.metrics_scope!r})"

    def close(self) -> None:
        self._client.close()

    def fetch_post_metrics(self, post_ids: Sequence[str]) -> AnalyticsFetch:
        ids = list(dict.fromkeys(post_ids))
        if not ids:
            return AnalyticsFetch()
        if len(ids) > MAX_IDS_PER_REQUEST:
            raise AnalyticsError(
                f"at most {MAX_IDS_PER_REQUEST} post ids per request",
                failure_category=AnalyticsFailureCategory.BAD_REQUEST,
            )
        started = time.perf_counter()
        # Errors are raised outside the ``except`` blocks so that nothing (no request,
        # no header, no payload) is attached as ``__context__``.
        failure: AnalyticsError | None = None
        try:
            response = self._client.get(
                LOOKUP_PATH, {"ids": ",".join(ids), "tweet.fields": POST_FIELDS}
            )
        except ProviderError as exc:
            failure = _analytics_error(exc, (time.perf_counter() - started) * 1000)
        if failure is not None:
            raise failure
        latency_ms = (time.perf_counter() - started) * 1000
        body: _RawLookup | None = None
        with contextlib.suppress(ValidationError):
            body = _RawLookup.model_validate(response.body)
        if body is None:
            raise AnalyticsError(
                "X API returned a lookup body in an unexpected shape",
                failure_category=AnalyticsFailureCategory.MALFORMED_RESPONSE,
                http_status=200,
                latency_ms=latency_ms,
            )
        return _normalize(
            ids,
            body,
            latency_ms=latency_ms,
            remaining=response.rate_limit.remaining,
            reset_at=response.rate_limit.reset_at,
        )


def _normalize(
    ids: list[str],
    body: _RawLookup,
    *,
    latency_ms: float,
    remaining: int | None,
    reset_at: datetime | None,
) -> AnalyticsFetch:
    returned = {post.id: post for post in body.data}
    problems = {(p.resource_id or p.value): p for p in body.errors if p.resource_id or p.value}
    records: list[PostMetricRecord] = []
    misses: list[PostMetricMiss] = []
    for post_id in ids:
        post = returned.get(post_id)
        if post is not None:
            m = post.public_metrics or _RawPublicMetrics()
            records.append(
                PostMetricRecord(
                    provider_post_id=post_id,
                    provider_created_at=post.created_at,
                    likes=m.like_count,
                    reposts=m.retweet_count,
                    replies=m.reply_count,
                    quotes=m.quote_count,
                    bookmarks=m.bookmark_count,
                    impressions=m.impression_count,
                )
            )
            continue
        problem = problems.get(post_id)
        misses.append(_miss(post_id, problem))
    return AnalyticsFetch(
        records=records,
        misses=misses,
        metrics_scope="public",
        http_status=200,
        latency_ms=latency_ms,
        posts_returned=len(body.data),
        rate_limit_remaining=remaining,
        rate_limit_reset_at=reset_at,
    )


def _miss(post_id: str, problem: _RawProblem | None) -> PostMetricMiss:
    if problem is None:
        return PostMetricMiss(
            provider_post_id=post_id,
            category=AnalyticsFailureCategory.MALFORMED_RESPONSE,
            detail="the response neither returned the post nor an error for it",
        )
    kind = problem.type or ""
    text = " - ".join(dict.fromkeys(str(p) for p in (problem.title, problem.detail) if p))
    detail = " ".join(text.split())[:_MAX_DETAIL] or "no detail"
    if kind.endswith(_NOT_FOUND):
        return PostMetricMiss(
            provider_post_id=post_id,
            category=AnalyticsFailureCategory.NOT_FOUND,
            detail=f"not found (deleted?): {detail}",
        )
    if kind.endswith(_NOT_AUTHORIZED):
        return PostMetricMiss(
            provider_post_id=post_id,
            category=AnalyticsFailureCategory.NOT_FOUND,
            detail=f"not available (protected or suspended): {detail}",
        )
    return PostMetricMiss(
        provider_post_id=post_id,
        category=AnalyticsFailureCategory.UNKNOWN,
        detail=f"unexpected per-post error: {detail}",
    )


def _analytics_error(exc: ProviderError, latency_ms: float) -> AnalyticsError:
    category = (
        _CATEGORY.get(exc.category, AnalyticsFailureCategory.UNKNOWN)
        if exc.category is not None
        else AnalyticsFailureCategory.UNKNOWN
    )
    reset_at = exc.reset_at if isinstance(exc, RateLimitedError) else None
    if isinstance(exc, RateLimitedError):
        category = AnalyticsFailureCategory.RATE_LIMITED
    return AnalyticsError(
        str(exc),
        failure_category=category,
        http_status=429 if category is AnalyticsFailureCategory.RATE_LIMITED else None,
        latency_ms=latency_ms,
        rate_limit_reset_at=reset_at,
    )
