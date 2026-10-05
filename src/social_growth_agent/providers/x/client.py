"""Minimal HTTP client for the X API v2, with error translation.

Security: the bearer token lives only in this client's default headers. It is never
logged, never put into an exception message, and transport exceptions (whose request
objects carry the headers) are not chained onto the errors raised from here.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import SecretStr

from social_growth_agent.errors import (
    ConfigurationError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitedError,
    TransientProviderError,
)
from social_growth_agent.models import ProviderErrorCategory

X_API_BASE_URL = "https://api.x.com"
USER_AGENT = "social-growth-agent/0.1"
_MAX_DETAIL = 200


@dataclass(frozen=True)
class RateLimit:
    remaining: int | None
    reset_at: datetime | None


@dataclass(frozen=True)
class XResponse:
    body: dict[str, Any]
    rate_limit: RateLimit


class XApiClient:
    """One method, one request. No retries, no pagination, no sleeping."""

    def __init__(
        self,
        bearer_token: SecretStr,
        *,
        timeout_seconds: float,
        base_url: str = X_API_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        token = bearer_token.get_secret_value().strip()
        if not token:
            raise ConfigurationError("X_BEARER_TOKEN is empty")
        self._base_url = base_url
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"XApiClient(base_url={self._base_url!r})"

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, params: dict[str, str]) -> XResponse:
        response: httpx.Response | None = None
        failure: ProviderError | None = None
        try:
            response = self._http.get(path, params=params)
        except httpx.TimeoutException:
            failure = ProviderTimeoutError(
                "X API request timed out", category=ProviderErrorCategory.TIMEOUT
            )
        except httpx.TransportError as exc:
            failure = TransientProviderError(
                f"X API network error ({type(exc).__name__})",
                category=ProviderErrorCategory.NETWORK,
            )
        # Raised outside the except blocks so the httpx exception (and its request,
        # which holds the auth header) is not attached as __context__.
        if failure is not None or response is None:
            raise failure or ProviderError("X API returned no response")

        rate_limit = _rate_limit(response.headers)
        body = _json_body(response)
        if response.status_code != httpx.codes.OK:
            raise _status_error(response.status_code, body, rate_limit)
        if not isinstance(body, dict):
            raise ProviderError(
                "X API returned a non-JSON or non-object body",
                category=ProviderErrorCategory.MALFORMED_RESPONSE,
            )
        return XResponse(body=body, rate_limit=rate_limit)


def _json_body(response: httpx.Response) -> Any:
    try:
        return json.loads(response.content)
    except ValueError:
        return None


def _status_error(status: int, body: Any, rate_limit: RateLimit) -> ProviderError:
    detail = _describe(body)
    suffix = f": {detail}" if detail else ""
    if status == httpx.codes.TOO_MANY_REQUESTS:
        reset = rate_limit.reset_at.isoformat() if rate_limit.reset_at else "unknown"
        return RateLimitedError(
            f"X API rate limit or usage cap reached (HTTP 429); resets at {reset}{suffix}",
            reset_at=rate_limit.reset_at,
        )
    if status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        return ProviderError(
            f"X API rejected the credentials or access level (HTTP {status}){suffix}",
            category=ProviderErrorCategory.AUTH,
        )
    if status == httpx.codes.BAD_REQUEST:
        return ProviderError(
            f"X API rejected the request (HTTP 400){suffix}",
            category=ProviderErrorCategory.BAD_REQUEST,
        )
    if status >= httpx.codes.INTERNAL_SERVER_ERROR:
        return TransientProviderError(
            f"X API server error (HTTP {status}){suffix}",
            category=ProviderErrorCategory.SERVER_ERROR,
        )
    return ProviderError(
        f"X API returned unexpected HTTP {status}{suffix}",
        category=ProviderErrorCategory.UNEXPECTED_STATUS,
    )


def _describe(body: Any) -> str:
    """A short, single-line summary of an X error body (title/detail), if any."""
    if not isinstance(body, dict):
        return ""
    parts = [body.get("title"), body.get("detail")]
    errors = body.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        parts += [errors[0].get("title"), errors[0].get("message")]
    text = " - ".join(dict.fromkeys(str(p) for p in parts if p))
    return " ".join(text.split())[:_MAX_DETAIL]


def _rate_limit(headers: httpx.Headers) -> RateLimit:
    return RateLimit(
        remaining=_int(headers.get("x-rate-limit-remaining")),
        reset_at=_epoch(headers.get("x-rate-limit-reset")),
    )


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _epoch(value: str | None) -> datetime | None:
    seconds = _int(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
