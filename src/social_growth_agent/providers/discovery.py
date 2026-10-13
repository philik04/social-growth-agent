"""Discovery source providers (Phase 9): retrieval only, one bounded request per call.

- ``FeedProvider``: the publisher's own RSS/Atom feed (conditional GET, size cap, no
  redirects off the registry's host, no private addresses).
- ``GitHubReleasesProvider``: GitHub REST ``GET /repos/{owner}/{repo}/releases``.
- ``HNSearchProvider``: the Algolia Hacker News search API.
- ``XDiscoveryProvider``: the existing ``SocialResearchProvider`` (Phase 3 X recent
  search, or its mock); no second X client exists.

No provider paginates, retries, sleeps or follows links found in content. Errors are
``ProviderError``s with a category and a short message that never contains response
bodies, credentials or headers. Everything returned is untrusted text, sanitized here.
"""

import ipaddress
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from social_growth_agent import __version__
from social_growth_agent.discovery.feeds import FeedParseError, describe_empty, parse_feed
from social_growth_agent.discovery.text import (
    MAX_TITLE_CHARS,
    canonical_url,
    host_of,
    sanitize_text,
)
from social_growth_agent.errors import (
    ProviderError,
    ProviderTimeoutError,
    RateLimitedError,
    TransientProviderError,
)
from social_growth_agent.models import ProviderErrorCategory, ResearchQuery
from social_growth_agent.models.discovery import (
    DiscoveredItem,
    DiscoveryLimits,
    SourceKind,
    SourceSpec,
)
from social_growth_agent.providers.social import SocialResearchProvider

USER_AGENT = f"social-growth-agent/{__version__} (content discovery; read-only)"
GITHUB_API = "https://api.github.com"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
MAX_BODY_BYTES = 2_000_000
MAX_REDIRECTS = 3


@dataclass(frozen=True)
class Conditional:
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class FetchResult:
    items: list[DiscoveredItem]
    requests_made: int
    post_reads: int = 0
    user_reads: int = 0
    http_status: int | None = None
    not_modified: bool = False
    validators: Conditional = field(default_factory=Conditional)
    latency_ms: float = 0.0
    request: dict[str, str | int | None] = field(default_factory=dict)
    note: str | None = None
    """A short diagnostic for the fetch ledger (e.g. why a feed was empty)."""


class DiscoverySourceProvider(Protocol):
    provider_name: str
    synthetic: bool

    def fetch(
        self, spec: SourceSpec, limits: DiscoveryLimits, *, conditional: Conditional | None = None
    ) -> FetchResult: ...


class _Http:
    """GET with a size cap and safe errors. Redirects are followed only within the
    same host, and never to an IP literal in a private range."""

    def __init__(self, timeout_seconds: float, transport: httpx.BaseTransport | None) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[httpx.Response, bytes, float]:
        _refuse_private(url)
        origin = host_of(url)
        started = time.perf_counter()
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            response, body = self._once(current, params, headers or {})
            if response.status_code in (301, 302, 303, 307, 308):
                target = urljoin(current, response.headers.get("location", ""))
                if host_of(target) != origin or not target.startswith("https://"):
                    raise ProviderError(
                        "redirect to another host refused",
                        category=ProviderErrorCategory.UNEXPECTED_STATUS,
                    )
                _refuse_private(target)
                current, params = target, None
                continue
            return response, body, (time.perf_counter() - started) * 1000
        raise ProviderError("too many redirects", category=ProviderErrorCategory.UNEXPECTED_STATUS)

    def _once(
        self, url: str, params: dict[str, str] | None, headers: dict[str, str]
    ) -> tuple[httpx.Response, bytes]:
        failure: ProviderError | None = None
        try:
            with self._client.stream("GET", url, params=params, headers=headers) as response:
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BODY_BYTES:
                        raise ProviderError(
                            f"response larger than {MAX_BODY_BYTES} bytes",
                            category=ProviderErrorCategory.MALFORMED_RESPONSE,
                        )
                    chunks.append(chunk)
                return response, b"".join(chunks)
        except httpx.TimeoutException:
            failure = ProviderTimeoutError(
                "request timed out", category=ProviderErrorCategory.TIMEOUT
            )
        except httpx.TransportError as exc:
            failure = TransientProviderError(
                f"network error ({type(exc).__name__})", category=ProviderErrorCategory.NETWORK
            )
        raise failure


def _refuse_private(url: str) -> None:
    host = host_of(url) or ""
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".internal"):
            raise ProviderError(
                "local hosts are never fetched", category=ProviderErrorCategory.BAD_REQUEST
            ) from None
        return
    if not address.is_global:
        raise ProviderError(
            "private addresses are never fetched", category=ProviderErrorCategory.BAD_REQUEST
        )


def _status_error(response: httpx.Response, what: str) -> ProviderError:
    status = response.status_code
    if status == 429:
        return RateLimitedError(f"{what}: rate limited (HTTP 429)", reset_at=_reset(response))
    if status in (401, 403):
        if response.headers.get("x-ratelimit-remaining") == "0":
            return RateLimitedError(
                f"{what}: rate limited (HTTP {status})", reset_at=_reset(response)
            )
        return ProviderError(f"{what}: HTTP {status}", category=ProviderErrorCategory.AUTH)
    if status >= 500:
        return TransientProviderError(
            f"{what}: HTTP {status}", category=ProviderErrorCategory.SERVER_ERROR
        )
    if 400 <= status < 500:
        return ProviderError(f"{what}: HTTP {status}", category=ProviderErrorCategory.BAD_REQUEST)
    return ProviderError(f"{what}: HTTP {status}", category=ProviderErrorCategory.UNEXPECTED_STATUS)


def _reset(response: httpx.Response) -> datetime | None:
    raw = response.headers.get("x-ratelimit-reset")
    if raw and raw.isdigit():
        return datetime.fromtimestamp(int(raw), tz=UTC)
    return None


def _item(spec: SourceSpec, **values: Any) -> DiscoveredItem:
    return DiscoveredItem(
        source_id=spec.id,
        kind=spec.kind,
        category=spec.category,
        evidence_type=spec.evidence_type,
        tier=spec.tier,
        publisher=spec.publisher,
        evergreen=spec.evergreen,
        **values,
    )


class FeedProvider:
    provider_name = "feed"
    synthetic = False

    def __init__(
        self, *, timeout_seconds: float = 10.0, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._http = _Http(timeout_seconds, transport)

    def close(self) -> None:
        self._http.close()

    def fetch(
        self, spec: SourceSpec, limits: DiscoveryLimits, *, conditional: Conditional | None = None
    ) -> FetchResult:
        assert spec.kind is SourceKind.FEED and spec.url
        headers: dict[str, str] = {
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9"
        }
        if conditional and conditional.etag:
            headers["If-None-Match"] = conditional.etag
        if conditional and conditional.last_modified:
            headers["If-Modified-Since"] = conditional.last_modified
        response, body, latency = self._http.get(spec.url, headers=headers)
        request: dict[str, str | int | None] = {"url": spec.url, "max_items": limits.items_per_feed}
        validators = Conditional(
            etag=response.headers.get("etag"), last_modified=response.headers.get("last-modified")
        )
        if response.status_code == 304:
            return FetchResult(
                items=[],
                requests_made=1,
                http_status=304,
                not_modified=True,
                validators=conditional or validators,
                latency_ms=latency,
                request=request,
            )
        if response.status_code != 200:
            raise _status_error(response, f"feed {spec.id}")
        try:
            entries = parse_feed(body, max_items=limits.items_per_feed)
        except FeedParseError as exc:
            raise ProviderError(
                f"feed {spec.id}: {exc}", category=ProviderErrorCategory.MALFORMED_RESPONSE
            ) from None
        items = [
            _item(
                spec,
                platform="feed",
                provider_item_id=e.entry_id,
                url=e.url,
                title=e.title,
                text=e.summary or e.title,
                author=e.author or spec.publisher,
                published_at=e.published_at,
            )
            for e in entries
        ]
        return FetchResult(
            items=items,
            requests_made=1,
            http_status=200,
            validators=validators,
            latency_ms=latency,
            request=request,
            note=None if items else describe_empty(body, response.headers.get("content-type")),
        )


class _RawRelease(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    html_url: str
    tag_name: str
    name: str | None = None
    body: str | None = None
    draft: bool = False
    prerelease: bool = False
    published_at: datetime | None = None


class GitHubReleasesProvider:
    provider_name = "github"
    synthetic = False

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        base_url: str = GITHUB_API,
    ) -> None:
        self._http = _Http(timeout_seconds, transport)
        self._base = base_url.rstrip("/")

    def close(self) -> None:
        self._http.close()

    def fetch(
        self, spec: SourceSpec, limits: DiscoveryLimits, *, conditional: Conditional | None = None
    ) -> FetchResult:
        assert spec.kind is SourceKind.GITHUB_RELEASES and spec.repo
        per_page = min(limits.items_per_feed, 10)
        url = f"{self._base}/repos/{spec.repo}/releases"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if conditional and conditional.etag:
            headers["If-None-Match"] = conditional.etag
        response, body, latency = self._http.get(
            url, params={"per_page": str(per_page)}, headers=headers
        )
        request: dict[str, str | int | None] = {"repo": spec.repo, "per_page": per_page}
        if response.status_code == 304:
            return FetchResult(
                items=[],
                requests_made=1,
                http_status=304,
                not_modified=True,
                validators=conditional or Conditional(),
                latency_ms=latency,
                request=request,
            )
        if response.status_code != 200:
            raise _status_error(response, f"github {spec.repo}")
        try:
            raw = httpx.Response(200, content=body).json()
            releases = (
                [_RawRelease.model_validate(r) for r in raw] if isinstance(raw, list) else None
            )
        except (ValueError, ValidationError):
            releases = None
        if releases is None:
            raise ProviderError(
                f"github {spec.repo}: unexpected response shape",
                category=ProviderErrorCategory.MALFORMED_RESPONSE,
            )
        items = []
        for release in releases[:per_page]:
            if release.draft:
                continue
            title = sanitize_text(
                f"{spec.repo} {release.name or release.tag_name}"
                + (" (pre-release)" if release.prerelease else ""),
                limit=MAX_TITLE_CHARS,
            )
            items.append(
                _item(
                    spec,
                    platform="github",
                    provider_item_id=str(release.id),
                    url=canonical_url(release.html_url),
                    title=title,
                    text=sanitize_text(release.body) or title,
                    author=spec.publisher,
                    published_at=release.published_at,
                )
            )
        return FetchResult(
            items=items,
            requests_made=1,
            http_status=200,
            validators=Conditional(etag=response.headers.get("etag")),
            latency_ms=latency,
            request=request,
        )


class _RawHit(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    objectID: str  # noqa: N815 - the API's field name
    title: str | None = None
    url: str | None = None
    author: str | None = None
    created_at: datetime | None = None
    points: int | None = None
    num_comments: int | None = None
    story_text: str | None = None


class HNSearchProvider:
    provider_name = "hn_algolia"
    synthetic = False

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        url: str = HN_SEARCH_URL,
    ) -> None:
        self._http = _Http(timeout_seconds, transport)
        self._url = url

    def close(self) -> None:
        self._http.close()

    def fetch(
        self, spec: SourceSpec, limits: DiscoveryLimits, *, conditional: Conditional | None = None
    ) -> FetchResult:
        assert spec.kind is SourceKind.HN_SEARCH and spec.query
        hits_per_page = min(limits.items_per_feed, 20)
        params = {"query": spec.query, "tags": "story", "hitsPerPage": str(hits_per_page)}
        response, body, latency = self._http.get(self._url, params=params)
        request: dict[str, str | int | None] = {"query": spec.query, "hits": hits_per_page}
        if response.status_code != 200:
            raise _status_error(response, "hacker news search")
        try:
            raw = httpx.Response(200, content=body).json()
            hits = (
                [_RawHit.model_validate(h) for h in raw.get("hits", [])]
                if isinstance(raw, dict)
                else None
            )
        except (ValueError, ValidationError):
            hits = None
        if hits is None:
            raise ProviderError(
                "hacker news search: unexpected response shape",
                category=ProviderErrorCategory.MALFORMED_RESPONSE,
            )
        items = []
        for hit in hits[:hits_per_page]:
            title = sanitize_text(hit.title, limit=MAX_TITLE_CHARS)
            if not title:
                continue
            thread = f"https://news.ycombinator.com/item?id={hit.objectID}"
            items.append(
                _item(
                    spec,
                    platform="hn",
                    provider_item_id=hit.objectID,
                    url=canonical_url(hit.url) or thread,
                    title=title,
                    text=sanitize_text(hit.story_text) or title,
                    author=sanitize_text(hit.author, limit=40) or None,
                    published_at=hit.created_at,
                    points=hit.points,
                    comments=hit.num_comments,
                )
            )
        return FetchResult(
            items=items, requests_made=1, http_status=200, latency_ms=latency, request=request
        )


class XDiscoveryProvider:
    """X sources through the existing research provider. One search per call; the
    provider's own caps (results per request) still apply."""

    def __init__(self, research: SocialResearchProvider) -> None:
        self._research = research
        self.provider_name = research.source_name
        self.synthetic = research.synthetic

    def fetch(
        self, spec: SourceSpec, limits: DiscoveryLimits, *, conditional: Conditional | None = None
    ) -> FetchResult:
        if spec.kind is SourceKind.X_ACCOUNTS:
            query = ResearchQuery(
                text=spec.query or "-is:reply",
                authors=list(spec.handles),
                max_results=limits.posts_per_x_request,
            )
        else:
            assert spec.query
            query = ResearchQuery(text=spec.query, max_results=limits.posts_per_x_request)
        result = self._research.search(query)
        fetch = result.fetch
        items = [
            _item(
                spec,
                platform="x",
                provider_item_id=post.source_id.removeprefix("x_"),
                url=f"https://x.com/i/web/status/{post.source_id.removeprefix('x_')}",
                title=None,
                text=sanitize_text(post.text),
                author=post.author_username,
                author_id=post.author_id,
                published_at=post.created_at,
                likes=post.likes,
                reposts=post.reposts,
                replies=post.replies,
                quotes=post.quotes,
                impressions=post.impressions,
            )
            for post in result.posts
        ]
        return FetchResult(
            items=items,
            requests_made=fetch.requests_made,
            post_reads=fetch.posts_fetched,
            user_reads=fetch.users_fetched,
            http_status=200 if fetch.requests_made else None,
            latency_ms=fetch.latency_ms,
            request={
                "query": fetch.query,
                "effective_query": fetch.effective_query,
                "max_results": fetch.max_results,
            },
        )


ProviderFactory = Callable[[SourceKind], DiscoverySourceProvider | None]


class LiveDiscoveryProviders:
    """``ProviderFactory`` for ``DISCOVERY_SOURCES=live``: real feeds, GitHub and Hacker
    News; X sources through the configured research provider (mock or X)."""

    def __init__(
        self,
        research: SocialResearchProvider,
        *,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """``transport`` replaces the network (tests only)."""
        x = XDiscoveryProvider(research)
        timeout = timeout_seconds
        self._by_kind: dict[SourceKind, DiscoverySourceProvider] = {
            SourceKind.FEED: FeedProvider(timeout_seconds=timeout, transport=transport),
            SourceKind.GITHUB_RELEASES: GitHubReleasesProvider(
                timeout_seconds=timeout, transport=transport
            ),
            SourceKind.HN_SEARCH: HNSearchProvider(timeout_seconds=timeout, transport=transport),
            SourceKind.X_SEARCH: x,
            SourceKind.X_ACCOUNTS: x,
        }

    def __call__(self, kind: SourceKind) -> DiscoverySourceProvider | None:
        return self._by_kind.get(kind)
