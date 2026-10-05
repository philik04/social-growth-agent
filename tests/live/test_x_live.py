"""Opt-in live test against the X API. Never runs in the normal suite.

RUN_LIVE_X_TESTS=1 X_BEARER_TOKEN=... uv run pytest -m live tests/live/test_x_live.py

Makes exactly one recent-search request for at most 10 posts.
"""

import os

import pytest

from social_growth_agent.config import AppSettings
from social_growth_agent.models import FetchOutcome, ResearchQuery
from social_growth_agent.providers.factory import build_research_provider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_X_TESTS") != "1" or not os.environ.get("X_BEARER_TOKEN"),
        reason="set RUN_LIVE_X_TESTS=1 and X_BEARER_TOKEN to run live X tests",
    ),
]


def test_live_x_search_returns_normalized_posts_with_one_request():
    settings = AppSettings(research_provider="x", x_max_results_per_query=10)
    provider = build_research_provider(settings)
    result = provider.search(ResearchQuery(text="AI agents lang:en"))

    assert result.fetch.requests_made == 1
    assert result.fetch.outcome in (FetchOutcome.SUCCESS, FetchOutcome.EMPTY)
    assert len(result.posts) <= 10
    for post in result.posts:
        assert post.source_id.startswith("x_")
        assert post.query == "AI agents lang:en"
