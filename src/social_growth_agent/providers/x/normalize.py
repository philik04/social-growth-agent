"""Normalize X recent-search responses into ``SourcePost``s.

Raw payload shapes are validated here and go no further: nothing outside this module
sees X JSON. Missing optional fields become ``None``; nothing is invented.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from social_growth_agent.errors import ProviderError
from social_growth_agent.models import ProviderErrorCategory, SourcePost

SOURCE_ID_PREFIX = "x_"


class _Raw(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _RawMetrics(_Raw):
    like_count: int | None = None
    retweet_count: int | None = None
    reply_count: int | None = None
    quote_count: int | None = None
    impression_count: int | None = None


class _RawPost(_Raw):
    id: str
    text: str
    created_at: datetime
    author_id: str
    lang: str | None = None
    public_metrics: _RawMetrics | None = None


class _RawUser(_Raw):
    id: str
    username: str | None = None


class _RawIncludes(_Raw):
    users: list[_RawUser] = []


class _RawError(_Raw):
    title: str | None = None
    detail: str | None = None


class _RawMeta(_Raw):
    result_count: int | None = None


class _RawSearchResponse(_Raw):
    data: list[_RawPost] | None = None
    includes: _RawIncludes | None = None
    meta: _RawMeta | None = None
    errors: list[_RawError] | None = None


class NormalizedPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    posts: list[SourcePost]
    users_returned: int


def source_id(platform_post_id: str) -> str:
    return f"{SOURCE_ID_PREFIX}{platform_post_id}"


def normalize_search_response(
    body: dict[str, Any], *, query: str, retrieved_at: datetime
) -> NormalizedPage:
    try:
        return _normalize(body, query, retrieved_at)
    except ValidationError as exc:
        # Report field locations only; never echo payload content.
        locations = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})[:5]
        raise ProviderError(
            f"X API response did not match the expected shape at {locations}",
            category=ProviderErrorCategory.MALFORMED_RESPONSE,
        ) from None


def _normalize(body: dict[str, Any], query: str, retrieved_at: datetime) -> NormalizedPage:
    raw = _RawSearchResponse.model_validate(body)
    if not raw.data:
        if raw.errors:
            first = raw.errors[0]
            detail = " - ".join(p for p in (first.title, first.detail) if p)[:200]
            raise ProviderError(
                f"X API returned errors and no posts: {detail}",
                category=ProviderErrorCategory.BAD_REQUEST,
            )
        return NormalizedPage(posts=[], users_returned=0)

    users = raw.includes.users if raw.includes else []
    usernames = {u.id: u.username for u in users}
    posts = [_post(p, usernames, query, retrieved_at) for p in raw.data]
    return NormalizedPage(posts=posts, users_returned=len(users))


def _post(
    raw: _RawPost, usernames: dict[str, str | None], query: str, retrieved_at: datetime
) -> SourcePost:
    metrics = raw.public_metrics or _RawMetrics()
    return SourcePost(
        source_id=source_id(raw.id),
        platform="x",
        author_id=raw.author_id,
        author_username=usernames.get(raw.author_id),
        text=raw.text,
        created_at=raw.created_at,
        lang=raw.lang,
        likes=metrics.like_count,
        reposts=metrics.retweet_count,
        replies=metrics.reply_count,
        quotes=metrics.quote_count,
        impressions=metrics.impression_count,
        query=query,
        retrieved_at=retrieved_at,
    )
