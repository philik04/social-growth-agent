"""Phase 9 demo: content discovery to a reviewed draft, an optional @grok question that
stays manual, its verified manual-publication registration, and a deletion request.

    DATABASE_URL=postgresql://... uv run python -m social_growth_agent.services.discovery_demo

Offline and free. Feeds, GitHub releases and Hacker News are served from bundled
fixtures through the real provider classes (no network); the X posts are synthetic; the
agents are the deterministic fakes; the "post published by hand" is a synthetic post in
the mock verifier. Nothing is posted, replied to, mentioned, followed or liked, and no
model is called. The demo shows, in order:

1. the plan: the capped calls and the maximum X read cost, before anything is called;
2. one bounded session: one GET per source, provenance and outcomes per fetch;
3. ranked opportunities (score components, claim hints, suggested intents, at most one
   @grok-eligible topic) and the read-only audience shortlist;
4. a news-commentary run from an opportunity: its research replays the stored evidence
   (0 external reads) and the draft waits for human review;
5. when a topic qualifies, one @grok question: approved, refused by the API publish
   path, exported for manual posting, registered from its URL after a one-read check of
   author and text, registered again idempotently, and traced in the lineage;
6. a deletion request: the post's text leaves items, run evidence and checkpoints, and a
   later session does not store it again.

Use a scratch database: the demo runs ``sga-db upgrade`` on it.
"""

import secrets

from pydantic import SecretStr

from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.config import AppSettings
from social_growth_agent.errors import NotPublishableError
from social_growth_agent.graph import Dependencies
from social_growth_agent.models import (
    Account,
    ContentIntent,
    ContentStrategy,
    HookType,
    PlatformPost,
    new_id,
    utc_now,
)
from social_growth_agent.models.discovery import (
    DiscoveryRequest,
    OpportunityView,
    PurgeRequest,
    StartRunFromOpportunity,
)
from social_growth_agent.models.review import ReviewAction, ReviewDecision
from social_growth_agent.models.strategy import ReviewAuthMethod
from social_growth_agent.persistence.migrate import upgrade
from social_growth_agent.providers.mocks import MockAnalyticsProvider, MockResearchProvider
from social_growth_agent.providers.mocks.discovery import MockDiscoveryProviders
from social_growth_agent.services.discovery import ExportBody, ManualPublicationBody
from social_growth_agent.services.review_auth import ReviewAuth
from social_growth_agent.services.runs import InlineExecutor
from social_growth_agent.services.runtime import Runtime, build_runtime

OWNER_ID = "4242"
REVIEWER = "demo-reviewer"


def build(settings: AppSettings, verifier: MockAnalyticsProvider) -> Runtime:
    deps = Dependencies(llm=build_fake_llm(), research_provider=MockResearchProvider())
    return build_runtime(
        settings,
        deps=deps,
        executor=InlineExecutor(),
        discovery_providers=MockDiscoveryProviders(),
        post_verifier=lambda: verifier,
    )


def approve(runtime: Runtime, run_id: str) -> str:
    detail = runtime.service.get_run(run_id)
    candidate_id = detail.review.candidate_ids[0]
    runtime.service.submit_review(
        run_id,
        ReviewDecision(action=ReviewAction.APPROVE, candidate_id=candidate_id, reviewer=REVIEWER),
    )
    return candidate_id


def content_of(runtime: Runtime, run_id: str, candidate_id: str) -> str:
    detail = runtime.service.get_run(run_id)
    return next(c.content for c in detail.candidates if c.id == candidate_id)


def pick(opportunities: list[OpportunityView], intent: ContentIntent) -> OpportunityView | None:
    return next((o for o in opportunities if intent in o.suggested_intents), None)


def main() -> int:
    settings = AppSettings()
    if settings.database_url is None:
        print("Set DATABASE_URL to a scratch PostgreSQL database (see docs/DATABASE.md).")
        return 2
    upgrade(settings.database_url.get_secret_value())
    token = secrets.token_urlsafe(24)  # never printed
    settings = settings.model_copy(
        update={
            "llm_provider": "fake",
            "research_provider": "mock",
            "publisher_provider": "mock",
            "analytics_provider": "mock",
            "discovery_sources": "mock",
            "strategy_proposer": "fake",
            "strategy_review_token": SecretStr(token),
            "x_account_user_id": OWNER_ID,
        }
    )
    verifier = MockAnalyticsProvider()
    runtime = build(settings, verifier)
    try:
        return run_demo(runtime, verifier, ReviewAuth(token=SecretStr(token)), token)
    finally:
        runtime.close()


def run_demo(
    runtime: Runtime, verifier: MockAnalyticsProvider, review_auth: ReviewAuth, token: str
) -> int:
    discovery, strategies = runtime.discovery, runtime.strategies
    assert discovery is not None and strategies is not None
    auth = review_auth.authenticate(f"Bearer {token}")
    account = Account(id=new_id("acct"), handle="@phase9_demo", niche="AI engineering")
    strategy = ContentStrategy(
        id=new_id("strat"),
        account_id=account.id,
        pillars=["agent engineering", "llm evaluation", "inference infrastructure"],
        tone="practical, specific, no hype",
        target_audience="engineers shipping LLM features",
        preferred_hooks=[HookType.HOW_TO],
    )
    strategies.register_version(strategy, reviewer=REVIEWER, auth=auth, note="phase 9 demo")
    strategies.activate(
        strategy.id, 1, expected_active_version_id=None, reviewer=REVIEWER, auth=auth, note=None
    )

    print("== 1. plan (calls nothing)")
    request = DiscoveryRequest(account_id=account.id, requested_by="demo")
    plan = discovery.plan(request)
    for call in plan.calls:
        print(f"    {call.source_id:<22} {call.calls} call(s)  {call.note}")
    for reason in plan.skipped:
        print(f"    skipped: {reason}")
    print(
        f"    max X reads: {plan.max_x_post_reads} posts, {plan.max_x_user_reads} users; "
        f"estimated max cost {plan.estimated_max_cost} (synthetic X fixtures are unbilled)"
    )

    print("\n== 2. one bounded session (fixtures; nothing paginates or retries)")
    session = discovery.run_session(request, auth=auth)
    print(f"    {session.id}: {session.status.value}, synthetic={session.synthetic}")
    for fetch in session.fetches:
        note = f" ({fetch.error_message})" if fetch.error_message else ""
        print(
            f"    {fetch.source_id:<22} {fetch.outcome.value:<12} "
            f"items={fetch.items_returned}{note}"
        )

    print("\n== 3. ranked opportunities and the read-only audience shortlist")
    opportunities = discovery.opportunities(session.id)
    for opp in opportunities[:5]:
        components = ", ".join(f"{c.name}={c.value:.2f}" for c in opp.components)
        grok = "  @grok-eligible" if opp.grok.eligible else ""
        print(f"    #{opp.rank} {opp.score:.3f} {opp.headline[:70]}{grok}")
        print(f"       {components}")
        print(f"       intents: {', '.join(i.value for i in opp.suggested_intents)}")
        print(f"       claims: {opp.claim_summary}")
    eligible = sum(o.grok.eligible for o in opportunities)
    print(f"    @grok-eligible topics: {eligible} (at most one per session; zero is fine)")
    for entry in discovery.audience(session.id)[:4]:
        print(f"    audience {entry.kind.value:<10} {entry.ref}: {entry.explanation}")

    print("\n== 4. a news-commentary run from an opportunity (evidence replay)")
    news = pick(opportunities, ContentIntent.NEWS_COMMENTARY)
    if news is None:
        print("    no opportunity suggests news commentary")
        return 1
    started = discovery.start_run(
        news.id,
        StartRunFromOpportunity(
            account=account, intent=ContentIntent.NEWS_COMMENTARY, reviewer=REVIEWER
        ),
        auth=auth,
    )
    detail = runtime.service.get_run(started.run.id)
    replay = detail.research_fetches[0]
    print(
        f"    run {started.run.id}: {detail.run.status.value}; research via {replay.provider} "
        f"({replay.requests_made} requests, {replay.posts_fetched} reads)"
    )
    for finding in detail.research.findings if detail.research else []:
        print(f"    finding [{finding.claim_kind}/{finding.verification}] {finding.summary[:80]}")
    for candidate in detail.candidates[:2]:
        print(f"    draft: {candidate.content[:100]}")

    print("\n== 5. an optional @grok question (manual publication only)")
    grok_opp = pick(opportunities, ContentIntent.GROK_QUESTION)
    if grok_opp is None or not grok_opp.grok.eligible:
        print("    no topic qualified this session; no @grok question is drafted")
    else:
        grok_question(runtime, verifier, account, grok_opp, auth)

    print("\n== 6. a deletion request, then a later session")
    # a discussion item the news run used as evidence was deleted on its platform
    item = next(i for i in discovery.opportunity_items(news.id) if i.platform in ("x", "hn"))
    purged = discovery.purge(
        PurgeRequest(
            platform=item.platform, provider_item_ids=[item.provider_item_id], requested_by=REVIEWER
        ),
        auth=auth,
    )
    print(
        f"    purged {item.platform}:{item.provider_item_id}: items={purged.discovery_items} "
        f"run evidence={purged.source_posts} checkpoint blobs={purged.checkpoint_blobs} "
        f"writes={purged.checkpoint_writes} tombstones={purged.tombstones}"
    )
    again = discovery.run_session(request.model_copy(update={"refresh": True}), auth=auth)
    fetch = next(f for f in again.fetches if f.source_id == item.source_id)
    print(f"    next session, {fetch.source_id}: {fetch.outcome.value} ({fetch.error_message})")
    print(f"    retention sweep now: {discovery.purge_expired().provider_item_ids} (nothing old)")
    return 0


def grok_question(
    runtime: Runtime,
    verifier: MockAnalyticsProvider,
    account: Account,
    opportunity: OpportunityView,
    auth: ReviewAuthMethod,
) -> None:
    discovery = runtime.discovery
    assert discovery is not None
    print(f"    topic: {opportunity.headline[:80]}")
    for criterion in opportunity.grok.passed:
        print(f"      passed: {criterion}")
    started = discovery.start_run(
        opportunity.id,
        StartRunFromOpportunity(
            account=account, intent=ContentIntent.GROK_QUESTION, reviewer=REVIEWER
        ),
        auth=auth,
    )
    run_id = started.run.id
    candidate_id = approve(runtime, run_id)
    content = content_of(runtime, run_id, candidate_id)
    print(f"    approved draft: {content}")
    try:
        runtime.publications.request(run_id, candidate_id, requested_by=REVIEWER)
    except NotPublishableError as exc:
        print(f"    API publish refused: {exc}")
    export = discovery.export(run_id, candidate_id, ExportBody(exported_by=REVIEWER), auth=auth)
    print(f"    exported {export.id} (manual_only={export.manual_only})")
    for line in export.instructions:
        print(f"      {line}")

    # the person posts it by hand; X shows it under the configured account
    post_id = "1950000000000000001"
    verifier.add_post(
        PlatformPost(
            provider_post_id=post_id,
            author_id=OWNER_ID,
            author_username="phase9_demo",
            text=content,
            created_at=utc_now(),
        )
    )
    url = f"https://x.com/phase9_demo/status/{post_id}"
    body = ManualPublicationBody(post=url, registered_by=REVIEWER)
    first = discovery.register_manual_publication(run_id, candidate_id, body, auth=auth)
    print(
        f"    registered {first.provider_post_url}: text {first.text_match.value}, "
        f"author verified, analytics jobs={first.analytics_jobs}"
    )
    again = discovery.register_manual_publication(run_id, candidate_id, body, auth=auth)
    print(
        f"    registered again: already_registered={again.already_registered}, "
        f"same record={again.id == first.id}, verifier reads={len(verifier.lookups)}"
    )
    lineage = runtime.analytics.lineage(first.publication_id)
    assert lineage.discovery is not None
    print(
        f"    lineage: publication {lineage.publication.status} via "
        f"{lineage.publication.provider}, opportunity {lineage.discovery.opportunity_id}, "
        f"{len(lineage.discovery.evidence)} evidence item(s)"
    )


if __name__ == "__main__":
    raise SystemExit(main())
