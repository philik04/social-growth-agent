"""Offline stand-ins for the X API: canned responses served through httpx.MockTransport."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import SecretStr

from social_growth_agent.providers.x import XResearchProvider
from social_growth_agent.providers.x.client import XApiClient

TOKEN = "test-bearer-SECRET-7f3a9c"
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
RESET_EPOCH = 1_791_116_100  # NOW + 15 minutes, as sent in x-rate-limit-reset

type Reply = httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]


def x_post(
    post_id: str,
    text: str,
    author_id: str = "u1",
    *,
    metrics: dict[str, int] | None = None,
    lang: str | None = "en",
) -> dict[str, Any]:
    post: dict[str, Any] = {
        "id": post_id,
        "text": text,
        "created_at": "2026-10-04T09:30:00.000Z",
        "author_id": author_id,
        "edit_history_tweet_ids": [post_id],
    }
    if lang is not None:
        post["lang"] = lang
    if metrics is not None:
        post["public_metrics"] = metrics
    return post


def full_metrics(likes: int = 10, impressions: int | None = 1000) -> dict[str, int]:
    metrics = {
        "retweet_count": 2,
        "reply_count": 3,
        "like_count": likes,
        "quote_count": 1,
        "bookmark_count": 4,
    }
    if impressions is not None:
        metrics["impression_count"] = impressions
    return metrics


def search_body(
    posts: list[dict[str, Any]], users: list[dict[str, str]] | None = None, **meta: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "data": posts,
        "meta": {"result_count": len(posts), "newest_id": "1", "oldest_id": "0", **meta},
    }
    if users is not None:
        body["includes"] = {"users": users}
    return body


def sample_body(n: int = 3) -> dict[str, Any]:
    posts = [
        x_post(
            f"18000{i}",
            f"Shipping AI agents: lesson {i} #AgentEngineering",
            f"u{i}",
            metrics=full_metrics(likes=10 * (i + 1)),
        )
        for i in range(n)
    ]
    users = [{"id": f"u{i}", "username": f"builder_{i}", "name": f"Builder {i}"} for i in range(n)]
    return search_body(posts, users)


def ok(body: dict[str, Any], **headers: str) -> httpx.Response:
    return httpx.Response(200, json=body, headers=headers)


def error(status: int, body: dict[str, Any] | None = None, **headers: str) -> httpx.Response:
    return httpx.Response(status, json=body or {"title": "Error", "detail": "x"}, headers=headers)


class FakeX:
    """Serves replies in order (the last one repeats) and records every request."""

    def __init__(self, *replies: Reply) -> None:
        self._replies = list(replies) or [ok(sample_body())]
        self.requests: list[httpx.Request] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, Exception):
            raise reply
        if callable(reply) and not isinstance(reply, httpx.Response):
            return reply(request)
        return reply

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def provider(self, **kwargs: Any) -> XResearchProvider:
        client = XApiClient(SecretStr(TOKEN), timeout_seconds=1.0, transport=self.transport())
        return XResearchProvider(client, clock=lambda: NOW, **kwargs)

    @property
    def last_params(self) -> dict[str, str]:
        return dict(self.requests[-1].url.params)
