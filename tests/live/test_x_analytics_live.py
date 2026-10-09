"""Opt-in live test of the X analytics read. Never runs in the normal suite.

RUN_LIVE_X_ANALYTICS_TESTS=1 X_BEARER_TOKEN=... X_ANALYTICS_LIVE_POST_ID=<id> \\
  uv run pytest -m live tests/live/test_x_analytics_live.py

Makes exactly one ``GET /2/tweets`` request for one post id (read-only, about one post
read). It checks which public metrics our credentials actually receive; nothing is
written anywhere.
"""

import os

import pytest

from social_growth_agent.config import AppSettings
from social_growth_agent.providers.factory import build_analytics_provider

POST_ID = os.environ.get("X_ANALYTICS_LIVE_POST_ID", "")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_X_ANALYTICS_TESTS") != "1"
        or not os.environ.get("X_BEARER_TOKEN")
        or not POST_ID.isdigit(),
        reason=(
            "set RUN_LIVE_X_ANALYTICS_TESTS=1, X_BEARER_TOKEN and X_ANALYTICS_LIVE_POST_ID "
            "to run the live analytics read"
        ),
    ),
]


def test_live_public_metrics_read_returns_normalized_counts():
    provider = build_analytics_provider(AppSettings(analytics_provider="x"))
    fetch = provider.fetch_post_metrics([POST_ID])

    assert fetch.metrics_scope == "public" and fetch.http_status == 200
    assert len(fetch.records) + len(fetch.misses) == 1
    for record in fetch.records:
        assert record.provider_post_id == POST_ID
        assert record.provider_created_at is not None
        # The public counts the bearer token is expected to receive (validated here).
        for name in ("likes", "reposts", "replies", "quotes"):
            assert getattr(record, name) is not None, name
