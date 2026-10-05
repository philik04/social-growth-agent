"""XResearchProvider: normalization, query translation, errors, cost limits, secrecy.

All offline: responses are served by httpx.MockTransport (see tests/x_fakes.py).
"""

import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from social_growth_agent.config import AppSettings
from social_growth_agent.errors import (
    ConfigurationError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitedError,
    TransientProviderError,
)
from social_growth_agent.models import (
    FetchOutcome,
    ProviderErrorCategory,
    ResearchQuery,
    SourcePost,
)
from social_growth_agent.providers.factory import build_research_provider
from social_growth_agent.providers.mocks import MockResearchProvider
from social_growth_agent.providers.x import XResearchProvider, build_search_request
from social_growth_agent.providers.x import provider as x_provider_module
from social_growth_agent.providers.x.client import XApiClient
from tests.x_fakes import (
    NOW,
    RESET_EPOCH,
    TOKEN,
    FakeX,
    error,
    full_metrics,
    ok,
    sample_body,
    search_body,
    x_post,
)

Q = ResearchQuery(text="AI agents lang:en")


# --- 1. normalization -----------------------------------------------------------------


def test_x_posts_are_normalized_into_source_posts():
    fake = FakeX(ok(sample_body(2)))
    result = fake.provider().search(Q)

    first = result.posts[0]
    assert isinstance(first, SourcePost)
    assert first.source_id == "x_180000"
    assert first.platform == "x"
    assert (first.author_id, first.author_username) == ("u0", "builder_0")
    assert first.text == "Shipping AI agents: lesson 0 #AgentEngineering"
    assert first.created_at == datetime(2026, 10, 4, 9, 30, tzinfo=UTC)
    assert first.lang == "en"
    assert (first.likes, first.reposts, first.replies, first.quotes, first.impressions) == (
        10,
        2,
        3,
        1,
        1000,
    )
    assert first.query == "AI agents lang:en"  # the original query, not the effective one
    assert first.retrieved_at == NOW
    assert [p.source_id for p in result.posts] == ["x_180000", "x_180001"]


# --- 2. missing optional metrics ------------------------------------------------------


def test_missing_optional_fields_stay_none_and_are_not_invented():
    body = search_body(
        [
            x_post("1", "no metrics at all", "u1", lang=None),
            x_post("2", "no impressions", "u2", metrics=full_metrics(impressions=None)),
            x_post("3", "author not in includes", "u404", metrics={"like_count": 5}),
        ],
        users=[{"id": "u1", "username": "alpha"}, {"id": "u2", "username": "beta"}],
    )
    posts = FakeX(ok(body)).provider().search(Q).posts

    bare, no_impr, partial = posts
    assert (bare.likes, bare.reposts, bare.replies, bare.quotes, bare.impressions) == (None,) * 5
    assert bare.lang is None
    assert bare.missing_metrics() == ["likes", "reposts", "replies", "quotes", "impressions"]
    assert no_impr.impressions is None
    assert no_impr.likes == 10
    assert partial.author_username is None
    assert partial.likes == 5
    assert partial.missing_metrics() == ["reposts", "replies", "quotes", "impressions"]


# --- 3. query translation -------------------------------------------------------------


def test_query_is_translated_into_minimal_x_parameters():
    fake = FakeX()
    provider = fake.provider()
    result = provider.search(Q)

    assert fake.requests[0].url.path == "/2/tweets/search/recent"
    assert fake.last_params == {
        "query": "AI agents lang:en -is:retweet",
        "max_results": "10",
        "tweet.fields": "created_at,author_id,lang,public_metrics",
        "expansions": "author_id",
        "user.fields": "username",
        "sort_order": "relevancy",
    }
    assert Q.text == "AI agents lang:en"  # the caller's query is untouched
    assert result.fetch.query == "AI agents lang:en"
    assert result.fetch.effective_query == "AI agents lang:en -is:retweet"


def test_or_queries_are_grouped_before_operators_are_appended():
    query = ResearchQuery(
        text='"agent engineering" OR evals',
        language="en",
        author_username="some_dev",
        recency_hours=24,
    )
    request = build_search_request(query, max_results_cap=10, now=NOW)

    assert request.effective_query == (
        '("agent engineering" OR evals) lang:en from:some_dev -is:retweet'
    )
    assert request.original_query == '"agent engineering" OR evals'
    assert request.params["start_time"] == "2026-10-03T12:00:00Z"


def test_existing_operators_are_not_duplicated_and_reposts_can_be_kept():
    query = ResearchQuery(text="agents lang:de -is:retweet", language="en")
    assert build_search_request(query, max_results_cap=10, now=NOW).effective_query == (
        "agents lang:de -is:retweet"
    )
    keep = ResearchQuery(text="agents", exclude_reposts=False)
    assert build_search_request(keep, max_results_cap=10, now=NOW).effective_query == "agents"


def test_query_validation_rejects_unsupported_values():
    with pytest.raises(ValidationError):
        ResearchQuery(text="")
    with pytest.raises(ValidationError):
        ResearchQuery(text="a", recency_hours=24 * 8)  # beyond the 7-day window
    with pytest.raises(ValidationError):
        ResearchQuery(text="a", max_results=5)  # X minimum is 10
    with pytest.raises(ValidationError):
        ResearchQuery(text="a", author_username="not a handle")


# --- 4. auth and configuration --------------------------------------------------------


def test_x_provider_requires_a_token_at_startup():
    with pytest.raises(ConfigurationError, match="X_BEARER_TOKEN is not set"):
        build_research_provider(AppSettings(_env_file=None, research_provider="x"))
    with pytest.raises(ConfigurationError, match="empty"):
        XApiClient(SecretStr("   "), timeout_seconds=1)


def test_factory_builds_x_provider_with_configured_limits():
    settings = AppSettings(
        _env_file=None,
        research_provider="x",
        x_bearer_token=TOKEN,
        x_max_results_per_query=25,
        x_max_queries_per_run=2,
    )
    provider = build_research_provider(settings)
    assert isinstance(provider, XResearchProvider)
    assert provider.max_requests_per_run == 2
    assert provider.synthetic is False
    assert TOKEN not in repr(settings)
    assert TOKEN not in repr(provider)


def test_mock_remains_the_default_research_provider():
    assert isinstance(build_research_provider(AppSettings(_env_file=None)), MockResearchProvider)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_fail_clearly_and_are_not_retryable(status):
    body = {"title": "Unauthorized", "detail": "Unauthorized", "type": "about:blank"}
    with pytest.raises(ProviderError) as info:
        FakeX(error(status, body)).provider().search(Q)

    exc = info.value
    assert not isinstance(exc, TransientProviderError)
    assert exc.category is ProviderErrorCategory.AUTH
    assert f"HTTP {status}" in str(exc)
    assert TOKEN not in str(exc)


# --- 5. API errors --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "error_type", "category"),
    [
        (
            error(400, {"title": "Invalid Request", "detail": "bad query"}),
            ProviderError,
            "bad_request",
        ),
        (error(404), ProviderError, "unexpected_status"),
        (error(500), TransientProviderError, "server_error"),
        (error(503), TransientProviderError, "server_error"),
        (httpx.ReadTimeout("timed out"), ProviderTimeoutError, "timeout"),
        (httpx.ConnectError("connection refused"), TransientProviderError, "network"),
        (httpx.Response(200, text="<html>oops</html>"), ProviderError, "malformed_response"),
        (ok({"data": {"not": "a list"}}), ProviderError, "malformed_response"),
        (ok({"data": [{"id": "1", "author_id": "u"}]}), ProviderError, "malformed_response"),
        (
            ok({"data": [x_post("1", "t", metrics={"like_count": -1})]}),
            ProviderError,
            "malformed_response",
        ),
        (
            ok({"errors": [{"title": "Not Found Error", "detail": "x"}]}),
            ProviderError,
            "bad_request",
        ),
    ],
)
def test_api_failures_translate_predictably(reply, error_type, category):
    with pytest.raises(error_type) as info:
        FakeX(reply).provider().search(Q)

    exc = info.value
    assert type(exc) is error_type
    assert exc.category == category
    fetch = exc.research_fetch
    assert fetch is not None
    assert (fetch.outcome, fetch.error_category, fetch.requests_made) == (
        FetchOutcome.ERROR,
        category,
        1,
    )
    assert TOKEN not in str(exc)


def test_400_detail_from_x_is_surfaced_without_payload_dumps():
    body = {"title": "Invalid Request", "detail": "One or more parameters are invalid."}
    with pytest.raises(ProviderError, match="One or more parameters are invalid"):
        FakeX(error(400, body)).provider().search(Q)


# --- 6. rate limits -------------------------------------------------------------------


def test_rate_limit_records_reset_time_and_is_not_transient():
    reply = error(
        429,
        {"title": "Too Many Requests", "detail": "Too Many Requests"},
        **{"x-rate-limit-remaining": "0", "x-rate-limit-reset": str(RESET_EPOCH)},
    )
    with pytest.raises(RateLimitedError) as info:
        FakeX(reply).provider().search(Q)

    exc = info.value
    reset = datetime.fromtimestamp(RESET_EPOCH, tz=UTC)
    assert reset == NOW + timedelta(minutes=15)
    assert not isinstance(exc, TransientProviderError)  # the graph never retries it
    assert exc.reset_at == reset
    assert exc.category is ProviderErrorCategory.RATE_LIMITED
    assert reset.isoformat() in str(exc)
    fetch = exc.research_fetch
    assert fetch is not None
    assert fetch.rate_limit_reset_at == reset
    assert fetch.rate_limit_remaining == 0
    assert fetch.error_category is ProviderErrorCategory.RATE_LIMITED


def test_rate_limit_without_headers_still_fails_explicitly():
    with pytest.raises(RateLimitedError, match="resets at unknown") as info:
        FakeX(error(429)).provider().search(Q)
    assert info.value.reset_at is None


def test_successful_fetch_records_remaining_rate_limit():
    reply = ok(sample_body(1), **{"x-rate-limit-remaining": "449", "x-rate-limit-reset": "1"})
    fetch = FakeX(reply).provider().search(Q).fetch
    assert fetch.rate_limit_remaining == 449


# --- 7. empty results -----------------------------------------------------------------


def test_empty_results_are_returned_safely():
    result = FakeX(ok({"meta": {"result_count": 0}})).provider().search(Q)

    assert result.posts == []
    assert result.fetch.outcome is FetchOutcome.EMPTY
    assert (result.fetch.posts_fetched, result.fetch.requests_made) == (0, 1)


# --- 14. cost limits ------------------------------------------------------------------


def test_max_results_is_capped_by_configuration():
    fake = FakeX()
    fake.provider(max_results_per_query=10).search(ResearchQuery(text="a", max_results=100))
    assert fake.last_params["max_results"] == "10"

    fake.provider(max_results_per_query=25).search(ResearchQuery(text="a"))
    assert fake.last_params["max_results"] == "25"


def test_one_search_is_exactly_one_request_without_pagination():
    body = sample_body(3) | {"meta": {"result_count": 3, "next_token": "more"}}
    fake = FakeX(ok(body))
    result = fake.provider().search(Q)

    assert len(fake.requests) == 1
    assert "next_token" not in fake.last_params
    assert result.fetch.requests_made == 1
    assert result.fetch.posts_fetched == 3
    assert result.fetch.users_fetched == 3
    assert result.fetch.max_results == 10


def test_overlong_effective_query_is_rejected_before_any_request(monkeypatch):
    monkeypatch.setattr(x_provider_module, "X_MAX_QUERY_LENGTH", 100)
    fake = FakeX()
    with pytest.raises(ProviderError) as info:
        fake.provider().search(ResearchQuery(text="a" * 120))

    assert fake.requests == []
    assert info.value.category is ProviderErrorCategory.BAD_REQUEST
    assert info.value.research_fetch is not None
    assert info.value.research_fetch.requests_made == 0


@pytest.mark.parametrize(
    "kwargs",
    [{"max_results_per_query": 5}, {"max_results_per_query": 101}, {"max_queries_per_run": 0}],
)
def test_provider_rejects_limits_outside_safe_bounds(kwargs):
    with pytest.raises(ConfigurationError):
        FakeX().provider(**kwargs)


def test_settings_reject_runaway_limits():
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, x_max_queries_per_run=50)
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, x_max_results_per_query=500)
    defaults = AppSettings(_env_file=None)
    assert (defaults.x_max_results_per_query, defaults.x_max_queries_per_run) == (10, 1)


# --- secrecy --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [httpx.ReadTimeout("timed out"), httpx.ConnectError("refused"), error(401), error(429)],
)
def test_errors_never_carry_the_token_or_the_request(reply):
    with pytest.raises(ProviderError) as info:
        FakeX(reply).provider().search(Q)

    exc = info.value
    assert TOKEN not in str(exc)
    assert TOKEN not in repr(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None  # the httpx exception (and its headers) is not chained


def test_token_is_sent_only_as_the_authorization_header():
    fake = FakeX()
    fake.provider().search(Q)
    request = fake.requests[0]
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(request.url)


def test_fetch_logs_contain_metadata_but_no_secrets(caplog):
    caplog.set_level(logging.INFO, logger="social_growth_agent")
    FakeX(ok(sample_body(2))).provider().search(Q)
    with pytest.raises(ProviderError):
        FakeX(error(401)).provider().search(Q)

    records = [r for r in caplog.records if r.getMessage() == "research fetch"]
    assert len(records) == 2
    ok_record, failed = records
    assert (ok_record.provider, ok_record.requests, ok_record.posts_fetched) == ("x", 1, 2)
    assert ok_record.outcome == "success"
    assert failed.error_category == ProviderErrorCategory.AUTH
    for record in caplog.records:
        assert TOKEN not in str(record.__dict__)
