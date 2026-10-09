"""X analytics provider (offline): request shape, normalization, NULL vs 0, errors."""

import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from social_growth_agent.config import AppSettings
from social_growth_agent.errors import AnalyticsError, ConfigurationError
from social_growth_agent.models import AnalyticsFailureCategory
from social_growth_agent.providers.factory import build_analytics_provider
from social_growth_agent.providers.mocks import MockAnalyticsProvider
from social_growth_agent.providers.x import XAnalyticsProvider
from social_growth_agent.providers.x.analytics import LOOKUP_PATH, MAX_IDS_PER_REQUEST
from social_growth_agent.providers.x.client import XApiClient
from tests.x_fakes import RESET_EPOCH, TOKEN, FakeX, error, full_metrics, ok

CREATED = "2026-10-04T09:30:00.000Z"


def post(post_id: str, metrics: dict[str, int] | None = None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"id": post_id, "text": "hello", "created_at": CREATED, **extra}
    if metrics is not None:
        body["public_metrics"] = metrics
    return body


def problem(post_id: str, kind: str) -> dict[str, Any]:
    return {
        "resource_id": post_id,
        "value": post_id,
        "type": f"https://api.twitter.com/2/problems/{kind}",
        "title": "Not Found Error",
        "detail": f"Could not find tweet with ids: [{post_id}].",
    }


def provider(fake: FakeX) -> XAnalyticsProvider:
    client = XApiClient(SecretStr(TOKEN), timeout_seconds=1.0, transport=fake.transport())
    return XAnalyticsProvider(client)


def test_lookup_requests_public_metrics_and_creation_time_for_the_batch():
    fake = FakeX(ok({"data": [post("1", full_metrics()), post("2", full_metrics())]}))
    provider(fake).fetch_post_metrics(["1", "2", "1"])

    [request] = fake.requests
    assert request.method == "GET"
    assert request.url.path == LOOKUP_PATH
    assert fake.last_params == {"ids": "1,2", "tweet.fields": "created_at,public_metrics"}
    assert "non_public_metrics" not in str(request.url)
    assert "organic_metrics" not in str(request.url)


def test_public_metrics_are_mapped_field_by_field():
    metrics = full_metrics(likes=7, impressions=1234)
    fetch = provider(FakeX(ok({"data": [post("42", metrics)]}))).fetch_post_metrics(["42"])

    [record] = fetch.records
    assert record.provider_post_id == "42"
    assert record.provider_created_at == datetime(2026, 10, 4, 9, 30, tzinfo=UTC)
    assert (record.likes, record.reposts, record.replies, record.quotes) == (7, 2, 3, 1)
    assert (record.bookmarks, record.impressions) == (4, 1234)
    assert fetch.metrics_scope == "public"
    assert (fetch.http_status, fetch.posts_returned, fetch.misses) == (200, 1, [])


def test_missing_metrics_stay_null_and_reported_zeros_stay_zero():
    zeros = {
        "like_count": 0,
        "retweet_count": 0,
        "reply_count": 0,
        "quote_count": 0,
        "bookmark_count": 0,
        "impression_count": 0,
    }
    body = {
        "data": [
            post("1", full_metrics(impressions=None)),  # impression_count absent
            post("2", zeros),
            post("3"),  # no public_metrics object at all
        ]
    }
    one, two, three = provider(FakeX(ok(body))).fetch_post_metrics(["1", "2", "3"]).records

    assert one.impressions is None and one.likes == 10
    assert (two.likes, two.reposts, two.replies, two.quotes, two.bookmarks, two.impressions) == (
        0,
        0,
        0,
        0,
        0,
        0,
    )
    assert three.likes is None and three.impressions is None and three.bookmarks is None


def test_missing_created_at_is_null_not_invented():
    body = {"data": [{"id": "1", "text": "x", "public_metrics": full_metrics()}]}
    [record] = provider(FakeX(ok(body))).fetch_post_metrics(["1"]).records
    assert record.provider_created_at is None


def test_out_of_order_batches_are_matched_by_id():
    body = {"data": [post("3", full_metrics(likes=3)), post("1", full_metrics(likes=1))]}
    fetch = provider(FakeX(ok(body))).fetch_post_metrics(["1", "3"])
    assert {r.provider_post_id: r.likes for r in fetch.records} == {"1": 1, "3": 3}


def test_deleted_and_protected_posts_are_not_found_misses():
    body = {
        "data": [post("1", full_metrics())],
        "errors": [
            problem("2", "resource-not-found"),
            problem("3", "not-authorized-for-resource"),
        ],
    }
    fetch = provider(FakeX(ok(body))).fetch_post_metrics(["1", "2", "3"])

    assert [r.provider_post_id for r in fetch.records] == ["1"]
    misses = {m.provider_post_id: m for m in fetch.misses}
    assert misses["2"].category is AnalyticsFailureCategory.NOT_FOUND
    assert "deleted" in misses["2"].detail
    assert misses["3"].category is AnalyticsFailureCategory.NOT_FOUND
    assert "protected" in misses["3"].detail


def test_only_errors_and_no_data_is_still_a_valid_response():
    body = {"errors": [problem("9", "resource-not-found")]}
    fetch = provider(FakeX(ok(body))).fetch_post_metrics(["9"])
    assert fetch.records == [] and fetch.posts_returned == 0
    assert fetch.misses[0].category is AnalyticsFailureCategory.NOT_FOUND


def test_an_id_neither_returned_nor_reported_is_a_malformed_response():
    fetch = provider(FakeX(ok({"data": [post("1", full_metrics())]}))).fetch_post_metrics(
        ["1", "2"]
    )
    [miss] = fetch.misses
    assert (miss.provider_post_id, miss.category) == (
        "2",
        AnalyticsFailureCategory.MALFORMED_RESPONSE,
    )


def test_an_unknown_per_post_error_is_unknown_not_not_found():
    body = {"errors": [problem("5", "something-new")]}
    [miss] = provider(FakeX(ok(body))).fetch_post_metrics(["5"]).misses
    assert miss.category is AnalyticsFailureCategory.UNKNOWN


@pytest.mark.parametrize(
    ("reply", "category"),
    [
        (error(401), AnalyticsFailureCategory.AUTH),
        (error(403), AnalyticsFailureCategory.AUTH),
        (error(400), AnalyticsFailureCategory.BAD_REQUEST),
        (error(503), AnalyticsFailureCategory.TRANSIENT_SERVER),
        (error(404), AnalyticsFailureCategory.UNKNOWN),
        (httpx.ReadTimeout("timed out"), AnalyticsFailureCategory.TIMEOUT),
        (httpx.ConnectError("refused"), AnalyticsFailureCategory.TRANSIENT_SERVER),
        (httpx.Response(200, content=b"<html>"), AnalyticsFailureCategory.MALFORMED_RESPONSE),
        (ok({"data": "not-a-list"}), AnalyticsFailureCategory.MALFORMED_RESPONSE),
        (ok({"data": [{"no_id": True}]}), AnalyticsFailureCategory.MALFORMED_RESPONSE),
    ],
)
def test_request_failures_map_to_analytics_categories(reply, category):
    with pytest.raises(AnalyticsError) as info:
        provider(FakeX(reply)).fetch_post_metrics(["1"])
    assert info.value.failure_category is category
    assert info.value.latency_ms is not None


def test_rate_limit_carries_the_reset_time_and_status():
    reply = error(429, **{"x-rate-limit-remaining": "0", "x-rate-limit-reset": str(RESET_EPOCH)})
    with pytest.raises(AnalyticsError) as info:
        provider(FakeX(reply)).fetch_post_metrics(["1"])
    exc = info.value
    assert exc.failure_category is AnalyticsFailureCategory.RATE_LIMITED
    assert exc.http_status == 429
    assert exc.rate_limit_reset_at == datetime.fromtimestamp(RESET_EPOCH, tz=UTC)


def test_successful_lookup_records_rate_limit_headers():
    reply = ok(
        {"data": [post("1", full_metrics())]},
        **{"x-rate-limit-remaining": "299", "x-rate-limit-reset": str(RESET_EPOCH)},
    )
    fetch = provider(FakeX(reply)).fetch_post_metrics(["1"])
    assert fetch.rate_limit_remaining == 299
    assert fetch.rate_limit_reset_at == datetime.fromtimestamp(RESET_EPOCH, tz=UTC)


def test_more_than_one_hundred_ids_are_refused_before_any_request():
    fake = FakeX()
    with pytest.raises(AnalyticsError) as info:
        provider(fake).fetch_post_metrics([str(i) for i in range(MAX_IDS_PER_REQUEST + 1)])
    assert info.value.failure_category is AnalyticsFailureCategory.BAD_REQUEST
    assert fake.requests == []


def test_no_ids_make_no_request():
    fake = FakeX()
    fetch = provider(fake).fetch_post_metrics([])
    assert fake.requests == [] and fetch.records == []


@pytest.mark.parametrize(
    "reply",
    [
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("refused"),
        error(401),
        error(429),
        ok({"data": 5}),
    ],
)
def test_errors_never_carry_the_token_or_the_request(reply):
    with pytest.raises(AnalyticsError) as info:
        provider(FakeX(reply)).fetch_post_metrics(["1"])
    exc = info.value
    assert TOKEN not in str(exc) and TOKEN not in repr(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None


def test_token_is_only_the_authorization_header_and_never_in_repr(caplog):
    caplog.set_level(logging.DEBUG, logger="social_growth_agent")
    fake = FakeX(ok({"data": [post("1", full_metrics())]}))
    x = provider(fake)
    x.fetch_post_metrics(["1"])
    assert fake.requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(fake.requests[0].url)
    assert TOKEN not in repr(x)
    for record in caplog.records:
        assert TOKEN not in str(record.__dict__)


def test_x_analytics_requires_the_bearer_token():
    with pytest.raises(ConfigurationError, match="X_BEARER_TOKEN"):
        XAnalyticsProvider.from_token(None, timeout_seconds=1.0)


def test_factory_builds_the_configured_analytics_provider():
    mock = build_analytics_provider(AppSettings(_env_file=None))
    assert isinstance(mock, MockAnalyticsProvider)

    settings = AppSettings(
        _env_file=None,
        analytics_provider="x",
        x_bearer_token=SecretStr(TOKEN),
    )
    x = build_analytics_provider(settings)
    assert isinstance(x, XAnalyticsProvider)
    assert x.metrics_scope == "public" and x.max_batch_size == 100

    with pytest.raises(ConfigurationError):
        build_analytics_provider(AppSettings(_env_file=None, analytics_provider="x"))
