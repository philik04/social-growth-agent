from datetime import timedelta

import pytest

from social_growth_agent.models import ResearchQuery, new_id, utc_now
from social_growth_agent.models.discovery import (
    DiscoveredItem,
    DiscoveryFetchOutcome,
    DiscoveryLimits,
    PurgeRequest,
    SourceSpec,
)
from social_growth_agent.models.strategy import ReviewAuthMethod
from social_growth_agent.persistence import discovery as ddb
from social_growth_agent.persistence import repository as repo
from social_growth_agent.persistence.tables import (
    DiscoveryOpportunityRow,
    DiscoverySessionRow,
    OpportunityEvidenceRow,
)
from social_growth_agent.providers.mocks import MockResearchProvider
from social_growth_agent.services.enrichment import EnrichmentBody, enrich_primary_source
from social_growth_agent.services.replay import EvidenceReplayProvider
from tests.db.conftest import sql
from tests.db.strategy_helpers import ACCOUNT, AUTH, TOKEN, V1
from tests.enrichment_helpers import FIRST, HTML, URL, article_provider


@pytest.fixture
def env(make_runtime, client_for):
    runtime = make_runtime(strategy_review_token=TOKEN)
    discovery = runtime.discovery
    assert discovery is not None
    sid, fid, oid = new_id("disc"), new_id("dfetch"), new_id("opp")
    now = utc_now()
    spec = SourceSpec(
        id="huggingface-blog",
        kind="feed",
        category="primary",
        publisher="Vendor Labs",
        tier="official",
        evidence_type="engineering_blog",
        url="https://huggingface.co/blog/feed.xml",
    )
    with runtime.db.transaction() as session:
        session.add(
            DiscoverySessionRow(
                id=sid,
                account_id=ACCOUNT.id,
                status="succeeded",
                synthetic=False,
                request={},
                limits=DiscoveryLimits().model_dump(mode="json"),
                registry_version="test",
                registry_sha256="a" * 64,
                requested_by="tester",
                auth_method="review_token",
                started_at=now,
                finished_at=now,
                pricing_id=repo.save_price_list(session, discovery._prices),
            )
        )
        session.flush()
        session.add(
            discovery._fetch_row(fid, sid, spec, "feed", now, DiscoveryFetchOutcome.SUCCEEDED, {})
        )
        session.flush()
        item = DiscoveredItem(
            source_id=spec.id,
            platform="feed",
            kind="feed",
            category="primary",
            publisher=spec.publisher,
            tier="official",
            evidence_type="engineering_blog",
            provider_item_id=URL,
            url=URL,
            title="Local agents",
            text="Local agents",
            published_at=now,
            retrieved_at=now,
        )
        ids, _ = ddb.upsert_items(
            session,
            session_id=sid,
            fetch_id=fid,
            items=[item],
            vocabulary=["agents"],
            synthetic=False,
            now=now,
        )
        session.add(
            DiscoveryOpportunityRow(
                id=oid,
                session_id=sid,
                account_id=ACCOUNT.id,
                fingerprint="base",
                rank=1,
                score=0.6,
                keywords=["agents"],
                headline="Local agents",
                components=[],
                reasons=["Primary source"],
                claim_hints=[],
                claim_summary={},
                suggested_intents=["technical_explanation", "original_insight"],
                grok={"eligible": False, "passed": [], "failed": ["No discussion"]},
                grok_eligible=False,
                audience_entry_ids=[],
                synthetic=False,
                status="open",
                expires_at=now + timedelta(days=7),
                created_at=now,
            )
        )
        session.flush()
        session.add(
            OpportunityEvidenceRow(opportunity_id=oid, item_id=ids[0], role="primary", position=0)
        )
    return runtime, client_for(runtime), oid, ids[0]


def enrich(env, provider, **kwargs):
    runtime, _, oid, item = env
    return enrich_primary_source(
        runtime.discovery,
        oid,
        EnrichmentBody(item_id=item, url=URL, requested_by="tester", **kwargs),
        auth=ReviewAuthMethod.REVIEW_TOKEN,
        provider=provider,
    )


def test_success_preserves_history_and_has_normal_replay_lineage(env):
    runtime, client, oid, old = env
    requests = []
    result = enrich(env, article_provider(requests))
    assert result.session.status.value == "succeeded"
    assert result.opportunity.id != oid and not result.opportunity.synthetic
    new = result.opportunity.evidence[0].item_id
    assert new != old and result.session.request["parent_opportunity_id"] == oid
    assert (
        result.session.summary["self_reported"]
        and not result.session.summary["independently_verified"]
    )
    assert len(result.session.summary["response_sha256"]) == 64
    original = runtime.discovery.opportunity_items(oid)[0]
    assert original.text == "Local agents"
    enriched = runtime.discovery.opportunity_items(result.opportunity.id)[0]
    assert FIRST in enriched.text and enriched.author == "Vendor Labs"
    response = client.post(
        f"/discovery/opportunities/{result.opportunity.id}/runs",
        json={
            "account": ACCOUNT.model_dump(mode="json"),
            "strategy": V1.model_dump(mode="json"),
            "intent": "technical_explanation",
            "reviewer": "tester",
        },
        headers=AUTH,
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run"]["id"]
    detail = client.get(f"/runs/{run_id}").json()
    assert detail["run"]["status"] == "awaiting_review"
    assert detail["research_fetches"][0]["requests_made"] == 0
    assert detail["source_posts"][0]["source_id"] == new
    assert sql(
        runtime.db.libpq_url, "SELECT source_id FROM finding_evidence WHERE run_id=:r", r=run_id
    ) == [(new,)]
    assert len(requests) == 2  # no article re-read on run/review


def test_duplicate_and_explicit_versions_are_audited_without_overwriting(env):
    requests = []
    provider = article_provider(requests)
    first = enrich(env, provider)
    second = enrich(env, provider)
    assert second.already_enriched and second.session.id == first.session.id and len(requests) == 2
    changed = HTML.replace(b"32K", b"64K")
    third = enrich(env, article_provider(requests, body=changed), refresh=True)
    assert third.session.id != first.session.id
    assert third.opportunity.evidence[0].item_id != first.opportunity.evidence[0].item_id
    runtime = env[0]
    assert "32K" in runtime.discovery.opportunity_items(first.opportunity.id)[0].text
    assert "64K" in runtime.discovery.opportunity_items(third.opportunity.id)[0].text


@pytest.mark.parametrize(
    "body,status",
    [(HTML, 500), (HTML[:-20], 200), (b"<html><article><p>Title</p></article></html>", 200)],
)
def test_failed_fetch_has_started_finished_ledger_and_no_evidence(env, body, status):
    requests = []
    provider = article_provider(requests, body=body, status=status)
    result = enrich(env, provider)
    assert result.session.status.value == "failed" and result.opportunity is None
    assert result.session.fetches[0].requests_made == 2
    assert result.session.fetches[0].http_status == status
    assert result.session.fetches[0].finished_at
    assert enrich(env, provider).already_enriched and len(requests) == 2
    assert sql(env[0].db.libpq_url, "SELECT count(*) FROM discovery_items") == [(1,)]


def test_auth_and_url_binding_before_fetch(env, monkeypatch):
    _runtime, client, oid, item = env
    requests = []
    monkeypatch.setattr(
        "social_growth_agent.services.enrichment.PrimaryArticleProvider",
        lambda: article_provider(requests),
    )
    body = {"item_id": item, "url": URL, "requested_by": "tester"}
    assert client.post(f"/discovery/opportunities/{oid}/enrich", json=body).status_code == 401
    assert (
        client.post(
            f"/discovery/opportunities/{oid}/enrich",
            json=body | {"url": "https://evil.example/article"},
            headers=AUTH,
        ).status_code
        == 422
    )
    assert requests == []
    got = client.post(f"/discovery/opportunities/{oid}/enrich", json=body, headers=AUTH)
    assert got.status_code == 201 and got.json()["session"]["status"] == "succeeded"


def test_purge_covers_all_versions_and_restart_checkpoints(env, make_runtime, client_for):
    runtime, client, oid, _old = env
    result = enrich(env, article_provider([]))
    enriched_oid = result.opportunity.id
    run = client.post(
        f"/discovery/opportunities/{enriched_oid}/runs",
        json={
            "account": ACCOUNT.model_dump(mode="json"),
            "strategy": V1.model_dump(mode="json"),
            "intent": "technical_explanation",
            "reviewer": "tester",
        },
        headers=AUTH,
    ).json()["run"]["id"]
    restarted = make_runtime(strategy_review_token=TOKEN)
    resumed_evidence = restarted.service._workflow.get_run(run).state.source_posts
    assert FIRST in resumed_evidence[0].text
    replay = EvidenceReplayProvider(MockResearchProvider(), restarted.db).search(
        ResearchQuery(text="local", discovery_opportunity_id=enriched_oid)
    )
    assert FIRST in replay.posts[0].text and replay.fetch.requests_made == 0
    runtime.discovery.purge(
        PurgeRequest(platform="feed", provider_item_ids=[URL], requested_by="tester"),
        auth=ReviewAuthMethod.REVIEW_TOKEN,
    )
    assert all(
        i.purged and i.text is None
        for i in runtime.discovery.opportunity_items(oid)
        + runtime.discovery.opportunity_items(enriched_oid)
    )
    posts = (
        EvidenceReplayProvider(MockResearchProvider(), restarted.db)
        .search(ResearchQuery(text="local", discovery_opportunity_id=enriched_oid))
        .posts
    )
    assert posts == []
    # The review checkpoint is redacted, not just the visible DB evidence projection.
    state = restarted.service._workflow.get_run(run).state
    assert FIRST not in str(state)
    with pytest.raises(Exception, match="unpurged"):
        enrich(env, article_provider([]))


def test_deletion_during_fetch_wins_before_new_version_is_stored(env):
    runtime = env[0]
    provider = article_provider([])
    original = provider.fetch

    def race(url, selected=None):
        result = original(url, selected)
        runtime.discovery.purge(
            PurgeRequest(platform="feed", provider_item_ids=[URL], requested_by="tester"),
            auth=ReviewAuthMethod.REVIEW_TOKEN,
        )
        return result

    provider.fetch = race
    result = enrich(env, provider)
    assert result.session.status.value == "failed" and result.opportunity is None
    assert sql(runtime.db.libpq_url, "SELECT count(*) FROM discovery_items") == [(1,)]


def test_inflight_duplicate_reuses_started_audit_without_another_fetch(env):
    requests = []
    provider = article_provider(requests)
    original = provider.fetch
    observed = []

    def concurrent(url, selected=None):
        observed.append(enrich(env, article_provider([])))
        return original(url, selected)

    provider.fetch = concurrent
    result = enrich(env, provider)
    assert observed[0].already_enriched
    assert observed[0].session.status.value == "running"
    assert observed[0].session.id == result.session.id
    assert len(requests) == 2
