import hashlib

import httpx
import pytest

from social_growth_agent.discovery.article import extract_article
from social_growth_agent.errors import InvalidDiscoveryRequestError
from social_growth_agent.policies.grounding import (
    grounding_from,
    unsupported_numbers,
    unsupported_urls,
)
from social_growth_agent.providers.article import ArticleReadError, PrimaryArticleProvider
from tests.enrichment_helpers import FIRST, HTML, SECOND, THIRD, URL, article_provider


def test_literal_excerpts_attribution_dates_digest_and_excluded_ui():
    evidence = extract_article(HTML)
    assert evidence.author == "Vendor Labs"
    assert evidence.published_at.isoformat() == "2026-10-13T10:00:00+00:00"
    assert evidence.response_sha256 == hashlib.sha256(HTML).hexdigest()
    assert FIRST in evidence.excerpts and SECOND in evidence.excerpts
    assert THIRD in evidence.excerpts
    text = evidence.evidence_text("Host")
    assert "Self-reported" in text and "not independently verified" in text
    assert "9999" not in text and "Ignore previous" not in text
    assert len(text) <= 4000
    comments = HTML.replace(
        b"</body>",
        b'<div class="prose"><p>The model reaches 9999 tokens/s, '
        b"claims an unrelated commenter.</p></div></body>",
    )
    assert "9999" not in extract_article(comments).evidence_text("Host")


def test_unseen_claims_cannot_be_submitted_as_excerpts_or_grounded_numbers():
    with pytest.raises(InvalidDiscoveryRequestError, match="literal"):
        extract_article(HTML, [FIRST, "The model is independently proven to run at 9999 tokens/s."])
    evidence = extract_article(HTML)
    grounding = grounding_from([evidence.evidence_text("Host")], [URL])
    assert unsupported_numbers("9999 tokens/s", grounding) == ["9999"]
    assert unsupported_numbers("32K context", grounding) == []
    assert unsupported_urls(URL, grounding) == []
    assert unsupported_urls("https://example.org/fabricated", grounding)


@pytest.mark.parametrize(
    "body",
    [
        HTML[:-14],
        b"<html><article><h1>Title only</h1></article></html>",
        HTML.replace(b"<article>", b"<div>").replace(b"</article>", b"</div>"),
    ],
)
def test_incomplete_or_insufficient_evidence_is_rejected(body):
    with pytest.raises(InvalidDiscoveryRequestError):
        extract_article(body)


def test_transport_is_pinned_with_original_tls_name_and_no_credentials():
    requests = []
    result = article_provider(requests).fetch(URL)
    assert result.requests_made == 2  # robots policy plus the article
    assert all(r.url.host == "93.184.216.34" for r in requests)
    assert all(r.headers["host"] == "huggingface.co" for r in requests)
    assert all(r.extensions["sni_hostname"] == "huggingface.co" for r in requests)
    assert all("authorization" not in r.headers for r in requests)
    assert result.final_url == URL


@pytest.mark.parametrize(
    "url",
    [
        "http://huggingface.co/blog/x",
        "https://localhost/x",
        "https://user:secret@huggingface.co/x",
        "https://huggingface.co:444/x",
        "https://huggingface.co/x#fragment",
    ],
)
def test_unsafe_urls_never_reach_transport(url):
    requests = []
    with pytest.raises(ArticleReadError):
        article_provider(requests).fetch(url)
    assert requests == []


@pytest.mark.parametrize(
    "addresses", [["127.0.0.1"], ["10.0.0.1"], ["::1"], ["93.184.216.34", "169.254.169.254"], []]
)
def test_private_or_mixed_dns_is_refused(addresses):
    requests = []
    provider = article_provider(requests)
    provider.resolver = lambda host: addresses
    with pytest.raises(ArticleReadError, match="unsafe"):
        provider.fetch(URL)
    assert requests == []


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/x",
        "http://huggingface.co/x",
        "https://127.0.0.1/x",
        "https://secret@huggingface.co/x",
    ],
)
def test_unsafe_redirects_are_refused_before_following(target):
    requests = []
    provider = article_provider(
        requests, handler=lambda r: httpx.Response(302, headers={"location": target})
    )
    with pytest.raises(ArticleReadError):
        provider.fetch(URL)
    assert len(requests) == 2


def test_redirects_are_bounded_and_resolve_again_to_prevent_rebinding():
    requests = []
    provider = article_provider(
        requests, handler=lambda r: httpx.Response(302, headers={"location": "/again"})
    )
    with pytest.raises(ArticleReadError, match="redirect limit"):
        provider.fetch(URL)
    assert len(requests) == 5  # one robots read and four article attempts
    provider = article_provider([])
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])
    provider.resolver = lambda host: next(answers)
    with pytest.raises(ArticleReadError, match="unsafe"):
        provider.fetch(URL)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_access_failures_are_not_retried(status):
    requests = []
    with pytest.raises(ArticleReadError) as error:
        article_provider(requests, status=status).fetch(URL)
    assert error.value.http_status == status and error.value.requests_made == 2
    assert len(requests) == 2


def test_response_size_type_storage_opt_out_and_network_failure():
    for kwargs in [
        {"body": b"x" * 2_000_001},
        {"headers": {"content-type": "application/json"}},
        {"headers": {"content-type": "text/html", "x-robots-tag": "noarchive"}},
        {"body": HTML.replace(b"</head>", b'<meta name="robots" content="nosnippet"></head>')},
    ]:
        with pytest.raises(ArticleReadError):
            article_provider([], **kwargs).fetch(URL)

    def failing(request):
        raise httpx.ReadTimeout("private transport diagnostic", request=request)

    with pytest.raises(ArticleReadError) as error:
        article_provider([], handler=failing).fetch(URL)
    assert "private transport diagnostic" not in str(error.value)
    assert error.value.__context__ is None


def test_robots_disallow_blocks_article_fetch():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text="User-agent: *\nDisallow: /blog/")

    provider = PrimaryArticleProvider(
        transport=httpx.MockTransport(handler), resolver=lambda h: ["93.184.216.34"]
    )
    with pytest.raises(ArticleReadError, match="disallowed"):
        provider.fetch(URL)
    assert len(requests) == 1


def test_elapsed_budget_and_dns_failure_stop_without_retry(monkeypatch):
    requests = []
    ticks = iter([0.0, 31.0])
    monkeypatch.setattr("social_growth_agent.providers.article.time.monotonic", lambda: next(ticks))
    with pytest.raises(ArticleReadError, match="time budget"):
        article_provider(requests).fetch(URL)
    assert len(requests) == 1


def test_redirect_within_authorized_host_keeps_attributed_final_url():
    requests = []

    def handler(request):
        if request.url.path.endswith("local-agents"):
            return httpx.Response(302, headers={"location": "/blog/vendor/article"})
        return httpx.Response(200, content=HTML, headers={"content-type": "text/html"})

    result = article_provider(requests, handler=handler).fetch(URL)
    assert result.final_url == "https://huggingface.co/blog/vendor/article"
    assert len(requests) == 3
