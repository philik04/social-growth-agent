"""X publisher: ``POST /2/tweets`` with OAuth 1.0a user context.

The only code that knows the publish endpoint, its auth header, raw JSON and error
mapping. Everything it raises is a ``PublishError`` subclass that states what is known
about the outcome; nothing here retries, sleeps or touches the database.

Outcome classification (X API v2 has no idempotency key, so this is the best a client
can know):

- definitely not sent: the TCP/TLS connection (or the pool) failed before any byte of
  the request was written -> ``PublishNotSentError``;
- definitely rejected: 4xx -> ``PublishRejectedError`` (401/403 auth, 403 duplicate
  content, 400 bad request, 429 rate limited with the reset time);
- unknown: read/write timeout, broken connection after sending, 5xx, or a 2xx without
  a post id -> ``PublishOutcomeUnknownError``.

Security: the credentials are revealed only to compute each request's signature. The
signed header is never logged or put into an error message, and httpx exceptions
(whose request objects carry it) are never chained onto the errors raised here.
"""

import json
import time
from datetime import datetime
from typing import Any

import httpx

from social_growth_agent.config import AppSettings
from social_growth_agent.errors import (
    PublishError,
    PublishNotSentError,
    PublishOutcomeUnknownError,
    PublishRejectedError,
)
from social_growth_agent.models import PublishFailureCategory, PublishRequest, PublishResult
from social_growth_agent.providers.x.client import (
    USER_AGENT,
    X_API_BASE_URL,
    RateLimit,
    _describe,
    _epoch,
    _rate_limit,
)
from social_growth_agent.providers.x.oauth1 import OAuth1Credentials, authorization_header

CREATE_POST_PATH = "/2/tweets"
POST_URL_TEMPLATE = "https://x.com/i/web/status/{id}"

# Raised by httpx before the request is written: nothing reached X.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

_DUPLICATE_MARKERS = ("duplicate content",)


class XPublisher:
    """Implements ``SocialPublisher`` for X. One request per ``publish`` call."""

    platform = "x"
    provider_name = "x"

    def __init__(
        self,
        credentials: OAuth1Credentials,
        *,
        timeout_seconds: float,
        base_url: str = X_API_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._credentials = credentials
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self._base_url,
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    @classmethod
    def from_settings(
        cls, settings: AppSettings, *, transport: httpx.BaseTransport | None = None
    ) -> "XPublisher":
        credentials = OAuth1Credentials.from_values(
            consumer_key=settings.x_publish_api_key,
            consumer_secret=settings.x_publish_api_secret,
            token=settings.x_publish_access_token,
            token_secret=settings.x_publish_access_token_secret,
        )
        return cls(
            credentials, timeout_seconds=settings.x_publish_timeout_seconds, transport=transport
        )

    def __repr__(self) -> str:
        return f"XPublisher(base_url={self._base_url!r})"

    def close(self) -> None:
        self._http.close()

    def publish(self, request: PublishRequest) -> PublishResult:
        url = self._base_url + CREATE_POST_PATH
        headers = {"Authorization": authorization_header("POST", url, self._credentials)}
        started = time.perf_counter()
        response: httpx.Response | None = None
        failure: PublishError | None = None
        try:
            response = self._http.post(
                CREATE_POST_PATH, json={"text": request.content}, headers=headers
            )
        except _NOT_SENT as exc:
            failure = PublishNotSentError(
                f"X publish request was not sent ({type(exc).__name__})",
                failure_category=PublishFailureCategory.NOT_SENT,
            )
        except httpx.TimeoutException as exc:
            failure = PublishOutcomeUnknownError(
                f"X publish request timed out after sending ({type(exc).__name__}); "
                "the post may exist",
                failure_category=PublishFailureCategory.TIMEOUT,
            )
        except httpx.TransportError as exc:
            failure = PublishOutcomeUnknownError(
                f"X publish connection failed after sending ({type(exc).__name__}); "
                "the post may exist",
                failure_category=PublishFailureCategory.TRANSPORT_ERROR,
            )
        latency_ms = (time.perf_counter() - started) * 1000
        # Raised outside the except blocks: the httpx exception holds the signed request.
        if failure is not None or response is None:
            failure = failure or PublishOutcomeUnknownError(
                "X publish returned no response",
                failure_category=PublishFailureCategory.TRANSPORT_ERROR,
            )
            failure.latency_ms = latency_ms
            raise failure
        return _result(response, latency_ms)


def _result(response: httpx.Response, latency_ms: float) -> PublishResult:
    status = response.status_code
    rate_limit = _rate_limit(response.headers)
    body = _json(response)
    if status in (httpx.codes.OK, httpx.codes.CREATED):
        post_id = _post_id(body)
        if post_id is None:
            raise PublishOutcomeUnknownError(
                f"X publish returned HTTP {status} without a post id; the post may exist",
                failure_category=PublishFailureCategory.MALFORMED_RESPONSE,
                http_status=status,
                latency_ms=latency_ms,
            )
        return PublishResult(
            provider_post_id=post_id,
            provider_post_url=POST_URL_TEMPLATE.format(id=post_id),
            http_status=status,
            latency_ms=latency_ms,
            rate_limit_remaining=rate_limit.remaining,
            rate_limit_reset_at=rate_limit.reset_at,
        )
    raise _status_error(status, body, response.headers, rate_limit, latency_ms)


def _status_error(
    status: int, body: Any, headers: httpx.Headers, rate_limit: RateLimit, latency_ms: float
) -> PublishError:
    detail = _describe(body)
    suffix = f": {detail}" if detail else ""
    if status == httpx.codes.TOO_MANY_REQUESTS:
        reset = _latest(rate_limit.reset_at, _epoch(headers.get("x-user-limit-24hour-reset")))
        when = reset.isoformat() if reset else "unknown"
        return PublishRejectedError(
            f"X publish rate limit reached (HTTP 429); resets at {when}{suffix}",
            failure_category=PublishFailureCategory.RATE_LIMITED,
            http_status=status,
            latency_ms=latency_ms,
            rate_limit_reset_at=reset,
        )
    if status == httpx.codes.FORBIDDEN and any(m in detail.lower() for m in _DUPLICATE_MARKERS):
        return PublishRejectedError(
            f"X refused duplicate content (HTTP 403){suffix}",
            failure_category=PublishFailureCategory.DUPLICATE_CONTENT,
            http_status=status,
            latency_ms=latency_ms,
        )
    if status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        return PublishRejectedError(
            f"X rejected the publishing credentials or permissions (HTTP {status}){suffix}",
            failure_category=PublishFailureCategory.AUTH,
            http_status=status,
            latency_ms=latency_ms,
        )
    if status == httpx.codes.REQUEST_TIMEOUT or status >= httpx.codes.INTERNAL_SERVER_ERROR:
        return PublishOutcomeUnknownError(
            f"X publish failed with HTTP {status}; the post may exist{suffix}",
            failure_category=PublishFailureCategory.SERVER_ERROR,
            http_status=status,
            latency_ms=latency_ms,
        )
    if httpx.codes.BAD_REQUEST <= status < httpx.codes.INTERNAL_SERVER_ERROR:
        return PublishRejectedError(
            f"X rejected the post (HTTP {status}){suffix}",
            failure_category=PublishFailureCategory.BAD_REQUEST,
            http_status=status,
            latency_ms=latency_ms,
        )
    # 1xx/3xx or another unexpected 2xx: we cannot tell whether a post was created.
    return PublishOutcomeUnknownError(
        f"X publish returned unexpected HTTP {status}; the post may exist",
        failure_category=PublishFailureCategory.MALFORMED_RESPONSE,
        http_status=status,
        latency_ms=latency_ms,
    )


def _json(response: httpx.Response) -> Any:
    try:
        return json.loads(response.content)
    except ValueError:
        return None


def _post_id(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    post_id = data.get("id")
    return post_id if isinstance(post_id, str) and post_id.isdigit() else None


def _latest(*times: datetime | None) -> datetime | None:
    known = [t for t in times if t is not None]
    return max(known) if known else None
