"""Content discovery (Phase 9) on PostgreSQL: sessions, caps and cache, provenance,
ranking output, runs from opportunities with evidence replay, restart."""

from typing import Any

import pytest

from social_growth_agent.persistence.migrate import alembic_config, current_revision, upgrade
from tests.db.conftest import create_scratch_database, drop_scratch_database, sql
from tests.db.discovery_helpers import Env, by_intent
from tests.db.strategy_helpers import ACCOUNT, AUTH


@pytest.fixture
def env(make_runtime, client_for) -> Env:
    return Env(make_runtime, client_for)


def outcomes(session: dict[str, Any]) -> dict[str, str]:
    return {f["source_id"]: f["outcome"] for f in session["fetches"]}


def test_plan_calls_nothing_and_shows_the_capped_budget(env: Env):
    response = env.client.post(
        "/discovery/plan", json={"account_id": ACCOUNT.id, "requested_by": "philipp"}
    )
    assert response.status_code == 200, response.text
    plan = response.json()
    assert env.web.requests == [] and env.x.calls == []
    planned = {c["source_id"]: c for c in plan["calls"]}
    assert planned["x-ai-engineering"]["max_post_reads"] == 10
    assert plan["max_x_post_reads"] == 10 and plan["max_x_user_reads"] == 10
    assert any(s.startswith("x-curated: no curated handles") for s in plan["skipped"])
    assert plan["estimated_max_cost"] == "0"  # synthetic X fixtures are unbilled


def test_sources_list_is_read_only_configuration(env: Env):
    sources = env.client.get("/discovery/sources").json()
    assert {s["id"] for s in sources} >= {"openai-news", "arxiv-cs-ai", "x-ai-engineering"}
    assert all(s["kind"] != "x_accounts" or s["handles"] == [] for s in sources)


def test_a_session_needs_the_review_token(env: Env):
    body = {"account_id": ACCOUNT.id, "requested_by": "philipp"}
    assert env.client.post("/discovery/sessions", json=body).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert env.client.post("/discovery/sessions", json=body, headers=wrong).status_code == 403
    assert env.web.requests == []


def test_a_session_stores_items_with_provenance_and_ranks_opportunities(env: Env):
    session = env.session()
    assert session["status"] == "succeeded", session
    assert session["synthetic"] is True
    got = outcomes(session)
    assert got["x-curated"] == "skipped"
    assert {got[s] for s in ("openai-news", "arxiv-cs-ai", "langgraph-releases")} == {"succeeded"}
    # one GET per feed, one per repository, one HN search; nothing paginates
    assert len(env.web.requests) == 4 + 2 + 1
    assert all(r.method == "GET" for r in env.web.requests)
    assert session["summary"]["x_post_reads"] == 4  # fixture posts returned

    opportunities = env.opportunities(session["id"])
    assert opportunities, session["summary"]
    assert [o["rank"] for o in opportunities] == list(range(1, len(opportunities) + 1))
    assert sum(o["grok"]["eligible"] for o in opportunities) <= 1
    headlines = " ".join(o["headline"] for o in opportunities)
    assert "protein" not in headlines.lower()  # off-topic: no vocabulary match
    assert "static site" not in headlines.lower()
    for opp in opportunities:
        assert {c["name"] for c in opp["components"]} >= {"topic_relevance", "timeliness"}
        if not any(e["role"] == "primary" for e in opp["evidence"]):
            assert opp["score"] <= 0.5  # discussion-only topics are capped
        items = env.client.get(f"/discovery/opportunities/{opp['id']}/items").json()
        for item in items:
            assert item["first_retrieved_at"] and item["publisher"] and item["source_id"]
            assert item["verification_status"] in ("primary_source", "unverified")
            if item["platform"] == "x":
                assert item["verification_status"] == "unverified"  # popularity proves nothing
    eval_opp = next(o for o in opportunities if "trace grading" in o["headline"].lower())
    roles = {e["role"] for e in eval_opp["evidence"]}
    assert roles == {"primary", "discussion"}  # the HN thread links the vendor post


def test_the_audience_shortlist_is_read_only_suggestions(env: Env):
    session = env.session()
    audience = env.client.get(f"/discovery/sessions/{session['id']}/audience").json()
    assert audience
    for entry in audience:
        assert entry["kind"] in ("account", "discussion")
        assert entry["explanation"]
        assert not {"follow", "dm", "message"} & {s["name"] for s in entry["signals"]}
    assert any(e["ref"].startswith("https://news.ycombinator.com/item?id=") for e in audience)


def test_caps_skip_sources_and_hard_limits_are_refused(env: Env):
    session = env.session(limits={"x_requests": 0, "feeds": 1, "github_requests": 0})
    got = outcomes(session)
    assert got["x-ai-engineering"] == "skipped"
    assert got["openai-news"] == "succeeded"
    assert got["deepmind-news"] == "skipped" and got["langgraph-releases"] == "skipped"
    skipped = {f["source_id"]: f["error_message"] for f in session["fetches"]}
    assert "cap" in skipped["deepmind-news"]
    assert env.x.calls == []
    too_many = env.client.post(
        "/discovery/sessions",
        json={"account_id": ACCOUNT.id, "requested_by": "p", "limits": {"x_requests": 50}},
        headers=AUTH,
    )
    assert too_many.status_code == 422
    unknown = env.client.post(
        "/discovery/sessions",
        json={"account_id": ACCOUNT.id, "requested_by": "p", "sources": ["nope"]},
        headers=AUTH,
    )
    assert unknown.status_code == 422


def test_cache_window_and_conditional_requests(env: Env):
    first = env.session()
    calls = len(env.web.requests)
    second = env.session()
    assert set(outcomes(second).values()) <= {"cached", "skipped"}
    assert len(env.web.requests) == calls  # served from the cache: no external call
    assert len(env.opportunities(second["id"])) >= 0
    third = env.session(refresh=True)
    got = outcomes(third)
    assert got["openai-news"] == "not_modified"  # If-None-Match answered with 304
    assert got["langgraph-releases"] == "not_modified"
    assert first["id"] != second["id"] != third["id"]


def test_a_failing_source_is_recorded_without_its_response_body(make_runtime, client_for):
    env = Env(make_runtime, client_for, fail=("openai.com",))
    session = env.session()
    assert session["status"] == "partial"
    failed = next(f for f in session["fetches"] if f["source_id"] == "openai-news")
    assert failed["outcome"] == "failed"
    assert failed["error_category"] == "server_error"
    assert "fixture outage" not in (failed["error_message"] or "")
    assert outcomes(session)["arxiv-cs-ai"] == "succeeded"


def test_a_run_from_an_opportunity_replays_the_stored_evidence(env: Env):
    session = env.session()
    opp = by_intent(env.opportunities(session["id"]), "news_commentary")
    before = len(env.web.requests), len(env.x.calls)
    response = env.start(opp["id"], "news_commentary")
    assert response.status_code == 202, response.text
    run_id = response.json()["run"]["id"]
    detail = env.client.get(f"/runs/{run_id}").json()
    assert detail["run"]["status"] == "awaiting_review", detail["run"]
    assert (len(env.web.requests), len(env.x.calls)) == before

    url = env.runtime.db.libpq_url
    assert sql(url, "SELECT opportunity_id FROM runs WHERE id = :r", r=run_id) == [(opp["id"],)]
    fetches = sql(
        url,
        "SELECT provider, requests_made, posts_fetched FROM research_fetches WHERE run_id = :r",
        r=run_id,
    )
    assert fetches == [("discovery_replay", 0, 0)]
    evidence = {e["item_id"] for e in opp["evidence"]}
    stored = {
        s for (s,) in sql(url, "SELECT source_id FROM source_posts WHERE run_id = :r", r=run_id)
    }
    assert stored <= evidence and stored
    kinds = sql(
        url, "SELECT claim_kind, verification FROM research_findings WHERE run_id = :r", r=run_id
    )
    assert kinds and all(k is not None for k, _ in kinds)
    intents = sql(
        url, "SELECT DISTINCT content_intent FROM content_candidates WHERE run_id = :r", r=run_id
    )
    assert intents == [("news_commentary",)]
    contents = sql(url, "SELECT content FROM content_candidates WHERE run_id = :r", r=run_id)
    assert all("@" not in str(c) for (c,) in contents)

    refreshed = env.client.get(f"/discovery/opportunities/{opp['id']}").json()
    assert refreshed["status"] == "used" and refreshed["run_ids"] == [run_id]
    usage = env.client.get(f"/runs/{run_id}/usage").json()
    replay = [
        line
        for line in usage["at_current_pricing"]["lines"]
        if line["provider"] == "discovery_replay"
    ]
    assert replay and all(line["quantity"] == 0 for line in replay)  # no billable reads


def test_intent_and_state_checks_when_starting_a_run(env: Env):
    session = env.session()
    opportunities = env.opportunities(session["id"])
    opp = opportunities[0]
    unsuggested = next(
        (
            i
            for i in ("research_summary", "news_commentary", "technical_explanation")
            if i not in opp["suggested_intents"]
        ),
        None,
    )
    if unsuggested is not None:
        assert env.start(opp["id"], unsuggested).status_code == 422
    not_grok = next((o for o in opportunities if not o["grok"]["eligible"]), None)
    if not_grok is not None:
        assert env.start(not_grok["id"], "grok_question").status_code == 422
    other = ACCOUNT.model_copy(update={"id": "acct_other"})
    wrong_account = env.client.post(
        f"/discovery/opportunities/{opp['id']}/runs",
        json={
            "account": other.model_dump(mode="json"),
            "intent": "original_insight",
            "reviewer": "p",
        },
        headers=AUTH,
    )
    assert wrong_account.status_code == 422
    dismissed = env.client.post(
        f"/discovery/opportunities/{opp['id']}/dismiss",
        json={"reviewer": "philipp", "reason": "not for us"},
        headers=AUTH,
    )
    assert dismissed.status_code == 200 and dismissed.json()["status"] == "dismissed"
    assert env.start(opp["id"], "original_insight").status_code == 409
    # a dismissed fingerprint is suppressed in the next session
    again = env.session(refresh=True)
    assert opp["fingerprint"] not in {o["fingerprint"] for o in env.opportunities(again["id"])}
    reasons = [s["reason"] for s in again["summary"]["suppressed"]]
    assert any(r.startswith("dismissed") for r in reasons)


def test_ranking_fields_of_an_opportunity_are_immutable(env: Env):
    session = env.session()
    opp = env.opportunities(session["id"])[0]
    url = env.runtime.db.libpq_url
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        sql(
            url,
            "UPDATE discovery_opportunities SET score = 1 WHERE id = :o RETURNING id",
            o=opp["id"],
        )
    with pytest.raises(DBAPIError):
        sql(url, "DELETE FROM discovery_items RETURNING id")
    with pytest.raises(DBAPIError):
        sql(url, "UPDATE discovery_item_sightings SET position = 9 RETURNING item_id")


def test_0007_applies_to_a_populated_phase_8_database(base_database_url):
    from alembic import command

    url = create_scratch_database(base_database_url, prefix="sga_phase8")
    try:
        command.upgrade(alembic_config(url), "0006")
        sql(
            url,
            "INSERT INTO runs (id, status, research_query, account, strategy, config, "
            "strategy_id, strategy_version, research_attempts, generation_attempts, "
            "regeneration_rounds, edit_rounds, review_candidate_ids) VALUES ('run_p8', "
            "'approved', 'q', '{}', '{}', '{}', 's', 1, 1, 1, 0, 0, '[]') RETURNING id",
        )
        upgrade(url)
        assert current_revision(url) == "0007"
        assert sql(url, "SELECT opportunity_id FROM runs") == [(None,)]
        assert sql(url, "SELECT count(*) FROM discovery_items") == [(0,)]
        command.downgrade(alembic_config(url), "0006")
        assert sql(url, "SELECT id FROM runs") == [("run_p8",)]
        upgrade(url)
        assert current_revision(url) == "0007"
    finally:
        drop_scratch_database(base_database_url, url)
