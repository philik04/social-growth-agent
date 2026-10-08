"""Restart and resume: a new process (new runtime, new provider objects, same database)
continues from the checkpoint and never repeats a completed X retrieval or generation."""

import pytest

from social_growth_agent.agents import CriticReport, ResearchReport
from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.graph import RunConfig
from social_growth_agent.models import ResearchQuery, RunStatus
from social_growth_agent.services.demo import demo_account
from tests.db.conftest import sql
from tests.db.helpers import CrashOnce, SimulatedCrash
from tests.x_fakes import FakeX, ok, sample_body

CONFIG = RunConfig(research_query=ResearchQuery(text="AI agents lang:en"))


def crash_during(make_runtime, schema, fake_x):
    """Process 1: run until the given LLM step, then die mid-call."""
    first = make_runtime(
        llm=CrashOnce(build_fake_llm(), crash_on=schema), research=fake_x.provider()
    )
    account, strategy = demo_account()
    with pytest.raises(SimulatedCrash):
        first.service.create_run(account, strategy, CONFIG)
    [row] = first.service.list_runs()
    first.close()
    return row.id


def test_restart_after_crash_does_not_refetch_from_x(make_runtime, client_for):
    first_x = FakeX(ok(sample_body(5)))
    run_id = crash_during(make_runtime, ResearchReport, first_x)
    assert len(first_x.requests) == 1

    # Process 2: new runtime, new X client and LLM; startup only flags the run.
    second_x = FakeX(ok(sample_body(5)))
    llm = build_fake_llm()
    second = make_runtime(llm=llm, research=second_x.provider())
    client = client_for(second)  # lifespan runs recover_stalled()
    run = client.get(f"/runs/{run_id}").json()["run"]
    assert run["status"] == "stalled"
    assert run["current_node"] == "retrieve"
    assert second_x.requests == [] and llm.calls == []  # nothing spent at startup

    response = client.post(f"/runs/{run_id}/resume")
    assert response.status_code == 202
    body = client.get(f"/runs/{run_id}").json()
    assert body["run"]["status"] == "awaiting_review"
    assert second_x.requests == []  # the completed X retrieval was not repeated
    assert len(body["research_fetches"]) == 1
    assert {p["source_id"] for p in body["source_posts"]} == {f"x_18000{i}" for i in range(5)}
    assert llm.calls[0].agent == "research"  # continued where it died
    nodes = [
        n
        for (n,) in sql(
            second.db.libpq_url,
            "SELECT node FROM run_events WHERE run_id = :id ORDER BY started_at",
            id=run_id,
        )
    ]
    assert nodes.count("retrieve") == 1


def test_restart_during_critique_does_not_regenerate(make_runtime, client_for):
    fake_x = FakeX(ok(sample_body(5)))
    run_id = crash_during(make_runtime, CriticReport, fake_x)

    llm = build_fake_llm()
    second = make_runtime(llm=llm, research=FakeX().provider())
    client = client_for(second)
    assert client.post(f"/runs/{run_id}/resume").status_code == 202

    assert client.get(f"/runs/{run_id}").json()["run"]["status"] == "awaiting_review"
    agents = [c.agent for c in llm.calls]
    assert agents[0] == "critic"  # the checkpointed generation was reused
    assert agents.count("research") == 0
    generated_in_process_2 = [c for c in llm.calls if c.agent == "content"]
    # fake critic revises attempt 1, so exactly one *new* generation (attempt 2) follows
    assert [c.payload["attempt"] for c in generated_in_process_2] == [2]


def test_review_after_restart_resumes_without_any_provider_calls(make_runtime, client_for):
    account, strategy = demo_account()
    first = make_runtime(research=FakeX(ok(sample_body(5))).provider())
    run_id = first.service.create_run(account, strategy, CONFIG).id
    candidate = first.service.get_run(run_id).review.candidate_ids[0]
    first.close()

    fake_x, llm = FakeX(), build_fake_llm()
    client = client_for(make_runtime(llm=llm, research=fake_x.provider()))
    pending = client.get("/reviews/pending").json()
    assert [p["run_id"] for p in pending] == [run_id]  # paused runs are not "stalled"

    response = client.post(
        f"/runs/{run_id}/review",
        json={"action": "approve", "candidate_id": candidate, "reviewer": "philipp"},
    )
    assert response.status_code == 202
    assert client.get(f"/runs/{run_id}").json()["run"]["status"] == "approved"
    assert fake_x.requests == [] and llm.calls == []


def test_stalled_run_that_was_paused_for_review_returns_to_review(make_runtime):
    account, strategy = demo_account()
    first = make_runtime()
    run_id = first.service.create_run(account, strategy).id
    # Simulate: a review was claimed (row -> running) and the process died before resuming.
    sql(
        first.db.libpq_url,
        "UPDATE runs SET status = 'running' WHERE id = :id RETURNING id",
        id=run_id,
    )
    first.close()

    llm = build_fake_llm()
    second = make_runtime(llm=llm)
    assert second.service.recover_stalled() == [run_id]
    assert second.service.resume(run_id).status is RunStatus.AWAITING_REVIEW
    assert llm.calls == []


def test_run_that_died_before_its_first_checkpoint_restarts_from_snapshots(make_runtime):
    account, strategy = demo_account()
    first = make_runtime()
    run_id = first.service.create_run(account, strategy).id
    sql(
        first.db.libpq_url,
        "DELETE FROM checkpoints WHERE thread_id = :id RETURNING thread_id",
        id=run_id,
    )
    sql(
        first.db.libpq_url,
        "UPDATE runs SET status = 'queued' WHERE id = :id RETURNING id",
        id=run_id,
    )
    first.close()

    second = make_runtime()
    second.service.recover_stalled()
    assert second.service.resume(run_id).status is RunStatus.AWAITING_REVIEW
