"""Phase 9 on PostgreSQL: @grok questions stay manual at every gate, export, verified
manual-publication registration (idempotent), analytics and lineage, experiments."""

from typing import Any

import pytest

from social_growth_agent.models.strategy import ReviewAuthMethod
from social_growth_agent.persistence import Database
from social_growth_agent.providers.mocks import MockPublisher
from social_growth_agent.services.publisher import PublisherWorker
from tests.db.conftest import sql
from tests.db.discovery_helpers import OWNER_ID, Env, by_intent
from tests.db.publishing_helpers import approved_run, execute
from tests.db.strategy_helpers import AUTH

POST_ID = "1950000000000000001"
POST_URL = f"https://x.com/p7_account/status/{POST_ID}"


@pytest.fixture
def env(make_runtime, client_for) -> Env:
    return Env(make_runtime, client_for)


def grok_run(env: Env) -> tuple[str, str, str]:
    """A discovery session, a Grok-question run on the eligible opportunity, approved.
    Returns (run_id, candidate_id, content)."""
    session = env.session()
    opp = by_intent(env.opportunities(session["id"]), "grok_question")
    response = env.start(opp["id"], "grok_question")
    assert response.status_code == 202, response.text
    run_id = response.json()["run"]["id"]
    candidate_id = env.approve(run_id)
    [(content,)] = sql(
        env.runtime.db.libpq_url,
        "SELECT content FROM content_candidates WHERE id = :c",
        c=candidate_id,
    )
    return run_id, candidate_id, str(content)


def export(env: Env, run_id: str, candidate_id: str) -> Any:
    return env.client.post(
        f"/runs/{run_id}/candidates/{candidate_id}/export",
        json={"exported_by": "philipp"},
        headers=AUTH,
    )


def register(env: Env, run_id: str, candidate_id: str, post: str = POST_URL) -> Any:
    return env.client.post(
        f"/runs/{run_id}/candidates/{candidate_id}/manual-publication",
        json={"post": post, "registered_by": "philipp"},
        headers=AUTH,
    )


def test_a_grok_question_is_drafted_once_and_refused_by_every_automated_path(
    make_runtime, client_for
):
    publisher = MockPublisher()
    env = Env(make_runtime, client_for)
    env.runtime.worker = PublisherWorker(db=env.runtime.db, publisher=publisher)
    run_id, candidate_id, content = grok_run(env)
    assert content.startswith("@grok ") and content.count("@") == 1 and "?" in content
    shown = env.client.get(f"/runs/{run_id}").json()["candidates"]
    assert next(c for c in shown if c["id"] == candidate_id)["manual_only"] is True
    url = env.runtime.db.libpq_url
    assert sql(
        url, "SELECT content_intent FROM content_candidates WHERE id = :c", c=candidate_id
    ) == [("grok_question",)]
    # the publish request refuses it (and records nothing)
    response = env.client.post(
        f"/runs/{run_id}/publish", json={"candidate_id": candidate_id, "requested_by": "philipp"}
    )
    assert response.status_code == 409 and "manual publication only" in response.json()["detail"]
    assert sql(url, "SELECT count(*) FROM publications WHERE run_id = :r", r=run_id) == [(0,)]
    # an intent smuggled into the table is refused by the worker before any call
    execute(
        url,
        "INSERT INTO publications (id, run_id, candidate_id, platform, idempotency_key, content, "
        "content_sha256, status, attempt_count) VALUES ('pub_smuggled', :r, :c, 'x', 'k1', "
        ":content, 'h', 'ready', 0)",
        r=run_id,
        c=candidate_id,
        content=content,
    )
    PublisherWorker(db=Database.connect(url), publisher=publisher).run_once()
    assert publisher.calls == []
    assert sql(
        url, "SELECT status, failure_category FROM publications WHERE id = 'pub_smuggled'"
    ) == [("failed", "manual_only")]


def test_an_approved_candidate_is_exported_and_its_manual_post_registered_once(env: Env):
    run_id, candidate_id, content = grok_run(env)
    first = export(env, run_id, candidate_id)
    assert first.status_code == 201, first.text
    exported = first.json()
    assert exported["manual_only"] is True and exported["content"] == content
    assert exported["content_intent"] == "grok_question"
    assert exported["intent_url"].startswith("https://x.com/intent/post?text=%40grok%20")
    again = export(env, run_id, candidate_id)
    assert again.json()["id"] == exported["id"]  # idempotent for unchanged content
    # once exported, the API publish path refuses it too (it would post twice)
    blocked = env.client.post(
        f"/runs/{run_id}/publish", json={"candidate_id": candidate_id, "requested_by": "philipp"}
    )
    assert blocked.status_code == 409

    env.post_as_owner(POST_ID, content.replace(" ", "  ", 1))  # X may differ in whitespace
    registered = register(env, run_id, candidate_id)
    assert registered.status_code == 201, registered.text
    body = registered.json()
    assert body["already_registered"] is False and body["text_match"] == "normalized"
    assert body["author_id"] == OWNER_ID and body["analytics_jobs"] >= 1
    url = env.runtime.db.libpq_url
    assert sql(
        url,
        "SELECT status, provider, provider_post_id FROM publications WHERE run_id = :r",
        r=run_id,
    ) == [("published", "manual", POST_ID)]
    jobs = sql(
        url,
        "SELECT count(*) FROM analytics_jobs WHERE publication_id = :p",
        p=body["publication_id"],
    )
    assert jobs[0][0] == body["analytics_jobs"]

    # registering again (by URL or id) changes nothing and calls nothing
    lookups = len(env.verifier.lookups)
    for ref in (POST_URL, POST_ID):
        repeat = register(env, run_id, candidate_id, ref)
        assert repeat.status_code == 201 and repeat.json()["already_registered"] is True
        assert repeat.json()["id"] == body["id"]
    assert len(env.verifier.lookups) == lookups
    assert sql(url, "SELECT count(*) FROM publications WHERE run_id = :r", r=run_id) == [(1,)]
    assert sql(url, "SELECT count(*) FROM manual_publications") == [(1,)]

    lineage = env.client.get(f"/publications/{body['publication_id']}/lineage").json()
    assert lineage["discovery"]["manual_publication_id"] == body["id"]
    assert lineage["discovery"]["opportunity_id"] is not None
    assert lineage["discovery"]["evidence"]
    assert lineage["candidate"]["content_intent"] == "grok_question"
    usage = env.client.get(f"/runs/{run_id}/usage").json()
    assert usage["usage"]["verification"][0]["post_reads"] == 1
    assert usage["usage"]["publishing"] == []  # a person posted it: no API create cost


def test_registration_refuses_other_accounts_other_text_and_unknown_posts(env: Env):
    run_id, candidate_id, content = grok_run(env)
    assert register(env, run_id, candidate_id).status_code == 409  # not exported yet
    assert export(env, run_id, candidate_id).status_code == 201
    assert register(env, run_id, candidate_id).status_code == 409  # post not found
    env.post_as_owner(POST_ID, content, author_id="999")
    refused = register(env, run_id, candidate_id)
    assert refused.status_code == 409 and "another account" in refused.json()["detail"]
    env.post_as_owner("1950000000000000002", content + " edited")
    refused = register(env, run_id, candidate_id, "1950000000000000002")
    assert refused.status_code == 409 and "differs" in refused.json()["detail"]
    assert register(env, run_id, candidate_id, "https://evil.example/status/1").status_code == 422
    url = env.runtime.db.libpq_url
    assert sql(url, "SELECT count(*) FROM publications WHERE run_id = :r", r=run_id) == [(0,)]


def test_one_post_cannot_be_registered_for_two_candidates(env: Env, run_payload):
    run_id, candidate_id, content = grok_run(env)
    assert export(env, run_id, candidate_id).status_code == 201
    env.post_as_owner(POST_ID, content)
    assert register(env, run_id, candidate_id).status_code == 201
    other_run, other_candidate = approved_run(env.client, run_payload)
    assert export(env, other_run, other_candidate).status_code == 201
    refused = register(env, other_run, other_candidate)
    assert refused.status_code == 409 and "another candidate" in refused.json()["detail"]


def test_registration_without_a_configured_account_is_unavailable(make_runtime, client_for):
    env = Env(make_runtime, client_for)
    env.runtime.discovery._config = type(env.runtime.discovery._config)()  # no account
    run_id, candidate_id, _ = grok_run(env)
    assert export(env, run_id, candidate_id).status_code == 201
    assert register(env, run_id, candidate_id).status_code == 503


def test_grok_questions_are_excluded_from_experiments(env: Env):
    from social_growth_agent.errors import InvalidExperimentError
    from social_growth_agent.graph.state import RunConfig
    from social_growth_agent.models import ContentIntent
    from social_growth_agent.policies.content_policy import ContentPolicy
    from social_growth_agent.services.experiments import ExperimentRunBody
    from tests.db.strategy_helpers import ACCOUNT

    body = ExperimentRunBody(
        account=ACCOUNT,
        config=RunConfig(content_policy=ContentPolicy(content_intent=ContentIntent.GROK_QUESTION)),
        reviewer="philipp",
    )
    assert env.runtime.experiments is not None
    try:
        env.runtime.experiments.create_run("exp_missing", body, auth=_auth())
    except InvalidExperimentError as exc:
        assert "excluded" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected InvalidExperimentError")


def _auth() -> ReviewAuthMethod:
    return ReviewAuthMethod.REVIEW_TOKEN


def test_a_plain_discovery_post_can_be_exported_or_published_but_not_both(env: Env):
    session = env.session()
    opp = by_intent(env.opportunities(session["id"]), "news_commentary")
    run_id = env.start(opp["id"], "news_commentary").json()["run"]["id"]
    candidate_id = env.approve(run_id)
    published = env.client.post(
        f"/runs/{run_id}/publish", json={"candidate_id": candidate_id, "requested_by": "philipp"}
    )
    assert published.status_code == 202, published.text
    refused = export(env, run_id, candidate_id)
    assert refused.status_code == 409 and "already exists" in refused.json()["detail"]
