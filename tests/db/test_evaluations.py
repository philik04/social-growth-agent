"""Evaluations on PostgreSQL (Phase 7): evidence packages, lineage, reproducibility, cutoff."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import DBAPIError

from social_growth_agent.analytics.evaluation import EvaluationConfig, evaluate, evaluation_digest
from social_growth_agent.models import HookType, utc_now
from social_growth_agent.persistence import evaluations as edb
from social_growth_agent.services.strategy_seed import SeedPost, seed_posts
from tests.db.analytics_helpers import count, published_runtime
from tests.db.conftest import sql
from tests.db.strategy_helpers import (
    ACCOUNT,
    V1,
    create_evaluation,
    seed_comparable,
    strategy_runtime,
)


@pytest.fixture
def runtime(make_runtime):
    return strategy_runtime(make_runtime)


@pytest.fixture
def client(runtime, client_for):
    return client_for(runtime)


def test_an_evaluation_stores_an_immutable_evidence_package(runtime, client, clean_db):
    ids = seed_comparable(runtime)
    ev = create_evaluation(client)

    assert ev["status"] == "sufficient"
    assert (ev["eligible_count"], ev["excluded_count"], ev["posts_considered"]) == (22, 0, 22)
    assert ev["platform_reads"] == 0 and ev["llm_calls"] == 0
    assert ev["config"]["config_version"] == "eval-v1" and len(ev["digest"]) == 64
    [cmp] = [c for c in ev["summary"]["comparisons"] if c["dimension"] == "hook_type"]
    assert cmp["comparison_id"] == "cmp:hook_type:how_to:vs:number"
    assert count(clean_db, "evaluation_observations") == len(ids)
    # the evaluation read nothing from the platform and called no model
    assert count(clean_db, "analytics_requests", "worker_id <> 'seed'") == 0
    assert count(clean_db, "llm_calls") == 0
    assert count(clean_db, "strategy_proposals") == 0


def test_observations_list_excluded_posts_with_reasons(runtime, client):
    seed_comparable(runtime, per_hook=3)
    start = utc_now() - timedelta(days=4)
    seed_posts(
        runtime.db,
        [
            SeedPost(
                ACCOUNT, V1, HookType.STORY, start, {"likes": 9}, capture_offset_seconds=20_000
            ),
            SeedPost(ACCOUNT, V1, HookType.STORY, start, None),
            SeedPost(ACCOUNT, V1, HookType.STORY, utc_now() - timedelta(hours=2), None),
            SeedPost(ACCOUNT, V1, HookType.STORY, start, {"likes": 9}, provider="x"),
        ],
    )
    ev = create_evaluation(client)
    assert ev["status"] == "insufficient_evidence" and ev["summary"]["comparisons"] == []
    assert ev["summary"]["excluded_by_reason"] == {
        "no_snapshot": 1,
        "not_yet_due": 1,
        "off_target_late": 1,
        "real_metrics_in_synthetic_evaluation": 1,
    }
    excluded = client.get(
        f"/evaluations/{ev['id']}/observations", params={"eligible": False}
    ).json()
    assert len(excluded) == 4 and all(o["exclusion_reason"] for o in excluded)
    late = next(o for o in excluded if o["exclusion_reason"] == "off_target_late")
    assert late["likes"] == 9 and late["post_metric_id"] is not None


def test_every_observation_leads_back_through_the_lineage(
    make_runtime, client_for, run_payload, clean_db
):
    runtime = strategy_runtime(make_runtime)
    client = client_for(runtime)
    _r, _c, [publication_id] = published_runtime(make_runtime, client_for, run_payload)
    [(account,)] = sql(clean_db, "SELECT account->>'id' FROM runs LIMIT 1")
    ev = create_evaluation(client, account_id=account, synthetic=False, snapshot_age="1h")
    [o] = client.get(f"/evaluations/{ev['id']}/observations").json()
    assert o["publication_id"] == publication_id
    assert o["exclusion_reason"] == "not_yet_due"  # published just now
    lineage = client.get(f"/publications/{publication_id}/lineage").json()
    assert lineage["candidate"]["id"] == o["candidate_id"]
    assert lineage["strategy_id"] == o["strategy_id"]
    assert lineage["findings"] and lineage["source_posts"]


def test_the_same_request_returns_the_same_evaluation_and_digest(runtime, client, clean_db):
    seed_comparable(runtime)
    cutoff = utc_now().isoformat()
    first = client.post(
        "/evaluations",
        json={
            "account_id": ACCOUNT.id,
            "snapshot_age": "24h",
            "synthetic": True,
            "data_cutoff": cutoff,
        },
    )
    again = client.post(
        "/evaluations",
        json={
            "account_id": ACCOUNT.id,
            "snapshot_age": "24H",
            "synthetic": True,
            "data_cutoff": cutoff,
        },
    )
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["created"] is False
    assert again.json()["evaluation"]["id"] == first.json()["evaluation"]["id"]
    assert count(clean_db, "performance_evaluations") == 1


def test_recomputing_after_new_data_reproduces_the_stored_digest(runtime, client):
    seed_comparable(runtime)
    cutoff = utc_now()
    # published before the cutoff, but its 24h snapshot is captured after it
    seed_posts(
        runtime.db,
        [SeedPost(ACCOUNT, V1, HookType.NUMBER, cutoff - timedelta(hours=23), {"likes": 1})],
    )
    ev = create_evaluation(client, data_cutoff=cutoff.isoformat())
    assert ev["summary"]["excluded_by_reason"] == {"not_yet_due": 1}
    # new data after the evaluation: a post published after the cutoff
    seed_posts(
        runtime.db,
        [SeedPost(ACCOUNT, V1, HookType.NUMBER, cutoff + timedelta(seconds=1), {"likes": 7})],
    )
    with runtime.db.transaction() as session:
        row = edb.get(session, ev["id"])
        config = EvaluationConfig.model_validate(row.config)
        posts = edb.load_posts(
            session,
            account_id=ACCOUNT.id,
            snapshot_age="24h",
            cutoff=row.data_cutoff,
            since=None,
            until=None,
            strategy_id=None,
        )
        result = evaluate(posts, config, row.data_cutoff)
        assert evaluation_digest(row.request, config, result) == row.digest == ev["digest"]
    # the same request through the API returns the stored evaluation unchanged
    again = create_evaluation(client, data_cutoff=cutoff.isoformat())
    assert again["id"] == ev["id"] and again["digest"] == ev["digest"]


def test_cutoff_in_the_future_and_unknown_ages_are_rejected(client):
    future = (utc_now() + timedelta(hours=1)).isoformat()
    body = {"account_id": ACCOUNT.id, "snapshot_age": "24h"}
    assert client.post("/evaluations", json=body | {"data_cutoff": future}).status_code == 422
    assert client.post("/evaluations", json=body | {"snapshot_age": "5h"}).status_code == 422
    naive = datetime(2026, 10, 1).isoformat()
    assert client.post("/evaluations", json=body | {"data_cutoff": naive}).status_code == 422
    assert client.post("/evaluations", json=body | {"min_group_n": 1}).status_code == 422
    assert client.post("/evaluations", json=body | {"extra": 1}).status_code == 422
    assert client.get("/evaluations/eval_missing").status_code == 404


def test_synthetic_metrics_never_enter_a_real_evaluation(runtime, client):
    seed_comparable(runtime)
    real = create_evaluation(client, synthetic=False)
    assert real["eligible_count"] == 0
    assert real["summary"]["excluded_by_reason"] == {"synthetic_metrics": 22}
    seed_comparable(runtime, provider="x")
    real = create_evaluation(client, synthetic=False)
    assert real["eligible_count"] == 22 and real["status"] == "sufficient"


def test_evaluation_rows_are_append_only(runtime, client, clean_db):
    seed_comparable(runtime, per_hook=2)
    ev = create_evaluation(client)
    for statement in (
        "UPDATE performance_evaluations SET status = 'sufficient'",
        "DELETE FROM performance_evaluations",
        "UPDATE evaluation_observations SET likes = 1000",
        "DELETE FROM evaluation_observations",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            sql(clean_db, statement)
    assert client.get(f"/evaluations/{ev['id']}").json()["digest"] == ev["digest"]


def test_reading_evaluations_writes_nothing(runtime, client, clean_db):
    seed_comparable(runtime, per_hook=2)
    ev = create_evaluation(client)
    tables = (
        "performance_evaluations",
        "evaluation_observations",
        "strategy_proposals",
        "llm_calls",
        "analytics_requests",
    )
    before = {t: count(clean_db, t) for t in tables}
    assert client.get("/evaluations", params={"account_id": ACCOUNT.id}).status_code == 200
    assert client.get(f"/evaluations/{ev['id']}").status_code == 200
    assert client.get(f"/evaluations/{ev['id']}/observations").status_code == 200
    assert {t: count(clean_db, t) for t in tables} == before


def test_window_and_strategy_scope_filter_the_population(runtime, client):
    ids = seed_comparable(runtime, per_hook=2)
    assert len(ids) == 4
    since = (utc_now() - timedelta(days=20) + timedelta(hours=6)).isoformat()
    windowed = create_evaluation(client, published_since=since)
    assert windowed["posts_considered"] == 2  # posts are 5 hours apart
    scoped = create_evaluation(client, strategy_id="strat_other")
    assert scoped["posts_considered"] == 0
    assert datetime.fromisoformat(scoped["data_cutoff"]).tzinfo is not None
