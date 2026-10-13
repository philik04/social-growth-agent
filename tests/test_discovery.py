"""Content discovery (Phase 9), offline: sanitizing, feeds, providers and caps, claims,
ranking, Grok eligibility, mentions, grounding, pricing and manual-publication helpers."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from social_growth_agent.accounting import PriceList, UsageCounts, estimate_cost
from social_growth_agent.accounting.costs import PublishUsage, VerificationUsage
from social_growth_agent.accounting.pricing import XPrices
from social_growth_agent.discovery.analysis import (
    AnalysisContext,
    AnalysisItem,
    PriorOpportunity,
    RankedOpportunity,
    analyze,
    project_of,
)
from social_growth_agent.discovery.claims import (
    EvidenceFacts,
    claim_hints,
    claim_verification,
    item_verification,
    validate_claim,
)
from social_growth_agent.discovery.feeds import FeedParseError, parse_feed
from social_growth_agent.discovery.registry import load_registry
from social_growth_agent.discovery.text import canonical_url, sanitize_text
from social_growth_agent.errors import (
    InvalidDiscoveryRequestError,
    ProviderError,
    PublishRejectedError,
    RateLimitedError,
)
from social_growth_agent.models import (
    ContentCandidate,
    ContentIntent,
    HookType,
    PlatformPost,
    PostFormat,
    PublishFailureCategory,
    PublishRequest,
    ResearchQuery,
)
from social_growth_agent.models.discovery import (
    ClaimHintKind,
    ClaimKind,
    CredibilityTier,
    DiscoveryLimits,
    DiscoveryPolicy,
    EvidenceType,
    RankingWeights,
    SourceCategory,
    SourceKind,
    SourcePlatform,
    SourceSpec,
    VerificationStatus,
)
from social_growth_agent.policies.content_policy import ContentPolicy, find_violations
from social_growth_agent.policies.grounding import (
    grounding_from,
    unsupported_numbers,
    unsupported_urls,
)
from social_growth_agent.policies.mentions import MANUAL_ONLY_REASON, find_mentions, is_manual_only
from social_growth_agent.providers.discovery import (
    FeedProvider,
    GitHubReleasesProvider,
    HNSearchProvider,
    XDiscoveryProvider,
)
from social_growth_agent.providers.mocks import MockAnalyticsProvider, MockPublisher
from social_growth_agent.providers.mocks.discovery import FixtureWeb, MockDiscoveryProviders
from social_growth_agent.providers.x.analytics import XAnalyticsProvider
from social_growth_agent.providers.x.client import XApiClient
from social_growth_agent.providers.x.query import effective_query
from social_growth_agent.services.discovery import parse_post_ref, text_match
from tests.x_fakes import TOKEN, FakeX, error, ok

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
LIMITS = DiscoveryLimits()


# --- text and feeds ----------------------------------------------------------------------


def test_sanitize_removes_markup_controls_and_invisible_characters():
    raw = "<p>Hello&amp;<b>world</b></p>‮hidden​\x07 " + "x" * 600
    text = sanitize_text(raw)
    assert "<" not in text and "‮" not in text and "​" not in text
    assert text.startswith("Hello& world hidden")
    assert len(text) <= 500


def test_canonical_url_drops_tracking_and_normalizes():
    assert (
        canonical_url("http://www.Example.com/a/?utm_source=x&b=2&a=1#frag")
        == "https://example.com/a?a=1&b=2"
    )
    assert canonical_url("javascript:alert(1)") is None
    assert canonical_url(None) is None


def test_rss_and_atom_feeds_parse_to_bounded_entries():
    rss = (
        b'<?xml version="1.0"?><rss version="2.0"><channel>'
        b"<item><title>A &amp; B</title><link>https://ex.com/a</link>"
        b"<description>&lt;b&gt;bold&lt;/b&gt; text</description>"
        b"<pubDate>Mon, 13 Oct 2026 10:00:00 GMT</pubDate></item>"
        b"<item><title>Second</title><link>https://ex.com/b</link></item>"
        b"</channel></rss>"
    )
    entries = parse_feed(rss, max_items=1)
    assert len(entries) == 1
    assert entries[0].title == "A & B" and entries[0].summary == "bold text"
    assert entries[0].published_at == datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
    atom = (
        b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T</title>'
        b'<link href="https://ex.com/t"/><id>tag:1</id><updated>2026-10-12T08:00:00Z</updated>'
        b"<summary>S</summary></entry></feed>"
    )
    [entry] = parse_feed(atom, max_items=5)
    assert entry.url == "https://ex.com/t" and entry.summary == "S"


def test_feeds_with_entities_or_dtds_are_refused():
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b"<rss><channel><item><title>&x;</title></item></channel></rss>"
    )
    with pytest.raises(FeedParseError):
        parse_feed(xxe, max_items=5)
    with pytest.raises(FeedParseError):
        parse_feed(b"not xml", max_items=5)


def test_the_shipped_registry_loads_and_has_no_curated_handles():
    registry = load_registry(Path("discovery.toml"))
    kinds = {s.kind for s in registry.sources}
    assert kinds == set(SourceKind)
    assert all(s.url is None or s.url.startswith("https://") for s in registry.sources)
    assert next(s for s in registry.sources if s.kind is SourceKind.X_ACCOUNTS).handles == []
    # the domain's generic terms and the shortlist diversity live in the registry
    assert {"agent", "agents"} <= set(registry.generic_terms)
    assert registry.policy.shortlist_size == 5
    assert registry.policy.max_per_project_in_shortlist == 2


def test_registry_validation_rejects_bad_sources():
    with pytest.raises(ValidationError):
        SourceSpec(
            id="f",
            kind=SourceKind.FEED,
            category=SourceCategory.PRIMARY,
            publisher="p",
            tier=CredibilityTier.OFFICIAL,
            evidence_type=EvidenceType.PAPER,
            url="http://insecure.example/feed",
        )
    with pytest.raises(ValidationError):
        SourceSpec(
            id="xx",
            kind=SourceKind.X_ACCOUNTS,
            category=SourceCategory.DISCUSSION,
            publisher="X",
            tier=CredibilityTier.COMMUNITY,
            evidence_type=EvidenceType.DISCUSSION_POST,
            handles=["not a handle!"],
        )
    with pytest.raises(ValidationError):
        DiscoveryLimits(x_requests=6)  # above the hard limit


# --- providers ------------------------------------------------------------------------------


def spec(kind: SourceKind, **values: object) -> SourceSpec:
    base: dict[str, object] = {
        "id": "src-1",
        "kind": kind,
        "category": SourceCategory.PRIMARY,
        "publisher": "Pub",
        "tier": CredibilityTier.OFFICIAL,
        "evidence_type": EvidenceType.RELEASE,
    }
    return SourceSpec.model_validate(base | values)


def test_fixture_providers_make_one_request_each_and_never_paginate():
    web = FixtureWeb(lambda: NOW)
    providers = MockDiscoveryProviders(lambda: NOW, web=web)
    feed = providers(SourceKind.FEED)
    assert feed is not None and feed.synthetic and feed.provider_name == "mock_feed"
    result = feed.fetch(spec(SourceKind.FEED, url="https://rss.arxiv.org/rss/cs.AI"), LIMITS)
    assert result.requests_made == 1 and len(web.requests) == 1
    assert result.items[0].title == "Small models as routers for agentic workflows"
    github = providers(SourceKind.GITHUB_RELEASES)
    assert github is not None
    releases = github.fetch(spec(SourceKind.GITHUB_RELEASES, repo="langchain-ai/langgraph"), LIMITS)
    assert [i.title for i in releases.items] == ["langchain-ai/langgraph 1.1.0"]  # draft skipped
    assert web.requests[-1].url.params["per_page"] == "10"
    hn = providers(SourceKind.HN_SEARCH)
    assert hn is not None
    hits = hn.fetch(spec(SourceKind.HN_SEARCH, query="AI agents"), LIMITS)
    assert web.requests[-1].url.params["hitsPerPage"] == "20"
    assert hits.items[0].comments == 95 and hits.items[0].points == 180
    assert len(web.requests) == 3


def test_conditional_get_returns_not_modified():
    web = FixtureWeb(lambda: NOW)
    provider = FeedProvider(transport=web.transport)
    feed = spec(SourceKind.FEED, url="https://openai.com/news/rss.xml")
    first = provider.fetch(feed, LIMITS)
    second = provider.fetch(feed, LIMITS, conditional=first.validators)
    assert second.not_modified and second.items == [] and second.http_status == 304


@pytest.mark.parametrize(
    ("response", "category"),
    [
        (httpx.Response(401), "auth"),
        (httpx.Response(404), "bad_request"),
        (httpx.Response(503, text="secret body"), "server_error"),
    ],
)
def test_provider_errors_are_categorized_without_the_body(response, category):
    provider = GitHubReleasesProvider(transport=httpx.MockTransport(lambda _: response))
    with pytest.raises(ProviderError) as info:
        provider.fetch(spec(SourceKind.GITHUB_RELEASES, repo="a/b"), LIMITS)
    assert info.value.category is not None and info.value.category.value == category
    assert "secret body" not in str(info.value)


def test_rate_limits_and_redirects_off_host_are_refused():
    limited = httpx.Response(429, headers={"x-ratelimit-reset": "1791116100"})
    provider = HNSearchProvider(transport=httpx.MockTransport(lambda _: limited))
    with pytest.raises(RateLimitedError) as info:
        provider.fetch(spec(SourceKind.HN_SEARCH, query="q"), LIMITS)
    assert info.value.reset_at is not None
    moved = httpx.Response(302, headers={"location": "https://elsewhere.example/feed"})
    feed = FeedProvider(transport=httpx.MockTransport(lambda _: moved))
    with pytest.raises(ProviderError, match="another host"):
        feed.fetch(spec(SourceKind.FEED, url="https://ex.com/feed"), LIMITS)
    local = FeedProvider(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    with pytest.raises(ProviderError, match="private"):
        local.fetch(spec(SourceKind.FEED, url="https://10.0.0.1/feed"), LIMITS)


def test_oversized_responses_are_refused():
    big = httpx.Response(200, content=b"x" * 2_100_000)
    feed = FeedProvider(transport=httpx.MockTransport(lambda _: big))
    with pytest.raises(ProviderError, match="larger than"):
        feed.fetch(spec(SourceKind.FEED, url="https://ex.com/feed"), LIMITS)


def test_curated_accounts_use_one_from_group_query():
    query = ResearchQuery(text="-is:reply", authors=["alice", "bob_2"], max_results=10)
    assert "(from:alice OR from:bob_2)" in effective_query(query)
    fake = FakeX(ok({"data": [], "meta": {"result_count": 0}}))
    x = XDiscoveryProvider(fake.provider())
    result = x.fetch(
        spec(
            SourceKind.X_ACCOUNTS,
            category=SourceCategory.DISCUSSION,
            handles=["alice"],
            evidence_type=EvidenceType.DISCUSSION_POST,
            tier=CredibilityTier.COMMUNITY,
        ),
        LIMITS,
    )
    assert result.requests_made == 1 and len(fake.requests) == 1
    assert "from:alice" in fake.last_params["query"]
    assert "next_token" not in fake.last_params and "pagination_token" not in fake.last_params


# --- claims ----------------------------------------------------------------------------------


def facts(source_id: str, text: str, publisher: str, *, primary: bool) -> EvidenceFacts:
    return EvidenceFacts(
        source_id=source_id,
        text=text,
        publisher=publisher,
        verification=VerificationStatus.PRIMARY_SOURCE
        if primary
        else VerificationStatus.UNVERIFIED,
        category=SourceCategory.PRIMARY if primary else SourceCategory.DISCUSSION,
    )


def test_item_verification_depends_on_the_source_not_popularity():
    assert (
        item_verification(SourceCategory.PRIMARY, EvidenceType.RELEASE, CredibilityTier.OFFICIAL)
        is VerificationStatus.PRIMARY_SOURCE
    )
    assert (
        item_verification(
            SourceCategory.DISCUSSION, EvidenceType.DISCUSSION_POST, CredibilityTier.COMMUNITY
        )
        is VerificationStatus.UNVERIFIED
    )


def test_claim_kinds_are_downgraded_to_what_the_evidence_supports():
    x_post = facts("x1", "v2 is 40% faster", "someone", primary=False)
    fact = validate_claim(ClaimKind.REPORTED_FACT, "v2 is 40% faster", [x_post])
    assert fact.kind is ClaimKind.UNVERIFIED_CLAIM
    assert fact.verification is VerificationStatus.UNVERIFIED
    disputed = validate_claim(ClaimKind.DISPUTED, "people disagree", [x_post])
    assert disputed.kind is ClaimKind.OPEN_QUESTION


def test_corroboration_needs_the_same_specific_claim_from_two_publishers():
    a = facts("a", "vLLM 0.11.2 adds speculative decoding", "vLLM", primary=True)
    b = facts("b", "Serving with vLLM 0.11.2 speculative decoding", "Hugging Face", primary=True)
    c = facts("c", "vLLM adds speculative decoding", "Hugging Face", primary=True)
    same = "vLLM 0.11.2 adds speculative decoding"
    assert claim_verification(same, [a, b]) is VerificationStatus.CORROBORATED
    # same subject, but the second source does not state the specific version
    assert claim_verification(same, [a, c]) is VerificationStatus.PRIMARY_SOURCE
    # two sources from one publisher do not corroborate each other
    a2 = facts("a2", "vLLM 0.11.2 release notes", "vLLM", primary=True)
    assert claim_verification(same, [a, a2]) is VerificationStatus.PRIMARY_SOURCE
    # nothing specific to compare: never corroborated
    assert claim_verification("speculative decoding is here", [a, b]) is (
        VerificationStatus.PRIMARY_SOURCE
    )
    # a number nobody stated
    assert claim_verification("vLLM 0.12 adds it", [a, b]) is VerificationStatus.UNVERIFIED


def test_claim_hints_are_heuristic_surface_signals():
    hints = claim_hints(
        None, "I think agents will replace testers by 2030?", SourceCategory.DISCUSSION
    )
    assert {ClaimHintKind.QUESTION, ClaimHintKind.OPINION, ClaimHintKind.PREDICTION} <= set(hints)


# --- ranking and Grok eligibility -------------------------------------------------------------


def item(
    item_id: str,
    text: str,
    *,
    primary: bool,
    hours: float = 10,
    url: str | None = None,
    publisher: str = "Pub",
    platform: SourcePlatform = "feed",
    title: str | None = None,
    hints: tuple[ClaimHintKind, ...] = (),
    author: str | None = None,
) -> AnalysisItem:
    return AnalysisItem(
        id=item_id,
        source_id="s",
        platform=platform,
        category=SourceCategory.PRIMARY if primary else SourceCategory.DISCUSSION,
        evidence_type=EvidenceType.OFFICIAL_ANNOUNCEMENT
        if primary
        else EvidenceType.DISCUSSION_POST,
        tier=CredibilityTier.OFFICIAL if primary else CredibilityTier.COMMUNITY,
        verification=VerificationStatus.PRIMARY_SOURCE
        if primary
        else VerificationStatus.UNVERIFIED,
        publisher=publisher,
        url=url,
        title=title,
        text=text,
        author=author,
        published_at=NOW - timedelta(hours=hours),
        hints=hints,
    )


def context(
    *,
    prior: tuple[PriorOpportunity, ...] = (),
    own: tuple[str, ...] = (),
    grok: tuple[str, ...] = (),
) -> AnalysisContext:
    return AnalysisContext(
        now=NOW,
        vocabulary=["agent", "agents", "evals", "tool use", "inference", "vllm", "routing"],
        pillars=["agent evals"],
        avoid_topics=["crypto"],
        weights=RankingWeights(),
        policy=DiscoveryPolicy(),
        prior_opportunities=prior,
        own_posts=own,
        prior_grok_questions=grok,
        generic_terms=frozenset({"agent", "agents"}),
    )


def test_primary_evidence_ranks_above_discussion_and_discussion_only_is_capped():
    primary = item(
        "p1",
        "vLLM inference release adds routing for agents",
        primary=True,
        url="https://vllm.example/r",
        title="vLLM inference release",
    )
    chatter = item(
        "x1",
        "agents evals tool use thread with lots of replies",
        primary=False,
        platform="x",
        author="loud",
        hints=(ClaimHintKind.QUESTION,),
    )
    result = analyze([primary, chatter], context())
    by_id = {o.item_ids[0]: o for o in result.opportunities}
    assert by_id["x1"].score <= 0.5
    assert result.opportunities[0].item_ids == ("p1",)
    assert "discussion only" in " ".join(by_id["x1"].reasons)
    assert by_id["x1"].headline.startswith("Topic: ")  # no X text copied into a headline


def test_off_topic_avoided_and_recently_proposed_topics_are_not_proposed():
    off = item("o", "a recipe for sourdough bread", primary=True)
    avoided = item("a", "agents for crypto trading", primary=True, url="https://c.example")
    fresh = item(
        "f",
        "agents evals with tool use",
        primary=True,
        url="https://e.example/1",
        title="Agent evals",
    )
    first = analyze([off, avoided, fresh], context())
    assert [o.item_ids for o in first.opportunities] == [("f",)]
    assert any("avoided" in s.reason for s in first.suppressed)
    seen = PriorOpportunity(
        fingerprint=first.opportunities[0].fingerprint,
        keywords=frozenset(first.opportunities[0].keywords),
        created_at=NOW - timedelta(hours=1),  # after the newest evidence
        dismissed=False,
    )
    again = analyze([fresh], context(prior=(seen,)))
    assert again.opportunities == ()
    assert "already proposed" in again.suppressed[0].reason


def test_own_recent_posts_suppress_repeats():
    fresh = item("f", "agents evals with tool use", primary=True, url="https://e.example/1")
    result = analyze([fresh], context(own=("agents evals with tool use",)))
    assert result.opportunities == () and "recent post" in result.suppressed[0].reason


def test_at_most_one_grok_question_per_session_and_zero_is_normal():
    topics = {1: "vllm routing for agents", 2: "evals of tool use in inference"}

    def grok_cluster(n: int) -> list[AnalysisItem]:
        return [
            item(
                f"p{n}",
                f"release notes: {topics[n]}",
                primary=True,
                url=f"https://v.example/{n}",
                title=f"Release {n}",
            ),
            item(
                f"q{n}",
                f"does the new {topics[n]} hold up?",
                primary=False,
                url=f"https://v.example/{n}",
                platform="hn",
                hints=(ClaimHintKind.QUESTION,),
            ),
        ]

    result = analyze(grok_cluster(1) + grok_cluster(2), context())
    eligible = [o for o in result.opportunities if o.grok.eligible]
    assert len(eligible) == 1
    assert ContentIntent.GROK_QUESTION in eligible[0].intents
    other = next(o for o in result.opportunities if not o.grok.eligible)
    assert any(f.startswith("7 ") for f in other.grok.failed)
    assert ContentIntent.GROK_QUESTION not in other.intents
    quiet = analyze(
        [item("p", "vllm routing release for agents", primary=True, url="https://v.example")],
        context(),
    )
    assert not any(o.grok.eligible for o in quiet.opportunities)  # no open question: none


def test_a_repeated_grok_question_is_not_eligible():
    cluster = [
        item(
            "p",
            "vllm routing release for agents",
            primary=True,
            url="https://v.example",
            title="vLLM routing release",
        ),
        item(
            "q",
            "why does vllm routing break tool use?",
            primary=False,
            url="https://v.example",
            platform="hn",
            hints=(ClaimHintKind.QUESTION,),
        ),
    ]
    result = analyze(cluster, context(grok=("@grok what about vllm routing release agents?",)))
    assert not result.opportunities[0].grok.eligible
    assert result.opportunities[0].grok.similar_to is not None


def test_the_audience_shortlist_only_uses_public_topic_signals():
    posts = [
        item("x1", "agents evals tool use", primary=False, platform="x", author="Alice"),
        item(
            "x2",
            "agents inference vllm?",
            primary=False,
            platform="x",
            author="alice",
            hints=(ClaimHintKind.QUESTION,),
        ),
    ]
    [entry] = analyze(posts, context()).audience
    assert entry.ref == "@alice" and entry.platform == "x"
    names = {s.name for s in entry.signals}
    assert names == {
        "topic_relevance",
        "technical_focus",
        "recent_activity",
        "conversation_quality",
        "public_engagement",
        "recurring_questions",
        "pillar_overlap",
    }
    conversation = next(s for s in entry.signals if s.name == "conversation_quality")
    assert conversation.value is None  # metrics not returned: unknown, never 0


# --- mentions, grounding and content rules ------------------------------------------------------


def candidate(
    content: str, rationale: str = "Because it helps readers decide."
) -> ContentCandidate:
    return ContentCandidate(
        run_id="run_1",
        generation_attempt=1,
        strategy_id="s",
        strategy_version=1,
        research_finding_ids=["f"],
        topic="t",
        hook_type=HookType.QUESTION,
        format=PostFormat.SINGLE,
        target_audience="eng",
        content=content,
        rationale=rationale,
    )


def test_mentions_are_found_in_any_form_and_emails_are_not_mentions():
    assert find_mentions("ask @Grok and @x_y") == ["grok", "x_y"]
    assert find_mentions("full-width \uff20grok") == ["grok"]
    assert find_mentions("mail me at a@b.com") == []
    assert is_manual_only("hi @grok") and not is_manual_only("no mention")


def test_mention_rule_allows_only_one_grok_in_a_grok_question():
    plain = ContentPolicy()
    [violation] = find_violations(candidate("thanks @someone"), plain)
    assert violation.rule == "mentions"
    grok = ContentPolicy(content_intent=ContentIntent.GROK_QUESTION)
    assert find_violations(candidate("@grok is this settled?"), grok) == []
    rules = {v.rule for v in find_violations(candidate("@grok @bob is it settled?"), grok)}
    assert "mentions" in rules
    assert "grok_question" in {
        v.rule for v in find_violations(candidate("@grok no question"), grok)
    }
    short = candidate("@grok is it settled?", rationale="because")
    assert "grok_question" in {v.rule for v in find_violations(short, grok)}


def test_repetitive_grok_questions_are_refused():
    policy = ContentPolicy(
        content_intent=ContentIntent.GROK_QUESTION,
        recent_grok_questions=("@grok what evidence settles the vllm routing question?",),
    )
    repeat = candidate("@grok what evidence settles the vllm routing question now?")
    assert "grok_question" in {v.rule for v in find_violations(repeat, policy)}
    novel = candidate("@grok how do teams grade flaky tool calls in agent evals?")
    assert find_violations(novel, policy) == []


def test_grounding_allows_only_links_and_numbers_from_the_evidence():
    grounding = grounding_from(
        ["vLLM 0.11.2 is 40% faster on 1,000 prompts"], ["https://vllm.example/r?utm_source=x"]
    )
    assert unsupported_urls("see https://vllm.example/r", grounding) == []
    assert unsupported_urls("see https://evil.example/x", grounding) == ["https://evil.example/x"]
    assert unsupported_numbers("40% faster on 1000 prompts in 2026, 3 tips", grounding) == []
    assert unsupported_numbers("55% faster", grounding) == ["55%"]
    policy = ContentPolicy(grounding=grounding)
    assert {v.rule for v in find_violations(candidate("now 2x cheaper: 75%"), policy)} == {
        "evidence_grounding"
    }


def test_the_policy_payload_tells_agents_the_intent_without_changing_older_runs():
    assert "content_intent" not in ContentPolicy().prompt_payload()
    payload = ContentPolicy(content_intent=ContentIntent.NEWS_COMMENTARY).prompt_payload()
    assert payload["mentions"] == "none"


def test_publishers_refuse_mentions_before_any_call():
    request = PublishRequest(account_id="a", candidate_id="c", content="hi @grok?")
    mock = MockPublisher()
    with pytest.raises(PublishRejectedError) as info:
        mock.publish(request)
    assert info.value.failure_category is PublishFailureCategory.MANUAL_ONLY
    assert mock.calls == []
    assert MANUAL_ONLY_REASON in str(info.value)


# --- pricing, verification and manual-publication helpers -------------------------------------


def test_price_list_ids_do_not_change_when_the_url_price_is_unset():
    base = PriceList(version="v", as_of="2026-10-13", currency="USD")
    assert "post_create_with_url" not in base.content()["x"]
    with_url = PriceList(
        version="v",
        as_of="2026-10-13",
        currency="USD",
        x=XPrices(post_create_with_url=Decimal("0.2")),
    )
    assert with_url.id != base.id


def test_posts_with_urls_and_verification_reads_are_priced_separately():
    prices = PriceList(
        version="v",
        as_of="2026-10-13",
        currency="USD",
        x=XPrices(
            post_read=Decimal("0.005"),
            user_read=Decimal("0.01"),
            post_create=Decimal("0.015"),
            post_create_with_url=Decimal("0.2"),
        ),
    )
    usage = UsageCounts(
        publishing=[PublishUsage(provider="x", attempts=3, posts_created=3, posts_with_url=1)],
        verification=[VerificationUsage(provider="x", registrations=1, post_reads=1, user_reads=1)],
    )
    estimate = estimate_cost(usage, prices, basis="current_pricing")
    lines = {line.item: line for line in estimate.lines}
    assert lines["posts_created"].quantity == 2
    assert lines["posts_created_with_url"].cost == Decimal("0.2")
    assert estimate.total == Decimal("0.03") + Decimal("0.2") + Decimal("0.005") + Decimal("0.01")
    unpriced = estimate_cost(
        usage, PriceList(version="v", as_of="2026-10-13", currency="USD"), basis="current_pricing"
    )
    assert "x:posts_created_with_url" in unpriced.missing_prices and unpriced.total is None
    no_url = estimate_cost(
        UsageCounts(publishing=[PublishUsage(provider="x", attempts=1, posts_created=1)]),
        prices,
        basis="current_pricing",
    )
    assert "posts_created_with_url" not in {line.item for line in no_url.lines}


def test_post_references_are_parsed_strictly():
    assert parse_post_ref("1950000000000000001") == "1950000000000000001"
    assert parse_post_ref("https://x.com/me/status/1950000000000000001?s=20") == (
        "1950000000000000001"
    )
    assert parse_post_ref("https://twitter.com/i/web/status/19") == "19"
    for bad in ("https://evil.example/me/status/1", "http://x.com/me/status/1", "abc"):
        with pytest.raises(InvalidDiscoveryRequestError):
            parse_post_ref(bad)


def test_posted_text_must_match_after_link_and_whitespace_normalization():
    exported = "@grok is this settled? Source: https://vllm.example/r"
    assert text_match(exported, exported) is not None
    posted = "@grok is this  settled?\nSource: https://t.co/AbC123"
    assert text_match(exported, posted) is not None
    assert text_match(exported, "@grok is this settled? Source: x &amp; y") is None
    assert text_match("A & B", "A &amp; B") is not None


def test_x_post_lookup_reads_author_and_full_text_once():
    body = {
        "data": [
            {
                "id": "19",
                "author_id": "42",
                "text": "short",
                "created_at": "2026-10-13T10:00:00.000Z",
                "note_tweet": {"text": "the full long text"},
            }
        ],
        "includes": {"users": [{"id": "42", "username": "me"}]},
    }
    fake = FakeX(ok(body))
    client = XApiClient(SecretStr(TOKEN), timeout_seconds=1.0, transport=fake.transport())
    post = XAnalyticsProvider(client).lookup_post("19")
    assert post == PlatformPost(
        provider_post_id="19",
        author_id="42",
        author_username="me",
        text="the full long text",
        created_at=datetime(2026, 10, 13, 10, 0, tzinfo=UTC),
        post_reads=1,
        user_reads=1,
    )
    params = fake.last_params
    assert params["ids"] == "19" and params["expansions"] == "author_id"
    missing = FakeX(ok({"errors": [{"resource_id": "20", "type": "x/resource-not-found"}]}))
    client = XApiClient(SecretStr(TOKEN), timeout_seconds=1.0, transport=missing.transport())
    assert XAnalyticsProvider(client).lookup_post("20") is None
    failing = FakeX(error(503))
    client = XApiClient(SecretStr(TOKEN), timeout_seconds=1.0, transport=failing.transport())
    with pytest.raises(Exception) as info:
        XAnalyticsProvider(client).lookup_post("21")
    assert TOKEN not in str(info.value)


def test_the_mock_verifier_knows_only_posts_it_was_given():
    mock = MockAnalyticsProvider()
    assert mock.lookup_post("1") is None
    mock.add_post(PlatformPost(provider_post_id="1", author_id="a", text="t"))
    found = mock.lookup_post("1")
    assert found is not None and found.author_id == "a"


# --- shortlist diversity (Phase 9 correction) ---------------------------------------


def langgraph_heavy() -> list[AnalysisItem]:
    gh = "https://github.com/langchain-ai/langgraph/releases/tag/"
    releases = [
        ("r1", "langgraph 1.1.0 agents tool use evals checkpoint durability", "1.1.0"),
        ("r2", "langgraph-sdk 0.2.9 client retries for agents tool use evals", "sdk-0.2.9"),
        ("r3", "langgraph-cli 0.4.1 dockerfile generation agents tool use evals", "cli-0.4.1"),
        (
            "r4",
            "langgraph-checkpoint-postgres 2.0.3 agents state migrations tool use evals",
            "cp-2.0.3",
        ),
    ]
    out = [
        item(i, text, primary=True, hours=2, url=gh + tag, publisher="LangGraph (GitHub)")
        for i, text, tag in releases
    ]
    out += [
        item(
            "o1",
            "vLLM inference serving speedup",
            primary=True,
            hours=150,
            url="https://github.com/vllm-project/vllm/releases/tag/v1",
            publisher="vLLM",
        ),
        item(
            "o2",
            "evals for tool use in agents, a new benchmark paper",
            primary=True,
            hours=200,
            url="https://arxiv.org/abs/2610.00001",
            publisher="arXiv",
        ),
        item(
            "o3",
            "routing small models for agents explained",
            primary=True,
            hours=250,
            url="https://blog.example/routing",
            publisher="Blog",
        ),
    ]
    return out


def test_one_project_cannot_fill_the_shortlist_and_nothing_is_dropped():
    items = langgraph_heavy()
    ctx = context()
    result = analyze(items, ctx)
    opps = list(result.opportunities)
    projects = [o.project for o in opps]
    assert projects[:5].count("langchain-ai/langgraph") == 2
    assert len(opps) == 7 and projects.count("langchain-ai/langgraph") == 4

    uncapped = analyze(
        items,
        replace(ctx, policy=ctx.policy.model_copy(update={"max_per_project_in_shortlist": 50})),
    )
    by_score = list(uncapped.opportunities)
    assert [o.project for o in by_score[:5]].count("langchain-ai/langgraph") == 4
    # same opportunities, same scores and evidence; only the order differs
    assert {(o.fingerprint, o.score, o.item_ids) for o in opps} == {
        (o.fingerprint, o.score, o.item_ids) for o in by_score
    }
    assert [o.score for o in by_score] == sorted((o.score for o in by_score), reverse=True)
    assert len(result.moved) == 2 and uncapped.moved == ()
    for move in result.moved:
        assert move.project == "langchain-ai/langgraph"
        assert move.score_rank <= 5 < move.rank
        moved = next(o for o in opps if o.fingerprint == move.fingerprint)
        assert "shortlist diversity" in moved.reasons[-1]
        assert "score unchanged" in moved.reasons[-1]
    # deterministic
    assert [o.fingerprint for o in analyze(items, ctx).opportunities] == [
        o.fingerprint for o in opps
    ]


def test_diversity_ignores_items_without_a_project():
    feeds = [
        item(
            f"f{n}",
            f"agents evals story number {word}",
            primary=True,
            hours=n,
            url=f"https://news.example/{word}",
        )
        for n, word in enumerate(["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"], 1)
    ]
    result = analyze(feeds, context())
    assert result.moved == ()
    assert all(o.project is None for o in result.opportunities)


@pytest.mark.parametrize(
    ("url", "project"),
    [
        ("https://github.com/langchain-ai/langgraph/releases/tag/1.0", "langchain-ai/langgraph"),
        ("https://www.github.com/vllm-project/vllm", "vllm-project/vllm"),
        ("https://github.com/only-owner", None),
        ("https://gitlab.com/a/b", None),
        (None, None),
    ],
)
def test_project_of_github_urls(url, project):
    assert project_of(url) == project


# --- arXiv feed format (Phase 9 correction) -------------------------------------------

ARXIV_ITEM = (
    b"<item><title>Small models as routers for agentic workflows</title>"
    b"<link>https://arxiv.org/abs/2610.01234</link>"
    b"<description>arXiv:2610.01234v1 Announce Type: new \nAbstract: We route agents.</description>"
    b'<guid isPermaLink="false">oai:arXiv.org:2610.01234v1</guid><category>cs.AI</category>'
    b"<pubDate>Tue, 13 Oct 2026 00:00:00 -0400</pubDate>"
    b"<arxiv:announce_type>new</arxiv:announce_type><dc:creator>A. Author, B. Author</dc:creator>"
    b"</item>"
)


def arxiv_feed(items: bytes) -> bytes:
    return (
        b'<?xml version="1.0" encoding="UTF-8"?><rss xmlns:arxiv="http://arxiv.org/schemas/atom"'
        b' xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:atom="http://www.w3.org/2005/Atom"'
        b' version="2.0"><channel><title>cs.AI updates on arXiv.org</title>'
        b"<link>http://rss.arxiv.org/rss/cs.AI</link>"
        b"<lastBuildDate>Sat, 10 Oct 2026 00:30:00 +0000</lastBuildDate>"
        b"<skipDays><day>Saturday</day><day>Sunday</day></skipDays>" + items + b"</channel></rss>"
    )


def test_the_arxiv_rss_format_parses():
    [entry] = parse_feed(arxiv_feed(ARXIV_ITEM), max_items=20)
    assert entry.entry_id == "oai:arXiv.org:2610.01234v1"
    assert entry.url == "https://arxiv.org/abs/2610.01234"
    assert entry.author == "A. Author, B. Author"
    assert entry.published_at == datetime(2026, 10, 13, 4, 0, tzinfo=UTC)


def test_an_empty_feed_says_why_in_the_fetch_ledger():
    body = arxiv_feed(b"")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, content=body, headers={"content-type": "application/rss+xml"}
        )
    )
    arxiv = spec(SourceKind.FEED, id="arxiv-cs-ai", url="https://rss.arxiv.org/rss/cs.AI")
    result = FeedProvider(transport=transport).fetch(arxiv, DiscoveryLimits())
    assert result.items == []
    assert result.note is not None
    assert result.note.startswith("RSS channel with 0 item(s)")
    assert "built Sat, 10 Oct 2026" in result.note and "application/rss+xml" in result.note
    full = FeedProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=arxiv_feed(ARXIV_ITEM))
        )
    ).fetch(arxiv, DiscoveryLimits())
    assert len(full.items) == 1 and full.note is None


def test_ranking_and_grok_specificity_come_from_the_registry_not_from_code():
    """Nothing in analysis is technology-specific: another domain brings its own
    vocabulary and generic terms (here: fashion)."""
    looks = [
        item(
            "f1",
            "Runway season: tailoring returns, quiet luxury fades?",
            primary=True,
            hours=5,
            url="https://fashion.example/runway",
            hints=(ClaimHintKind.QUESTION,),
        ),
        item(
            "f2",
            "Is quiet luxury over? Buyers disagree",
            primary=False,
            hours=3,
            platform="x",
            hints=(ClaimHintKind.QUESTION,),
        ),
    ]
    base = context()
    fashion = replace(
        base,
        vocabulary=["tailoring", "quiet luxury", "runway", "style"],
        pillars=["tailoring"],
        generic_terms=frozenset({"style", "runway"}),
    )

    def runway(ctx: AnalysisContext) -> RankedOpportunity:
        return next(o for o in analyze(looks, ctx).opportunities if "f1" in o.item_ids)

    opp = runway(fashion)
    assert set(opp.keywords) >= {"tailoring", "quiet luxury"}
    assert "3 a specific subject" in opp.grok.passed and opp.grok.eligible
    broad = replace(fashion, generic_terms=frozenset({"tailoring", "quiet luxury", "runway"}))
    same = runway(broad)
    assert same.score == opp.score  # generic terms only affect Grok criterion 3
    assert "3 a specific subject" in same.grok.failed and not same.grok.eligible
    assert analyze(looks, base).opportunities == ()  # tech vocabulary: off-topic here
