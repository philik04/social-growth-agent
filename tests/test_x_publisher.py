"""OAuth 1.0a signing and the X publisher's outcome classification (offline)."""

import httpx
import pytest
from pydantic import SecretStr

from social_growth_agent.config import AppSettings
from social_growth_agent.errors import (
    ConfigurationError,
    PublishNotSentError,
    PublishOutcomeUnknownError,
    PublishRejectedError,
)
from social_growth_agent.models import PublishFailureCategory, PublishRequest
from social_growth_agent.providers.factory import build_publisher
from social_growth_agent.providers.x import XPublisher
from social_growth_agent.providers.x.oauth1 import (
    OAuth1Credentials,
    authorization_header,
    percent_encode,
    sign,
    signature_base_string,
)
from tests.x_fakes import RESET_EPOCH, FakeX, error

# X's own worked example ("Authorizing a request" / "Creating a signature").
EXAMPLE = {
    "consumer_key": "xvz1evFS4wEEPTGEFPHBog",
    "consumer_secret": "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
    "token": "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
    "token_secret": "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
    "nonce": "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
    "timestamp": 1318622958,
    "url": "https://api.twitter.com/1.1/statuses/update.json",
    "params": {
        "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
        "include_entities": "true",
    },
    "base": (
        "POST&https%3A%2F%2Fapi.twitter.com%2F1.1%2Fstatuses%2Fupdate.json&include_entities"
        "%3Dtrue%26oauth_consumer_key%3Dxvz1evFS4wEEPTGEFPHBog%26oauth_nonce%3DkYjzVBB8Y0ZFabx"
        "SWbWovY3uYSQ2pTgmZeNu2VS4cg%26oauth_signature_method%3DHMAC-SHA1%26oauth_timestamp"
        "%3D1318622958%26oauth_token%3D370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb%26"
        "oauth_version%3D1.0%26status%3DHello%2520Ladies%2520%252B%2520Gentlemen%252C%2520a"
        "%2520signed%2520OAuth%2520request%2521"
    ),
    "signature": "hCtSmYh+iHYCEqBWrE7C7hYmtUk=",
}

CREDENTIALS = OAuth1Credentials(
    consumer_key=SecretStr("publish-key-SECRET-aa11"),
    consumer_secret=SecretStr("publish-secret-SECRET-bb22"),
    token=SecretStr("publish-token-SECRET-cc33"),
    token_secret=SecretStr("publish-token-secret-SECRET-dd44"),
)
SECRET_VALUES = (
    "publish-key-SECRET-aa11",
    "publish-secret-SECRET-bb22",
    "publish-token-SECRET-cc33",
    "publish-token-secret-SECRET-dd44",
)


def created(post_id: str = "1908234567890123456") -> httpx.Response:
    return httpx.Response(201, json={"data": {"id": post_id, "text": "hello"}})


def publisher(*replies, timeout_seconds: float = 1.0) -> tuple[XPublisher, FakeX]:
    fake = FakeX(*replies)
    return (
        XPublisher(
            CREDENTIALS,
            timeout_seconds=timeout_seconds,
            base_url="https://api.x.com",
            transport=fake.transport(),
        ),
        fake,
    )


def request(content: str = "hello world") -> PublishRequest:
    return PublishRequest(
        account_id="acct_test", candidate_id="cand_1", content=content, idempotency_key="k" * 64
    )


# --- signing ---------------------------------------------------------------------------


def test_signature_matches_the_documented_example():
    params = {
        **EXAMPLE["params"],
        "oauth_consumer_key": EXAMPLE["consumer_key"],
        "oauth_nonce": EXAMPLE["nonce"],
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(EXAMPLE["timestamp"]),
        "oauth_token": EXAMPLE["token"],
        "oauth_version": "1.0",
    }
    base = signature_base_string("post", EXAMPLE["url"], params)
    assert base == EXAMPLE["base"]
    assert sign(base, EXAMPLE["consumer_secret"], EXAMPLE["token_secret"]) == EXAMPLE["signature"]


def test_percent_encoding_and_base_url_normalization():
    assert percent_encode("Ladies + Gentlemen") == "Ladies%20%2B%20Gentlemen"
    assert percent_encode("~-._") == "~-._"
    # Default ports are dropped and the host is lower-cased; query is not signed here.
    assert signature_base_string("GET", "https://API.X.com:443/2/tweets?a=b", {}) == (
        "GET&https%3A%2F%2Fapi.x.com%2F2%2Ftweets&"
    )


def test_authorization_header_carries_the_oauth_fields_only():
    header = authorization_header(
        "POST", "https://api.x.com/2/tweets", CREDENTIALS, nonce="n" * 32, timestamp=1700000000
    )
    assert header.startswith("OAuth ")
    assert 'oauth_signature_method="HMAC-SHA1"' in header
    assert 'oauth_version="1.0"' in header
    assert "oauth_signature=" in header
    # The secrets themselves are never in the header; only the key and token are.
    for secret in ("publish-secret-SECRET-bb22", "publish-token-secret-SECRET-dd44"):
        assert secret not in header


def test_missing_credentials_name_the_variables_not_the_values():
    with pytest.raises(ConfigurationError) as exc:
        OAuth1Credentials.from_values(
            consumer_key=SecretStr("k"),
            consumer_secret=SecretStr("  "),
            token=None,
            token_secret=SecretStr("s"),
        )
    message = str(exc.value)
    assert "X_PUBLISH_API_SECRET" in message and "X_PUBLISH_ACCESS_TOKEN" in message
    assert "X_PUBLISH_API_KEY" not in message


def test_credentials_never_appear_in_reprs():
    assert "SECRET" not in repr(CREDENTIALS)
    client, _ = publisher(created())
    assert "SECRET" not in repr(client)
    client.close()


# --- outcome classification ------------------------------------------------------------


def test_successful_create_returns_the_post_id_and_url():
    client, fake = publisher(created("1908000000000000001"))
    result = client.publish(request())

    assert result.provider_post_id == "1908000000000000001"
    assert result.provider_post_url == "https://x.com/i/web/status/1908000000000000001"
    assert result.http_status == 201
    sent = fake.requests[0]
    assert sent.method == "POST" and sent.url.path == "/2/tweets"
    assert sent.read() == b'{"text":"hello world"}'
    assert sent.headers["authorization"].startswith("OAuth ")
    client.close()


@pytest.mark.parametrize(
    ("reply", "category"),
    [
        (error(401, {"title": "Unauthorized"}), PublishFailureCategory.AUTH),
        (error(403, {"detail": "not permitted"}), PublishFailureCategory.AUTH),
        (
            error(403, {"detail": "You are not allowed to create a Tweet with duplicate content."}),
            PublishFailureCategory.DUPLICATE_CONTENT,
        ),
        (error(400, {"title": "Invalid Request"}), PublishFailureCategory.BAD_REQUEST),
        (error(404, {"title": "Not Found"}), PublishFailureCategory.BAD_REQUEST),
    ],
)
def test_definite_rejections_are_not_retryable(reply, category):
    client, _ = publisher(reply)
    with pytest.raises(PublishRejectedError) as exc:
        client.publish(request())
    assert exc.value.failure_category is category
    client.close()


def test_rate_limit_records_the_reset_and_does_not_sleep():
    client, _ = publisher(
        error(429, {"title": "Too Many Requests"}, **{"x-rate-limit-reset": str(RESET_EPOCH)})
    )
    with pytest.raises(PublishRejectedError) as exc:
        client.publish(request())
    assert exc.value.failure_category is PublishFailureCategory.RATE_LIMITED
    assert exc.value.rate_limit_reset_at is not None
    assert str(RESET_EPOCH) not in str(exc.value)  # reported as a timestamp, not an epoch
    client.close()


@pytest.mark.parametrize(
    ("reply", "category"),
    [
        (error(500, {"title": "Internal Error"}), PublishFailureCategory.SERVER_ERROR),
        (error(503, {"title": "Service Unavailable"}), PublishFailureCategory.SERVER_ERROR),
        (httpx.Response(201, json={"data": {}}), PublishFailureCategory.MALFORMED_RESPONSE),
        (httpx.Response(201, content=b"not json"), PublishFailureCategory.MALFORMED_RESPONSE),
        (httpx.ReadTimeout("timed out"), PublishFailureCategory.TIMEOUT),
        (httpx.WriteTimeout("timed out"), PublishFailureCategory.TIMEOUT),
        (httpx.RemoteProtocolError("server disconnected"), PublishFailureCategory.TRANSPORT_ERROR),
    ],
)
def test_outcomes_that_may_have_created_a_post_are_unknown(reply, category):
    client, _ = publisher(reply)
    with pytest.raises(PublishOutcomeUnknownError) as exc:
        client.publish(request())
    assert exc.value.failure_category is category
    assert "may exist" in str(exc.value)
    client.close()


@pytest.mark.parametrize(
    "reply",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.PoolTimeout("pool timed out"),
    ],
)
def test_failures_before_the_request_was_sent_are_not_sent(reply):
    client, _ = publisher(reply)
    with pytest.raises(PublishNotSentError) as exc:
        client.publish(request())
    assert exc.value.failure_category is PublishFailureCategory.NOT_SENT
    client.close()


def test_publish_errors_never_carry_credentials_or_a_chained_request():
    client, _ = publisher(httpx.ReadTimeout("timed out"))
    with pytest.raises(PublishOutcomeUnknownError) as exc:
        client.publish(request())
    error_text = f"{exc.value} {exc.value.__context__} {exc.value.__cause__} {exc.value!r}"
    for secret in SECRET_VALUES:
        assert secret not in error_text
    assert "OAuth" not in error_text and exc.value.__cause__ is None
    client.close()


# --- factory ---------------------------------------------------------------------------


def test_publisher_factory_defaults_to_the_mock_and_never_falls_back():
    mock = build_publisher(AppSettings(_env_file=None))
    assert mock.provider_name == "mock_publisher"
    with pytest.raises(ConfigurationError):
        build_publisher(AppSettings(_env_file=None, publisher_provider="x"))


def test_publishing_never_uses_the_research_bearer_token():
    settings = AppSettings(
        _env_file=None,
        publisher_provider="x",
        x_bearer_token=SecretStr("research-only-SECRET"),
        x_publish_api_key=SecretStr("k"),
        x_publish_api_secret=SecretStr("s"),
        x_publish_access_token=SecretStr("t"),
        x_publish_access_token_secret=SecretStr("ts"),
    )
    fake = FakeX(created())
    client = XPublisher.from_settings(settings, transport=fake.transport())
    client.publish(request())
    header = fake.requests[0].headers["authorization"]
    assert header.startswith("OAuth ") and "research-only-SECRET" not in header
    assert "Bearer" not in header
    client.close()


def test_lease_must_outlive_the_publish_timeout():
    with pytest.raises(ValueError, match="PUBLISHER_LEASE_SECONDS"):
        AppSettings(_env_file=None, x_publish_timeout_seconds=30, publisher_lease_seconds=30)
