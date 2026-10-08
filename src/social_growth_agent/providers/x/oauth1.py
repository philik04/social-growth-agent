"""OAuth 1.0a request signing (HMAC-SHA1), user context, on the standard library.

Implements RFC 5849 section 3.4 as X documents it ("Creating a signature"). Used only
for publishing: the four credentials are app consumer key/secret plus the posting
account's access token/secret. They live in ``OAuth1Credentials`` (``SecretStr``) and
are revealed only while a signature is computed; the header this returns must never be
logged or stored.

Only query parameters (and form-encoded body parameters, which X v2 does not use) are
signed. A JSON body, as sent to ``POST /2/tweets``, is not part of the signature.
"""

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import SecretStr

from social_growth_agent.errors import ConfigurationError


@dataclass(frozen=True)
class OAuth1Credentials:
    consumer_key: SecretStr
    consumer_secret: SecretStr
    token: SecretStr
    token_secret: SecretStr

    def __repr__(self) -> str:
        return "OAuth1Credentials(****)"

    @classmethod
    def from_values(cls, **values: SecretStr | None) -> "OAuth1Credentials":
        """Build from settings; names the missing *variables* (never values) on error."""
        env = {
            "consumer_key": "X_PUBLISH_API_KEY",
            "consumer_secret": "X_PUBLISH_API_SECRET",
            "token": "X_PUBLISH_ACCESS_TOKEN",
            "token_secret": "X_PUBLISH_ACCESS_TOKEN_SECRET",
        }
        missing = [
            env[name]
            for name in env
            if values.get(name) is None or not _reveal(values[name]).strip()
        ]
        if missing:
            raise ConfigurationError(
                "X publishing needs user-context credentials: " + ", ".join(missing) + " not set"
            )
        return cls(**{name: SecretStr(_reveal(values[name]).strip()) for name in env})


def _reveal(value: SecretStr | None) -> str:
    return "" if value is None else value.get_secret_value()


def percent_encode(value: str) -> str:
    """RFC 3986 encoding: everything except unreserved characters (A-Z a-z 0-9 - . _ ~)."""
    return quote(value, safe="~")


def signature_base_string(method: str, url: str, params: Mapping[str, str]) -> str:
    encoded = sorted((percent_encode(k), percent_encode(v)) for k, v in params.items())
    parameter_string = "&".join(f"{k}={v}" for k, v in encoded)
    return "&".join(
        (method.upper(), percent_encode(_base_url(url)), percent_encode(parameter_string))
    )


def sign(base_string: str, consumer_secret: str, token_secret: str) -> str:
    key = f"{percent_encode(consumer_secret)}&{percent_encode(token_secret)}"
    digest = hmac.new(key.encode(), base_string.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def authorization_header(
    method: str,
    url: str,
    credentials: OAuth1Credentials,
    *,
    params: Mapping[str, str] | None = None,
    nonce: str | None = None,
    timestamp: int | None = None,
) -> str:
    """The ``Authorization: OAuth ...`` value for one request.

    ``params`` are the request's query (or form) parameters. ``nonce`` and
    ``timestamp`` are fixed only by tests that check the documented example.
    """
    oauth = {
        "oauth_consumer_key": credentials.consumer_key.get_secret_value(),
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(timestamp if timestamp is not None else int(time.time())),
        "oauth_token": credentials.token.get_secret_value(),
        "oauth_version": "1.0",
    }
    base = signature_base_string(method, url, {**(params or {}), **oauth})
    oauth["oauth_signature"] = sign(
        base,
        credentials.consumer_secret.get_secret_value(),
        credentials.token_secret.get_secret_value(),
    )
    fields = ", ".join(
        f'{percent_encode(k)}="{percent_encode(v)}"' for k, v in sorted(oauth.items())
    )
    return f"OAuth {fields}"


def _base_url(url: str) -> str:
    """Scheme and host lower-cased, no query or fragment, default ports dropped."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port
    scheme = parts.scheme.lower()
    if port is not None and not (
        (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    ):
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, parts.path, "", ""))
