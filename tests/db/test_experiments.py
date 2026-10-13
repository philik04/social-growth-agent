"""Experiments (Phase 8) on PostgreSQL: lifecycle, authentication, randomization under
concurrency, masking, conclusion, immutability, adoption, lineage, usage, demo, schema."""

import random
import threading
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from social_growth_agent.models import HookType, utc_now
from social_growth_agent.models.strategy import ReviewAuthMethod
from social_growth_agent.persistence.migrate import alembic_config, current_revision, upgrade
from social_growth_agent.services import experiment_demo
from social_growth_agent.services.experiment_seed import (
    SeedOutcome,
    backdate_start,
    rate_outcome,
    seed_experiment_runs,
)
from social_growth_agent.services.experiments import ExperimentRunBody
from social_growth_agent.services.runtime import Runtime
from tests.db.analytics_helpers import count
from tests.db.conftest import create_scratch_database, drop_scratch_database, sql
from tests.db.strategy_helpers import (
    ACCOUNT,
    AUTH,
    TOKEN,
    V1,
    register_and_activate,
    strategy_runtime,
)
from tests.experiment_helpers import hook_design, length_design

POSITIVE = {"number": 0.04, "question": 0.07}


def design_json(**overrides: Any) -> dict[str, Any]:
    return hook_design(**overrides).model_dump(mode="json")


def setup(make_runtime, client_for):
    runtime = strategy_runtime(make_runtime)
    client = client_for(runtime)
    register_and_activate(client)
    return runtime, client


def create(client, **overrides: Any) -> dict[str, Any]:
    response = client.post(
        "/experiments",
        json={"account_id": ACCOUNT.id, "design": design_json(**overrides), "reviewer": "philipp"},
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def act(client, experiment_id: str, action: str, **body: Any) -> Any:
    return client.post(
        f"/experiments/{experiment_id}/{action}",
        json={"reviewer": "philipp"} | body,
        headers=AUTH,
    )


def running(client, **overrides: Any) -> dict[str, Any]:
    x = create(client, **overrides)
    assert act(client, x["id"], "approve").status_code == 200
    assert act(client, x["id"], "start").status_code == 200
    return x


def seed(
    runtime: Runtime,
    experiment_id: str,
    count_: int,
    outcome: Any = None,
    *,
    provider: str = "mock_analytics",
    seed_value: int = 7,
) -> list[str]:
    backdate_start(runtime.db, experiment_id, utc_now() - timedelta(days=40))
    return seed_experiment_runs(
        runtime.db,
        experiment_id,
        account=ACCOUNT,
        count=count_,
        outcome=outcome or rate_outcome(POSITIVE),
        start=utc_now() - timedelta(days=38),
        spacing=timedelta(hours=12),
        rng=random.Random(seed_value),
        provider=provider,
    )


# --- authentication and lifecycle -----------------------------------------------------------


def test_every_experiment_mutation_needs_the_review_token(make_runtime, client_for, clean_db):
    runtime, client = setup(make_runtime, client_for)
    body = {"account_id": ACCOUNT.id, "design": design_json(), "reviewer": "p"}
    assert client.post("/experiments", json=body).status_code == 401
    wrong = client.post("/experiments", json=body, headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 403 and TOKEN not in wrong.text
    x = running(client)
    run_body = {"account": ACCOUNT.model_dump(mode="json"), "reviewer": "p"}
    for action, extra in (
        ("approve", {}),
        ("start", {}),
        ("stop", {"reason": "cost"}),
        ("cancel", {}),
        ("unblind", {"reason": "peek"}),
        ("conclude", {}),
        ("adopt", {"variant": "question"}),
        ("runs", run_body),
    ):
        response = client.post(f"/experiments/{x['id']}/{action}", json={"reviewer": "p"} | extra)
        assert response.status_code == 401, action
    assert count(clean_db, "experiment_assignments") == 0
    assert count(clean_db, "runs") == 0
    # planning arithmetic is pure and open
    plan = client.post("/experiments/feasibility", json=design_json(planned_n_per_arm=100))
    assert plan.status_code == 200 and plan.json()["warnings"]
    assert runtime.experiments is not None


def test_lifecycle_pins_the_base_and_allows_one_open_experiment(make_runtime, client_for, clean_db):
    _, client = setup(make_runtime, client_for)
    active = client.get(f"/accounts/{ACCOUNT.id}/active-strategy").json()
    x = create(client)
    assert x["status"] == "draft" and x["base_version_id"] == active["version"]["id"]
    assert x["design"]["snapshot_age"] == "24h" and len(x["design_sha256"]) == 64
    assert x["feasibility"]["planned_n_per_arm"] == 30
    assert x["arms"][1]["description"] == "declared hook = question"
    assert act(client, x["id"], "start").status_code == 409  # not approved yet
    assert act(client, x["id"], "approve").status_code == 200
    other = create(client)
    refused = act(client, other["id"], "approve")
    assert refused.status_code == 409 and "already approved or running" in refused.json()["detail"]
    started = act(client, x["id"], "start").json()
    assert started["status"] == "running" and started["planned_end_at"] is not None
    kinds = [e["kind"] for e in started["events"]]
    assert kinds == ["created", "approved", "started"]
    assert all(e["auth_method"] == "review_token" for e in started["events"])
    stopped = act(client, x["id"], "stop", reason="operational", note="pause")
    assert stopped.json()["stop_reason"] == "operational"
    assert act(client, x["id"], "stop", reason="looks_good").status_code == 422
    assert act(client, other["id"], "approve").status_code == 200  # the first is closed now
    assert client.post(f"/experiments/{x['id']}/extend", json={}, headers=AUTH).status_code in (
        404,
        405,
    )
    listed = client.get("/experiments", params={"account_id": ACCOUNT.id}).json()
    assert {e["id"] for e in listed} == {x["id"], other["id"]}


def test_invalid_designs_are_refused(make_runtime, client_for, clean_db):
    _, client = setup(make_runtime, client_for)
    body = {"account_id": ACCOUNT.id, "reviewer": "p"}
    unknown_age = client.post(
        "/experiments", json=body | {"design": design_json(snapshot_age="7h")}, headers=AUTH
    )
    assert unknown_age.status_code == 422 and "configured ages" in unknown_age.json()["detail"]
    too_long = length_design().model_dump(mode="json")
    too_long["arms"][1]["treatment"]["length"] = {"min_chars": 200, "max_chars": 300}
    response = client.post("/experiments", json=body | {"design": too_long}, headers=AUTH)
    assert response.status_code == 422 and "exceeds the content policy" in response.json()["detail"]
    one_arm = design_json()
    one_arm["arms"] = one_arm["arms"][:1]
    assert (
        client.post("/experiments", json=body | {"design": one_arm}, headers=AUTH).status_code
        == 422
    )
    no_strategy = client.post(
        "/experiments",
        json={"account_id": "acct_none", "reviewer": "p", "design": design_json()},
        headers=AUTH,
    )
    assert no_strategy.status_code == 409
    assert count(clean_db, "experiments") == 0


# --- assignment ---------------------------------------------------------------------------------


def test_an_experiment_run_enforces_its_arm_and_records_the_assignment(
    make_runtime, client_for, clean_db
):
    _, client = setup(make_runtime, client_for)
    x = running(client)
    run_body = {"account": ACCOUNT.model_dump(mode="json"), "reviewer": "philipp"}
    response = client.post(f"/experiments/{x['id']}/runs", json=run_body, headers=AUTH)
    assert response.status_code == 202, response.text
    created = response.json()
    arm = created["assignment"]["arm"]
    hook = {"number": "number", "question": "question"}[arm]
    run = client.get(f"/runs/{created['run']['id']}").json()
    assert run["run"]["status"] == "awaiting_review"
    assert {c["hook_type"] for c in run["candidates"]} == {hook}
    [(config,)] = sql(clean_db, "SELECT config FROM runs WHERE id = :id", id=created["run"]["id"])
    assert isinstance(config, dict)
    assert config["content_policy"]["required_hook_type"] == hook
    assert created["assignment"]["block_no"] == 0 and created["assignment"]["position"] == 0
    assert count(clean_db, "publications") == 0  # nothing is published by an experiment
    assignments = client.get(f"/experiments/{x['id']}/assignments").json()
    assert [a["run_id"] for a in assignments] == [created["run"]["id"]]

    other = run_body | {"account": ACCOUNT.model_copy(update={"id": "acct_other"}).model_dump()}
    assert client.post(f"/experiments/{x['id']}/runs", json=other, headers=AUTH).status_code == 422
    act(client, x["id"], "stop", reason="cost")
    stopped = client.post(f"/experiments/{x['id']}/runs", json=run_body, headers=AUTH)
    assert stopped.status_code == 409
    assert count(clean_db, "experiment_assignments") == 1


def test_concurrent_assignments_take_distinct_balanced_slots(
    make_runtime, client_for, clean_db, monkeypatch
):
    runtime, client = setup(make_runtime, client_for)
    x = running(client)
    service = runtime.experiments
    assert service is not None
    started: list[str] = []
    monkeypatch.setattr(runtime.service, "start_inserted", started.append)
    errors: list[Exception] = []

    def assign() -> None:
        try:
            service.create_run(
                x["id"], ExperimentRunBody(account=ACCOUNT, reviewer="p"), auth=_auth()
            )
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=assign) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    rows = sql(
        clean_db,
        "SELECT a.block_no, a.position, arm.label FROM experiment_assignments a "
        "JOIN experiment_arms arm ON arm.id = a.arm_id",
    )
    assert len(rows) == 16 and len(started) == 16
    assert sorted((b, p) for b, p, _ in rows) == [divmod(i, 4) for i in range(16)]
    for block in range(4):
        labels = sorted(str(label) for b, _, label in rows if b == block)
        assert labels == ["number", "number", "question", "question"]
    assert count(clean_db, "runs") == 16


def _auth() -> ReviewAuthMethod:
    return ReviewAuthMethod.REVIEW_TOKEN


def test_the_assignment_cap_and_maximum_duration_are_enforced(make_runtime, client_for, clean_db):
    runtime, client = setup(make_runtime, client_for)
    x = running(client, max_runs_per_arm=30)
    seed(runtime, x["id"], 60)
    run_body = {"account": ACCOUNT.model_dump(mode="json"), "reviewer": "p"}
    capped = client.post(f"/experiments/{x['id']}/runs", json=run_body, headers=AUTH)
    assert capped.status_code == 409 and "assignment cap" in capped.json()["detail"]

    act(client, x["id"], "cancel")
    late = running(client, max_duration_days=7)
    backdate_start(runtime.db, late["id"], utc_now() - timedelta(days=8))
    expired = client.post(f"/experiments/{late['id']}/runs", json=run_body, headers=AUTH)
    assert expired.status_code == 409 and "maximum duration" in expired.json()["detail"]


def test_new_assignments_stop_when_the_active_strategy_changes(make_runtime, client_for, clean_db):
    _, client = setup(make_runtime, client_for)
    x = running(client)
    register_and_activate(client, V1.model_copy(update={"version": 2, "tone": "drier"}))
    run_body = {"account": ACCOUNT.model_dump(mode="json"), "reviewer": "p"}
    response = client.post(f"/experiments/{x['id']}/runs", json=run_body, headers=AUTH)
    assert response.status_code == 409 and "base version" in response.json()["detail"]


# --- masking, conclusion, immutability -------------------------------------------------------


def test_reads_are_masked_and_write_nothing(make_runtime, client_for, clean_db):
    runtime, client = setup(make_runtime, client_for)
    x = running(client)
    seed(runtime, x["id"], 64)
    tables = ("experiments", "experiment_events", "experiment_results", "runs", "llm_calls")
    before = {t: count(clean_db, t) for t in tables}
    analysis = client.get(f"/experiments/{x['id']}/analysis").json()
    assert analysis["stage"] == "masked_interim"
    assert analysis["outcomes"] is None and analysis["effects"] is None
    assert all(o["observation"] is None for o in analysis["observations"])
    assert len(analysis["masked_fields"]) == 4 and analysis["not_masked_elsewhere"]
    assert sum(f["analysable"] for f in analysis["funnel"]) == 64
    for path in ("", "/assignments", "/lineage", "/usage"):
        assert client.get(f"/experiments/{x['id']}{path}").status_code == 200
    assert {t: count(clean_db, t) for t in tables} == before
    [(peeked,)] = sql(clean_db, "SELECT peeked FROM experiments WHERE id = :id", id=x["id"])
    assert peeked is False


def test_conclusion_waits_for_the_plan_and_is_immutable(make_runtime, client_for, clean_db):
    runtime, client = setup(make_runtime, client_for)
    x = running(client)
    seed(runtime, x["id"], 20)
    early = act(client, x["id"], "conclude")
    assert early.status_code == 409 and "planned sample" in early.json()["detail"]
    act(client, x["id"], "stop", reason="cost")
    concluded = act(client, x["id"], "conclude").json()
    result = concluded["result"]
    assert concluded["status"] == "concluded"
    assert result["verdict"] == "insufficient_data"
    assert set(result["flags"]) >= {"below_planned_n", "stopped_early", "synthetic"}
    assert result["adoptable"] is False
    again = act(client, x["id"], "conclude").json()
    assert again["result"]["id"] == result["id"] and again["result"]["digest"] == result["digest"]
    stored = client.get(f"/experiments/{x['id']}/analysis").json()
    assert stored["stage"] == "final" and stored["verdict"] == "insufficient_data"
    funnel = {f["arm"]: f for f in stored["funnel"]}
    assert sum(f["assigned"] for f in funnel.values()) == 20
    assert all(f["published"] == f["assigned"] for f in funnel.values())

    for statement in (
        "UPDATE experiment_results SET verdict = 'favours_variant'",
        "DELETE FROM experiment_results",
        "UPDATE experiment_assignments SET arm_id = arm_id",
        "DELETE FROM experiment_arms",
        "UPDATE experiment_events SET reason = 'x'",
        "UPDATE experiments SET design = '{}'::jsonb",
        "UPDATE experiments SET synthetic = false",
        "UPDATE experiments SET snapshot_age_seconds = 1",
        "DELETE FROM experiments",
    ):
        with pytest.raises(DBAPIError):
            sql(clean_db, statement)
    # lifecycle columns may change; the design may not
    sql(clean_db, "UPDATE experiments SET stop_reason = stop_reason RETURNING id")
    assert count(clean_db, "experiment_results") == 1


def test_unblinding_early_makes_the_result_exploratory_and_unadoptable(
    make_runtime, client_for, clean_db
):
    runtime, client = setup(make_runtime, client_for)
    x = running(client, synthetic=False)
    seed(runtime, x["id"], 30, provider="x")
    assert act(client, x["id"], "unblind").status_code == 422  # a reason is required
    unblinded = act(client, x["id"], "unblind", reason="stakeholder preview").json()
    assert unblinded["peeked"] is True and unblinded["unblinded_at"] is not None
    assert act(client, x["id"], "unblind", reason="again").status_code == 409
    interim = client.get(f"/experiments/{x['id']}/analysis").json()
    assert interim["stage"] == "unblinded_interim" and interim["outcomes"] is not None
    assert interim["verdict"] is None and interim["evidence_class"] == "exploratory"
    seeded = seed_experiment_runs(
        runtime.db,
        x["id"],
        account=ACCOUNT,
        count=40,
        outcome=rate_outcome(POSITIVE),
        start=utc_now() - timedelta(days=20),
        spacing=timedelta(hours=6),
        rng=random.Random(11),
        provider="x",
    )
    assert len(seeded) == 40
    result = act(client, x["id"], "conclude").json()["result"]
    assert result["verdict"] == "exploratory_favours_variant"
    assert result["evidence_class"] == "exploratory" and "unblinded_early" in result["flags"]
    assert result["adoptable"] is False
    refused = act(client, x["id"], "adopt", variant="question")
    assert refused.status_code == 409 and "exploratory" in refused.json()["detail"]
    assert count(clean_db, "strategy_versions") == 1


# --- adoption -----------------------------------------------------------------------------------


def test_a_confirmatory_real_result_is_adopted_as_an_inactive_version(
    make_runtime, client_for, clean_db
):
    runtime, client = setup(make_runtime, client_for)
    before = client.get(f"/accounts/{ACCOUNT.id}/active-strategy").json()
    x = running(client, synthetic=False)
    seed(runtime, x["id"], 64, provider="x")
    result = act(client, x["id"], "conclude").json()["result"]
    assert result["verdict"] == "favours_variant", result
    assert result["evidence_class"] == "confirmatory" and result["adoptable"] is True
    control = act(client, x["id"], "adopt", variant="number")
    assert control.status_code == 409
    adopted = act(client, x["id"], "adopt", variant="question", note="ship it")
    assert adopted.status_code == 201, adopted.text
    version = adopted.json()
    assert version["origin"] == "experiment" and version["active"] is False
    assert version["source_experiment_result_id"] == result["id"]
    assert version["parent_version_id"] == before["version"]["id"]
    assert version["content"]["preferred_hooks"][0] == HookType.QUESTION.value
    assert version["version"] == 2
    after = client.get(f"/accounts/{ACCOUNT.id}/active-strategy").json()
    assert after["version"]["id"] == before["version"]["id"]  # nothing activated
    assert act(client, x["id"], "adopt", variant="question").status_code == 409
    view = client.get(f"/experiments/{x['id']}").json()
    assert view["result"]["adopted_version_id"] == version["id"]
    assert view["events"][-1]["kind"] == "adopted"
    assert (
        client.get(f"/experiments/{x['id']}/lineage").json()["adopted_version_id"] == version["id"]
    )
    activated = client.post(
        f"/strategies/{V1.id}/activate",
        json={"version": 2, "expected_active_version_id": before["version"]["id"], "reviewer": "p"},
        headers=AUTH,
    )
    assert activated.status_code == 200  # activation stays its own explicit request
    assert count(clean_db, "publications", "status <> 'published'") == 0


def test_adoption_is_refused_once_the_base_is_no_longer_active(make_runtime, client_for, clean_db):
    runtime, client = setup(make_runtime, client_for)
    x = running(client, synthetic=False)
    seed(runtime, x["id"], 64, provider="x")
    assert act(client, x["id"], "conclude").json()["result"]["verdict"] == "favours_variant"
    register_and_activate(client, V1.model_copy(update={"version": 2, "tone": "drier"}))
    refused = act(client, x["id"], "adopt", variant="question")
    assert refused.status_code == 409 and "no longer the active" in refused.json()["detail"]


def test_synthetic_results_are_never_adoptable(make_runtime, client_for, clean_db):
    runtime, client = setup(make_runtime, client_for)
    x = running(client)
    seed(runtime, x["id"], 64)
    result = act(client, x["id"], "conclude").json()["result"]
    assert result["verdict"] == "favours_variant"
    assert result["adoption_blockers"] == ["synthetic experiments are never adoptable"]
    refused = act(client, x["id"], "adopt", variant="question")
    assert refused.status_code == 409 and "synthetic" in refused.json()["detail"]


# --- lineage and usage ---------------------------------------------------------------------------


def test_lineage_and_usage_cover_every_assigned_run(make_runtime, client_for, clean_db):
    runtime, client = setup(make_runtime, client_for)
    x = running(client)

    def mostly(label: str, index: int, rng: random.Random) -> SeedOutcome:
        if index == 0:
            return SeedOutcome(generated=False, approved=False, published=False)
        return rate_outcome(POSITIVE)(label, index, rng)

    seed(runtime, x["id"], 8, mostly)
    run_body = {"account": ACCOUNT.model_dump(mode="json"), "reviewer": "p"}
    real = client.post(f"/experiments/{x['id']}/runs", json=run_body, headers=AUTH).json()
    lineage = client.get(f"/experiments/{x['id']}/lineage").json()
    assert len(lineage["entries"]) == 9
    published = [e for e in lineage["entries"] if e["publication_ids"]]
    assert len(published) == 6 and all(len(e["post_metric_ids"]) == 1 for e in published)
    empty = [e for e in lineage["entries"] if not e["candidate_ids"]]
    assert len(empty) == 2
    usage = client.get(f"/experiments/{x['id']}/usage").json()
    assert sum(a["runs"] for a in usage["per_arm"]) == 9
    real_arm = real["assignment"]["arm"]
    llm = next(a for a in usage["per_arm"] if a["arm"] == real_arm)["usage"]["llm"]
    assert llm and all(row["provider"] == "fake" for row in llm)
    assert usage["projected"]["remaining_runs"] == 2 * 60 - 9
    assert "not invoices" in usage["note"]


# --- demo ---------------------------------------------------------------------------------------


def test_the_synthetic_scenarios_demo(make_runtime, clean_db):
    runtime = strategy_runtime(make_runtime)
    lines: list[str] = []
    results = experiment_demo.run_scenarios(runtime, _auth(), lines.append)
    verdicts = {r.name: r.experiment.result.verdict.value for r in results if r.experiment.result}
    assert verdicts == {
        "positive": "favours_variant",
        "null": "inconclusive",
        "insufficient_data": "insufficient_data",
        "attrition": "inconclusive",
        "missing_impressions": "insufficient_data",
        "early_unblinding": "exploratory_favours_variant",
    }
    flags = {r.name: {f.value for f in r.analysis.flags} for r in results}
    assert "differential_attrition" in flags["attrition"]
    assert "impressions_missing" in flags["missing_impressions"]
    assert "max_duration_reached" in flags["insufficient_data"]
    assert all("synthetic" in f for f in flags.values())
    assert all(r.adopt_refusal and "synthetic" in r.adopt_refusal for r in results)
    assert count(clean_db, "strategy_versions", "origin = 'experiment'") == 0
    assert count(clean_db, "publications", "provider <> 'mock'") == 0
    assert any("candidates declare" in line for line in lines)
    assert TOKEN not in "\n".join(lines)


# --- schema -------------------------------------------------------------------------------------


def test_0006_applies_to_a_populated_phase_7_database(base_database_url):
    from alembic import command

    url = create_scratch_database(base_database_url, prefix="sga_phase7")
    try:
        command.upgrade(alembic_config(url), "0005")
        sql(
            url,
            "INSERT INTO strategy_versions (id, strategy_id, account_id, version, content, "
            "content_sha256, origin) VALUES ('sv_p7', 'strat_p7', 'acct_p7', 1, '{}', 'sha', "
            "'registered') RETURNING id",
        )
        upgrade(url)
        assert current_revision(url) == "0006"
        assert sql(url, "SELECT source_experiment_result_id FROM strategy_versions") == [(None,)]
        for table in ("experiments", "experiment_arms", "experiment_assignments"):
            assert count(url, table) == 0
        with pytest.raises(DBAPIError):
            sql(url, "UPDATE strategy_versions SET note = 'x'")  # still append-only
        command.downgrade(alembic_config(url), "0005")
        assert sql(url, "SELECT id FROM strategy_versions") == [("sv_p7",)]
        upgrade(url)
        assert current_revision(url) == "0006"
    finally:
        drop_scratch_database(base_database_url, url)


def test_a_human_edit_is_checked_against_the_arm(make_runtime, client_for, clean_db):
    _, client = setup(make_runtime, client_for)
    x = create(client, **length_design().model_dump())
    act(client, x["id"], "approve")
    act(client, x["id"], "start")
    run_body = {"account": ACCOUNT.model_dump(mode="json"), "reviewer": "p"}
    created = client.post(f"/experiments/{x['id']}/runs", json=run_body, headers=AUTH).json()
    run_id = created["run"]["id"]
    detail = client.get(f"/runs/{run_id}").json()
    [low, high] = {"short": (60, 120), "long": (180, 260)}[created["assignment"]["arm"]]
    assert all(low <= len(c["content"]) <= high for c in detail["candidates"])
    original = detail["review"]["candidate_ids"][0]
    edit = {
        "action": "edit",
        "candidate_id": original,
        "edited_content": "Far too short.",
        "reviewer": "philipp",
    }
    assert client.post(f"/runs/{run_id}/review", json=edit).status_code == 202
    after = client.get(f"/runs/{run_id}").json()
    edited = next(c for c in after["candidates"] if c["origin"] == "human_edit")
    rules = {v["rule"] for k in edited["critiques"] for v in k["policy_violations"]}
    assert "min_post_length" in rules
