"""User-authorized primary article GET: pinned public DNS, HTTPS, no credentials/retries."""

import ipaddress
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from social_growth_agent.discovery.article import ArticleEvidence, extract_article
from social_growth_agent.errors import (
    InvalidDiscoveryRequestError,
    ProviderError,
    SocialGrowthError,
)

MAX_ARTICLE_BYTES = 2_000_000
MAX_ARTICLE_REDIRECTS = 3
MAX_FETCH_SECONDS = 30
USER_AGENT = "social-growth-agent"


class ArticleReadError(ProviderError):
    def __init__(self, message: str, requests: int, status: int | None) -> None:
        super().__init__(message)
        self.requests_made = requests
        self.http_status = status


def public_addresses(host: str) -> list[str]:
    try:
        return list(
            dict.fromkeys(
                str(r[4][0]) for r in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            )
        )
    except OSError:
        raise InvalidDiscoveryRequestError("article hostname could not be resolved") from None


def validate_article_url(url: str, allowed_host: str) -> str:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise InvalidDiscoveryRequestError("invalid article port") from None
    if (
        parts.scheme != "https"
        or parts.hostname != allowed_host
        or parts.username
        or parts.password
        or port not in (None, 443)
        or parts.fragment
    ):
        raise InvalidDiscoveryRequestError("article must use HTTPS on the exact authorized host")
    if (
        allowed_host in {"localhost"}
        or "." not in allowed_host
        or allowed_host.endswith((".localhost", ".internal", ".local"))
    ):
        raise InvalidDiscoveryRequestError("unsafe article hostname")
    return allowed_host


@dataclass(frozen=True)
class ArticleFetch:
    evidence: ArticleEvidence
    final_url: str
    requests_made: int
    http_status: int


class PrimaryArticleProvider:
    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        resolver: Callable[[str], list[str]] = public_addresses,
    ) -> None:
        self.timeout = timeout_seconds
        self.transport = transport
        self.resolver = resolver

    def fetch(self, url: str, selected: list[str] | None = None) -> ArticleFetch:
        host = urlsplit(url).hostname or ""
        current = url
        failure: ProviderError | None = None
        requests = 0
        status = None
        started = time.monotonic()
        try:
            with httpx.Client(
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
                headers={
                    "User-Agent": "social-growth-agent/0.1 (explicit primary evidence read)",
                    "Accept": "text/html",
                },
            ) as client:
                validate_article_url(url, host)
                addresses = self.resolver(host)
                if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
                    raise InvalidDiscoveryRequestError("article DNS resolved to an unsafe address")
                robots_url = httpx.URL(url).copy_with(
                    host=addresses[0], path="/robots.txt", query=None
                )
                requests += 1
                with client.stream(
                    "GET", robots_url, headers={"Host": host}, extensions={"sni_hostname": host}
                ) as robots_response:
                    status = robots_response.status_code
                    if status == 200:
                        robots_body = bytearray()
                        for chunk in robots_response.iter_bytes():
                            if time.monotonic() - started > MAX_FETCH_SECONDS:
                                raise InvalidDiscoveryRequestError("article time budget exceeded")
                            if len(robots_body) + len(chunk) > 64_000:
                                raise InvalidDiscoveryRequestError(
                                    "robots policy exceeds size limit"
                                )
                            robots_body.extend(chunk)
                        robots = RobotFileParser()
                        robots.parse(robots_body.decode("utf-8").splitlines())
                    elif status in (404, 410):
                        robots = RobotFileParser()
                        robots.parse([])
                    else:
                        raise InvalidDiscoveryRequestError(
                            "robots access policy unavailable or refused"
                        )
                for _attempt in range(MAX_ARTICLE_REDIRECTS + 1):
                    if time.monotonic() - started > MAX_FETCH_SECONDS:
                        raise InvalidDiscoveryRequestError("article time budget exceeded")
                    validate_article_url(current, host)
                    if not robots.can_fetch(USER_AGENT, current):
                        raise InvalidDiscoveryRequestError(
                            "article access disallowed by robots policy"
                        )
                    addresses = self.resolver(host)
                    if not addresses or any(
                        not ipaddress.ip_address(a).is_global for a in addresses
                    ):
                        raise InvalidDiscoveryRequestError(
                            "article DNS resolved to an unsafe address"
                        )
                    # Pin the connection to the validated IP; TLS still verifies the original
                    # hostname via SNI. Disabling environment proxies prevents alternate routing.
                    pinned = httpx.URL(current).copy_with(host=addresses[0])
                    requests += 1
                    with client.stream(
                        "GET",
                        pinned,
                        headers={"Host": host},
                        extensions={"sni_hostname": host},
                    ) as response:
                        status = response.status_code
                        if status in {301, 302, 303, 307, 308}:
                            target = urljoin(current, response.headers.get("location", ""))
                            validate_article_url(target, host)
                            current = target
                            continue
                        if status != 200:
                            raise ProviderError(f"article fetch refused (HTTP {status}); no retry")
                        if "text/html" not in response.headers.get("content-type", "").lower():
                            raise InvalidDiscoveryRequestError("article response is not HTML")
                        restrictions = response.headers.get("x-robots-tag", "").lower()
                        if any(v in restrictions for v in ("noarchive", "nosnippet")):
                            raise InvalidDiscoveryRequestError("source disallows evidence storage")
                        data = bytearray()
                        for chunk in response.iter_bytes():
                            if time.monotonic() - started > MAX_FETCH_SECONDS:
                                raise InvalidDiscoveryRequestError("article time budget exceeded")
                            if len(data) + len(chunk) > MAX_ARTICLE_BYTES:
                                raise InvalidDiscoveryRequestError(
                                    "article exceeds response-size limit"
                                )
                            data.extend(chunk)
                        evidence = extract_article(bytes(data), selected)
                        return ArticleFetch(evidence, current, requests, status)
                raise InvalidDiscoveryRequestError("article redirect limit exceeded")
        except httpx.HTTPError:
            failure = ProviderError("article network/timeout/incomplete fetch; no evidence stored")
        except UnicodeError:
            failure = ProviderError("article encoding invalid; no evidence stored")
        except (SocialGrowthError, ValueError) as exc:
            failure = ProviderError(str(exc))
        raise ArticleReadError(str(failure), requests, status)
