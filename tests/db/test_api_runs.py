"""The run API against real PostgreSQL: persistence, provenance, review flow, errors."""

import pytest
from sqlalchemy import create_engine, pool, text
from sqlalchemy.exc import IntegrityError

from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.models import CritiqueVerdict
from social_growth_agent.persistence.db import sqlalchemy_url
from tests.conftest import always, make_llm
from tests.db.conftest import sql
from tests.x_fakes import FakeX, full_metrics, ok, search_body, x_post


def start(client, payload, **config):
    response = client.post("/runs", json=payload(**config))
    assert response.status_code == 202, response.text
    return response.json()


def detail(client, run_id):
    response = client.get(f"/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def review(client, run_id, **body):
    return client.post(f"/runs/{run_id}/review", json={"reviewer": "philipp", **body})


def x_body():
    posts = [
        x_post("1801", "Agent evals: 3 lessons", "u1", metrics=full_metrics(likes=40)),
        x_post("1802", "LLM evaluation in CI", "u2", metrics=full_metrics(impressions=None)),
        x_post("1803", "Shipping agent tools", "u3"),  # no metrics at all
    ]
    users = [{"id": f"u{i}", "username": f"dev_{i}", "name": "D"} for i in (1, 2, 3)]
    return search_body(posts, users)


# --- 1, 2. create and fetch ------------------------------------------------------------


def test_create_run_persists_snapshots_and_returns_202(make_runtime, client_for, run_payload):
    runtime = make_runtime()
    client = client_for(runtime)
    created = start(client, run_payload, max_regenerations=1)

    assert created["id"].startswith("run_")
    [(status, account, strategy, config, pricing_id)] = sql(
        runtime.db.libpq_url,
        "SELECT status, account, strategy, config, pricing_id FROM runs WHERE id = :id",
        id=created["id"],
    )
    assert status == "awaiting_review"  # inline executor: already paused for review
    assert account["handle"] == "@tester"
    assert strategy["id"] == "strat_test"
    assert config["max_regenerations"] == 1
    assert pricing_id.startswith("test-1:")


def test_status_fetch_returns_research_candidates_critiques_and_usage(
    make_runtime, client_for, run_payload
):
    client = client_for(make_runtime())
    run_id = start(client, run_payload)["id"]
    body = detail(client, run_id)

    assert body["run"]["status"] == "awaiting_review"
    assert body["run"]["current_node"] == "request_review"
    assert body["run"]["failure"] is None
    research = body["research"]
    assert research["provider"] == "mock_fixtures" and research["synthetic"] is True
    assert research["findings"] and all(f["evidence_source_ids"] for f in research["findings"])
    assert body["candidates"] and all(c["critiques"] for c in body["candidates"])
    assert body["review"]["awaiting_review"] is True
    assert len(body["review"]["candidate_ids"]) >= 1
    assert body["usage"]["llm"][0]["calls"] == 5  # research, generate x2, critic x2
    assert body["cost"]["estimated"] is True


# --- 3, 4, 5. provenance ---------------------------------------------------------------


def test_source_posts_are_persisted_with_provenance_and_missing_metrics(
    make_runtime, client_for, run_payload
):
    fake = FakeX(ok(x_body()))
    client = client_for(make_runtime(research=fake.provider()))
    run_id = start(client, run_payload, research_query={"text": "AI agents"})["id"]
    body = detail(client, run_id)

    posts = {p["source_id"]: p for p in body["source_posts"]}
    assert set(posts) == {"x_1801", "x_1802", "x_1803"}
    assert posts["x_1801"]["likes"] == 40 and posts["x_1801"]["impressions"] == 1000
    assert posts["x_1802"]["impressions"] is None  # unknown stays unknown, not 0
    assert posts["x_1803"]["likes"] is None
    assert all(p["query"] == "AI agents" for p in posts.values())
    [fetch] = body["research_fetches"]
    assert fetch["effective_query"] == "AI agents -is:retweet"
    assert (fetch["requests_made"], fetch["posts_fetched"], fetch["users_fetched"]) == (1, 3, 3)


def test_finding_evidence_must_reference_a_post_retrieved_by_the_same_run(
    make_runtime, client_for, run_payload
):
    runtime = make_runtime()
    client = client_for(runtime)
    run_id = start(client, run_payload)["id"]
    url = runtime.db.libpq_url

    rows = sql(
        url,
        "SELECT e.source_id, p.text FROM finding_evidence e "
        "JOIN source_posts p ON p.run_id = e.run_id AND p.source_id = e.source_id "
        "WHERE e.run_id = :id",
        id=run_id,
    )
    assert rows and all(text_ for _, text_ in rows)

    [(finding_id,)] = sql(
        url, "SELECT id FROM research_findings WHERE run_id = :id LIMIT 1", id=run_id
    )
    engine = create_engine(sqlalchemy_url(url), poolclass=pool.NullPool)
    insert = text(
        "INSERT INTO finding_evidence (finding_id, source_id, run_id, position) "
        "VALUES (:f, 'x_never_retrieved', :r, 9)"
    )
    with (
        pytest.raises(IntegrityError, match="fk_finding_evidence_source_post"),
        engine.begin() as conn,
    ):
        conn.execute(insert, {"f": finding_id, "r": run_id})
    engine.dispose()


def test_candidate_critique_finding_post_chain_is_queryable(make_runtime, client_for, run_payload):
    runtime = make_runtime()
    client = client_for(runtime)
    run_id = start(client, run_payload)["id"]

    chain = sql(
        runtime.db.libpq_url,
        """
        SELECT c.id, c.generation_attempt, k.verdict, f.id, p.source_id
        FROM content_candidates c
        JOIN critiques k ON k.candidate_id = c.id
        JOIN candidate_findings cf ON cf.candidate_id = c.id
        JOIN research_findings f ON f.id = cf.finding_id
        JOIN finding_evidence e ON e.finding_id = f.id
        JOIN source_posts p ON p.run_id = e.run_id AND p.source_id = e.source_id
        WHERE c.run_id = :id
        """,
        id=run_id,
    )
    attempts = {row[1] for row in chain}
    assert attempts == {1, 2}  # fake agents: attempt 1 is revised, attempt 2 passes
    assert {row[2] for row in chain} >= {"pass", "revise"}
    revised = sql(
        runtime.db.libpq_url,
        "SELECT count(*) FROM content_candidates "
        "WHERE run_id = :id AND revises_candidate_id IS NOT NULL",
        id=run_id,
    )
    assert revised[0][0] >= 1


# --- 6-11. review flow ----------------------------------------------------------------


def test_pending_reviews_lists_only_runs_awaiting_review(make_runtime, client_for, run_payload):
    client = client_for(make_runtime())
    waiting = start(client, run_payload)["id"]
    done = start(client, run_payload)["id"]
    assert review(client, done, action="reject").status_code == 202

    pending = client.get("/reviews/pending").json()
    assert [p["run_id"] for p in pending] == [waiting]
    [entry] = pending
    assert entry["candidates"] and all(
        c["critiques"][-1]["verdict"] == "pass" for c in entry["candidates"]
    )
    assert entry["regenerations_remaining"] == 2


def test_approve_resumes_and_completes_without_new_model_calls(
    make_runtime, client_for, run_payload
):
    llm = build_fake_llm()
    client = client_for(make_runtime(llm=llm))
    run_id = start(client, run_payload)["id"]
    candidate = detail(client, run_id)["review"]["candidate_ids"][0]
    calls_before = len(llm.calls)

    response = review(client, run_id, action="approve", candidate_id=candidate)
    assert response.status_code == 202
    body = detail(client, run_id)
    assert body["run"]["status"] == "approved"
    [decision] = body["review"]["decisions"]
    assert (decision["action"], decision["candidate_id"]) == ("approve", candidate)
    assert len(llm.calls) == calls_before


def test_reject_is_terminal(make_runtime, client_for, run_payload):
    client = client_for(make_runtime())
    run_id = start(client, run_payload)["id"]

    assert review(client, run_id, action="reject", note="off-topic").status_code == 202
    assert detail(client, run_id)["run"]["status"] == "rejected"
    again = review(client, run_id, action="regenerate", note="try again")
    assert again.status_code == 409


def test_edit_is_recritiqued_before_it_can_be_approved(make_runtime, client_for, run_payload):
    llm = make_llm(always(CritiqueVerdict.PASS))
    runtime = make_runtime(llm=llm)
    client = client_for(runtime)
    run_id = start(client, run_payload)["id"]
    original = detail(client, run_id)["review"]["candidate_ids"][0]

    edited_text = "3 checks before you ship an agent: evals, tracing, budgets."
    response = review(
        client, run_id, action="edit", candidate_id=original, edited_content=edited_text
    )
    assert response.status_code == 202
    body = detail(client, run_id)
    edited = next(c for c in body["candidates"] if c["origin"] == "human_edit")
    assert edited["content"] == edited_text
    assert edited["revises_candidate_id"] == original
    assert len(edited["critiques"]) == 1  # re-critiqued
    [decision] = body["review"]["decisions"]
    assert decision["resulting_candidate_id"] == edited["id"]
    assert body["run"]["status"] == "awaiting_review"
    assert edited["id"] in body["review"]["candidate_ids"]

    assert review(client, run_id, action="approve", candidate_id=edited["id"]).status_code == 202
    assert detail(client, run_id)["run"]["status"] == "approved"


def test_regenerate_persists_reviewer_notes(make_runtime, client_for, run_payload):
    runtime = make_runtime()
    client = client_for(runtime)
    run_id = start(client, run_payload)["id"]
    reviewed = detail(client, run_id)["review"]["candidate_ids"]

    note = "Shorter, and lead with a number."
    assert review(client, run_id, action="regenerate", note=note).status_code == 202
    body = detail(client, run_id)
    [decision] = body["review"]["decisions"]
    assert (decision["action"], decision["note"]) == ("regenerate", note)
    assert body["run"]["regeneration_rounds"] == 1
    assert body["review"]["regenerations_remaining"] == 1
    assert not set(body["review"]["candidate_ids"]) & set(reviewed)  # new candidates
    # the previous critiques are still available
    old = [c for c in body["candidates"] if c["id"] in reviewed]
    assert old and all(c["critiques"] for c in old)


def test_regenerate_passes_notes_to_the_next_generation(make_runtime, client_for, run_payload):
    llm = build_fake_llm()
    fetches_before = None
    runtime = make_runtime(llm=llm)
    client = client_for(runtime)
    run_id = start(client, run_payload)["id"]
    fetches_before = detail(client, run_id)["research_fetches"]

    note = "Mention evaluation budgets"
    review(client, run_id, action="regenerate", note=note)
    content_calls = [c for c in llm.calls if c.agent == "content"]
    assert content_calls[-1].payload["reviewer_notes"] == [note]
    body = detail(client, run_id)
    newest = [c for c in body["candidates"] if c["id"] in body["review"]["candidate_ids"]]
    assert any(note[:40] in c["content"] for c in newest)
    assert body["research_fetches"] == fetches_before  # research is not repeated


# --- 16, 17. usage and cost ------------------------------------------------------------


def test_usage_ledger_records_failed_provider_calls(make_runtime, client_for, run_payload):
    from social_growth_agent.errors import ProviderError

    llm = make_llm(always(CritiqueVerdict.PASS), fail_first=1, failure=ProviderError)
    runtime = make_runtime(llm=llm)
    client = client_for(runtime)
    run_id = start(client, run_payload)["id"]

    body = detail(client, run_id)
    assert body["run"]["status"] == "failed"
    assert body["run"]["failure"]["error_type"] == "ProviderError"
    rows = sql(
        runtime.db.libpq_url, "SELECT agent, outcome FROM llm_calls WHERE run_id = :id", id=run_id
    )
    assert rows == [("research", "provider_error")]
    assert body["usage"]["research"][0]["post_reads"] == 6


# --- 19, 20, 22, 23. errors -----------------------------------------------------------


def test_invalid_state_transitions_return_409(make_runtime, client_for, run_payload):
    client = client_for(make_runtime())
    run_id = start(client, run_payload, max_regenerations=0)["id"]
    candidate = detail(client, run_id)["review"]["candidate_ids"][0]

    limit = review(client, run_id, action="regenerate", note="again")
    assert limit.status_code == 409 and "regeneration limit" in limit.json()["detail"]
    unknown = review(client, run_id, action="approve", candidate_id="cand_unknown")
    assert unknown.status_code == 409
    assert detail(client, run_id)["run"]["status"] == "awaiting_review"  # still pending

    assert client.post(f"/runs/{run_id}/resume").status_code == 409  # not stalled
    assert review(client, run_id, action="approve", candidate_id=candidate).status_code == 202
    assert review(client, run_id, action="approve", candidate_id=candidate).status_code == 409


def test_unknown_run_returns_404(make_runtime, client_for):
    client = client_for(make_runtime())
    assert client.get("/runs/run_missing").status_code == 404
    assert client.get("/runs/run_missing/usage").status_code == 404
    assert client.post("/runs/run_missing/resume").status_code == 404
    response = review(client, "run_missing", action="reject")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"action": "approve"},  # approve requires candidate_id
        {"action": "edit", "candidate_id": "c"},  # edit requires content
        {"action": "approve", "candidate_id": "c", "edited_content": "x"},
        {"action": "publish", "candidate_id": "c"},
        {"action": "reject", "note": "n" * 1001},
        {"action": "reject", "unexpected": True},
    ],
)
def test_invalid_review_payload_returns_422(make_runtime, client_for, run_payload, body):
    client = client_for(make_runtime())
    run_id = start(client, run_payload)["id"]
    assert review(client, run_id, **body).status_code == 422


def test_invalid_run_payload_returns_422(make_runtime, client_for, run_payload):
    client = client_for(make_runtime())
    payload = run_payload()
    payload["strategy"]["account_id"] = "acct_other"
    assert client.post("/runs", json=payload).status_code == 422
    assert client.post("/runs", json=run_payload(max_generation_attempts=99)).status_code == 422


def test_list_runs_filters_by_status(make_runtime, client_for, run_payload):
    client = client_for(make_runtime())
    a = start(client, run_payload)["id"]
    b = start(client, run_payload)["id"]
    review(client, b, action="reject")

    assert {r["id"] for r in client.get("/runs").json()} == {a, b}
    assert [r["id"] for r in client.get("/runs", params={"status": "rejected"}).json()] == [b]
    assert client.get("/runs", params={"status": "bogus"}).status_code == 422
