"""Proposals, decisions, versions and activation on PostgreSQL (Phase 7)."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy.exc import DBAPIError

from social_growth_agent.agents.fakes import fake_strategy
from social_growth_agent.agents.strategy import StrategyProposalDraft
from social_growth_agent.errors import ProposalStateError, TransientProviderError
from social_growth_agent.models import TokenUsage
from social_growth_agent.models.strategy import ProposalOutcome, ProposalStatus, ReviewAuthMethod
from social_growth_agent.providers import LLMRequest, LLMResponse
from social_growth_agent.providers.mocks import ScriptedLLMProvider
from tests.db.analytics_helpers import count
from tests.db.conftest import sql
from tests.db.strategy_helpers import (
    ACCOUNT,
    AUTH,
    V1,
    approve_body,
    create_evaluation,
    register_and_activate,
    seed_comparable,
    strategy_runtime,
    with_strategy_llm,
)


class MeteredFake:
    """The fake Strategy Agent behind a provider that reports model and token usage."""

    def __init__(self, draft=None, model: str = "gpt-test") -> None:
        self.calls: list[LLMRequest] = []
        self._draft = draft
        self._model = model

    def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> LLMResponse[T]:
        self.calls.append(request)
        output = self._draft or fake_strategy(request)
        assert isinstance(output, schema)
        return LLMResponse(
            output=output, model=self._model, usage=TokenUsage(input_tokens=1000, output_tokens=250)
        )


@pytest.fixture
def runtime(make_runtime):
    return strategy_runtime(make_runtime)


@pytest.fixture
def client(runtime, client_for):
    return client_for(runtime)


@pytest.fixture
def evaluation(runtime, client):
    register_and_activate(client)
    seed_comparable(runtime)
    return create_evaluation(client)


def propose(client, evaluation) -> dict[str, Any]:
    response = client.post(
        f"/evaluations/{evaluation['id']}/proposals", json={"requested_by": "philipp"}
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def active_version(client) -> dict[str, Any]:
    return dict(client.get(f"/accounts/{ACCOUNT.id}/active-strategy").json()["version"])


# --- versions and activation -------------------------------------------------------------


def test_importing_run_strategies_is_explicit_and_never_guesses(runtime, clean_db):
    seed_comparable(runtime, per_hook=1)
    other = V1.model_copy(update={"id": "strat_other"})
    seed_comparable(runtime, per_hook=1, strategy=other)
    # the same id and version with a different body: never merged or picked
    seed_comparable(runtime, per_hook=1, strategy=other.model_copy(update={"tone": "loud"}))
    strategies = runtime.strategies
    assert strategies is not None

    dry = strategies.import_from_runs(dry_run=True)
    assert dry.registered == ["strat_p7@v1"]
    assert dry.conflicts == ["strat_other@v1: 2 different bodies in runs"]
    assert count(clean_db, "strategy_versions") == 0

    real = strategies.import_from_runs(dry_run=False)
    assert real == dry
    [(origin,)] = sql(clean_db, "SELECT origin FROM strategy_versions")
    assert origin == "imported_from_run"
    assert count(clean_db, "strategy_activations") == 0  # importing activates nothing

    again = strategies.import_from_runs(dry_run=False)
    assert again.registered == [] and again.already_registered == ["strat_p7@v1"]
    assert count(clean_db, "strategy_versions") == 1


def test_registration_is_idempotent_and_refuses_a_different_body(client):
    body = {"strategy": V1.model_dump(mode="json"), "reviewer": "philipp"}
    first = client.post("/strategies/versions", json=body, headers=AUTH)
    again = client.post("/strategies/versions", json=body, headers=AUTH)
    assert (first.status_code, again.status_code) == (201, 200)
    assert (
        first.json()["id"] == again.json()["id"] and first.json()["auth_method"] == "review_token"
    )
    changed = V1.model_copy(update={"tone": "loud"}).model_dump(mode="json")
    assert (
        client.post(
            "/strategies/versions", json=body | {"strategy": changed}, headers=AUTH
        ).status_code
        == 409
    )
    other = V1.model_copy(update={"account_id": "acct_other", "version": 2}).model_dump(mode="json")
    assert (
        client.post(
            "/strategies/versions", json=body | {"strategy": other}, headers=AUTH
        ).status_code
        == 409
    )


def test_activation_is_explicit_and_compare_and_set(client):
    v1 = register_and_activate(client)
    v2 = V1.model_copy(update={"version": 2, "tone": "warmer"})
    client.post(
        "/strategies/versions",
        json={"strategy": v2.model_dump(mode="json"), "reviewer": "p"},
        headers=AUTH,
    )
    assert active_version(client)["version"] == 1  # registering never activates
    body = {"version": 2, "expected_active_version_id": None, "reviewer": "p"}
    stale = client.post(f"/strategies/{V1.id}/activate", json=body, headers=AUTH)
    assert stale.status_code == 409 and "active version changed" in stale.json()["detail"]
    ok = client.post(
        f"/strategies/{V1.id}/activate",
        json=body | {"expected_active_version_id": v1["id"]},
        headers=AUTH,
    )
    assert ok.status_code == 200 and ok.json()["activation"]["seq"] == 2
    again = client.post(
        f"/strategies/{V1.id}/activate",
        json=body | {"expected_active_version_id": ok.json()["version"]["id"]},
        headers=AUTH,
    )
    assert again.status_code == 409 and "already active" in again.json()["detail"]
    history = client.get(f"/strategies/{V1.id}/versions").json()
    assert [v["active"] for v in history["versions"]] == [False, True]
    assert [a["previous_version_id"] for a in history["activations"]] == [None, v1["id"]]


def test_concurrent_activations_cannot_both_win(runtime, client):
    v1 = register_and_activate(client)
    for version in (2, 3):
        strategy = V1.model_copy(update={"version": version})
        client.post(
            "/strategies/versions",
            json={"strategy": strategy.model_dump(mode="json"), "reviewer": "p"},
            headers=AUTH,
        )
    assert runtime.strategies is not None
    service = runtime.strategies

    def activate(version: int) -> str:
        try:
            service.activate(
                V1.id,
                version,
                expected_active_version_id=v1["id"],
                reviewer="p",
                auth=ReviewAuthMethod.REVIEW_TOKEN,
                note=None,
            )
        except ProposalStateError:
            return "refused"
        return "activated"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(activate, [2, 3]))
    assert outcomes == ["activated", "refused"]
    assert count(runtime.db.url.get_secret_value(), "strategy_activations") == 2


# --- proposals ------------------------------------------------------------------------------


def test_a_proposal_is_validated_and_waits_for_review(client, evaluation, clean_db):
    proposal = propose(client, evaluation)
    assert proposal["status"] == "pending_review" and proposal["outcome"] == "change"
    [change] = proposal["proposal"]["changes"]
    assert (change["field"], change["current_value"], change["proposed_value"]) == (
        "preferred_hooks",
        ["how_to"],
        ["number"],
    )
    assert change["evidence_refs"] == ["cmp:hook_type:how_to:vs:number"]
    assert change["evidence_refs"][0] in {
        c["comparison_id"] for c in evaluation["summary"]["comparisons"]
    }
    assert proposal["evaluation_digest"] == evaluation["digest"]
    # nothing else happened: no version, activation, run or publication
    assert (
        count(clean_db, "strategy_versions") == 1 and count(clean_db, "strategy_activations") == 1
    )
    assert (
        count(clean_db, "publications", "true") == 22
        and count(clean_db, "strategy_proposal_decisions") == 0
    )
    assert (
        client.get("/strategy-proposals", params={"status": "pending_review"}).json()[0]["id"]
        == proposal["id"]
    )


def test_only_one_open_proposal_per_account(client, evaluation):
    propose(client, evaluation)
    second = client.post(f"/evaluations/{evaluation['id']}/proposals", json={})
    assert second.status_code == 409 and "already open" in second.json()["detail"]


def test_proposals_are_disabled_by_default(make_runtime, client_for):
    runtime = strategy_runtime(make_runtime, strategy_proposer="disabled")
    client = client_for(runtime)
    register_and_activate(client)
    seed_comparable(runtime, per_hook=2)
    ev = create_evaluation(client)
    response = client.post(f"/evaluations/{ev['id']}/proposals", json={})
    assert response.status_code == 409 and "disabled" in response.json()["detail"]


def test_a_proposal_needs_an_active_strategy(runtime, client):
    seed_comparable(runtime, per_hook=2)
    ev = create_evaluation(client)
    response = client.post(f"/evaluations/{ev['id']}/proposals", json={})
    assert response.status_code == 409 and "no active strategy" in response.json()["detail"]


def test_insufficient_evidence_gives_no_change_without_calling_a_model(runtime, client, clean_db):
    llm = MeteredFake()
    with_strategy_llm(runtime, llm)
    register_and_activate(client)
    seed_comparable(runtime, per_hook=3)
    ev = create_evaluation(client)
    assert ev["status"] == "insufficient_evidence"
    proposal = propose(client, ev)
    assert (proposal["status"], proposal["outcome"], proposal["proposer"]) == (
        "pending_review",
        "no_change",
        "deterministic",
    )
    assert llm.calls == [] and proposal["usage"]["usage"]["llm"] == []
    approve = client.post(
        f"/strategy-proposals/{proposal['id']}/approve", json=approve_body(proposal), headers=AUTH
    )
    assert approve.status_code == 409 and "nothing to approve" in approve.json()["detail"]
    reject = client.post(
        f"/strategy-proposals/{proposal['id']}/reject",
        json={"reviewer": "p", "summary": "ok"},
        headers=AUTH,
    )
    assert reject.json()["status"] == "rejected"


# --- decisions ------------------------------------------------------------------------------


def test_approval_creates_a_new_inactive_version_with_a_full_audit_record(
    client, evaluation, clean_db
):
    proposal = propose(client, evaluation)
    response = client.post(
        f"/strategy-proposals/{proposal['id']}/approve", json=approve_body(proposal), headers=AUTH
    )
    assert response.status_code == 200, response.text
    approved = response.json()
    decision = approved["decision"]
    assert approved["status"] == "approved"
    assert (
        decision["decision"],
        decision["reviewer"],
        decision["auth_method"],
        decision["summary"],
    ) == ("approve", "philipp", "review_token", "try it")
    assert decision["reviewed_proposal_sha256"] == proposal["proposal_sha256"]
    assert decision["reviewed_evaluation_digest"] == evaluation["digest"]
    history = client.get(f"/strategies/{V1.id}/versions").json()
    v1, v2 = history["versions"]
    assert v2["id"] == decision["resulting_version_id"] and v2["parent_version_id"] == v1["id"]
    assert (v2["version"], v2["origin"], v2["active"], v1["active"]) == (2, "proposal", False, True)
    assert v2["content"]["preferred_hooks"] == ["number"]
    assert {k: v for k, v in v2["content"].items() if k not in ("version", "preferred_hooks")} == {
        k: v for k, v in v1["content"].items() if k not in ("version", "preferred_hooks")
    }
    # approval never activates, publishes or starts a run
    assert active_version(client)["id"] == v1["id"]
    assert count(clean_db, "runs", "research_query <> 'seeded'") == 0
    assert count(clean_db, "publication_attempts") == 0


def test_a_proposal_is_decided_once(client, evaluation):
    proposal = propose(client, evaluation)
    url = f"/strategy-proposals/{proposal['id']}"
    assert (
        client.post(f"{url}/approve", json=approve_body(proposal), headers=AUTH).status_code == 200
    )
    assert (
        client.post(f"{url}/approve", json=approve_body(proposal), headers=AUTH).status_code == 409
    )
    assert (
        client.post(
            f"{url}/reject", json={"reviewer": "p", "summary": "no"}, headers=AUTH
        ).status_code
        == 409
    )
    assert len(client.get(f"/strategies/{V1.id}/versions").json()["versions"]) == 2


def test_concurrent_approvals_create_one_version(runtime, client, evaluation, clean_db):
    proposal = propose(client, evaluation)
    assert runtime.strategies is not None
    service = runtime.strategies

    def approve(_: int) -> str:
        try:
            service.approve(
                proposal["id"],
                reviewer="p",
                auth=ReviewAuthMethod.REVIEW_TOKEN,
                summary="s",
                proposal_sha256=proposal["proposal_sha256"],
                evaluation_digest=proposal["evaluation_digest"],
            )
        except ProposalStateError:
            return "refused"
        return "approved"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = sorted(pool.map(approve, range(4)))
    assert outcomes == ["approved", "refused", "refused", "refused"]
    assert count(clean_db, "strategy_versions") == 2
    assert count(clean_db, "strategy_proposal_decisions") == 1


def test_rejection_changes_nothing_but_the_proposal(client, evaluation, clean_db):
    proposal = propose(client, evaluation)
    before = active_version(client)
    response = client.post(
        f"/strategy-proposals/{proposal['id']}/reject",
        json={"reviewer": "philipp", "summary": "not convinced"},
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.json()["decision"]["decision"] == "reject"
    assert active_version(client) == before
    assert count(clean_db, "strategy_versions") == 1


def test_an_approval_of_a_different_proposal_or_evaluation_is_refused(client, evaluation):
    proposal = propose(client, evaluation)
    url = f"/strategy-proposals/{proposal['id']}/approve"
    for field in ("proposal_sha256", "evaluation_digest"):
        response = client.post(url, json=approve_body(proposal, **{field: "0" * 64}), headers=AUTH)
        assert response.status_code == 409 and "digest" in response.json()["detail"]
    assert client.get(f"/strategy-proposals/{proposal['id']}").json()["status"] == "pending_review"


def test_a_proposal_whose_base_is_no_longer_active_is_superseded(client, evaluation):
    proposal = propose(client, evaluation)
    v1 = active_version(client)
    other = V1.model_copy(update={"version": 5, "tone": "other"})
    client.post(
        "/strategies/versions",
        json={"strategy": other.model_dump(mode="json"), "reviewer": "p"},
        headers=AUTH,
    )
    client.post(
        f"/strategies/{V1.id}/activate",
        json={"version": 5, "expected_active_version_id": v1["id"], "reviewer": "p"},
        headers=AUTH,
    )
    response = client.post(
        f"/strategy-proposals/{proposal['id']}/approve", json=approve_body(proposal), headers=AUTH
    )
    assert response.status_code == 409 and "superseded" in response.json()["detail"]
    assert client.get(f"/strategy-proposals/{proposal['id']}").json()["status"] == "superseded"
    # the account can get a fresh proposal now
    assert client.post(f"/evaluations/{evaluation['id']}/proposals", json={}).status_code == 201


def test_new_versions_skip_numbers_used_by_ad_hoc_runs(client, evaluation, run_payload):
    payload = run_payload()
    payload["account"] = ACCOUNT.model_dump(mode="json")
    payload["strategy"] = V1.model_copy(update={"version": 7}).model_dump(mode="json")
    assert client.post("/runs", json=payload).status_code == 202  # unregistered inline v7
    proposal = propose(client, evaluation)
    approved = client.post(
        f"/strategy-proposals/{proposal['id']}/approve", json=approve_body(proposal), headers=AUTH
    ).json()
    versions = client.get(f"/strategies/{V1.id}/versions").json()["versions"]
    assert [v["version"] for v in versions] == [1, 8]
    assert versions[1]["id"] == approved["decision"]["resulting_version_id"]


def test_versions_activations_and_decisions_are_append_only(client, evaluation, clean_db):
    proposal = propose(client, evaluation)
    client.post(
        f"/strategy-proposals/{proposal['id']}/approve", json=approve_body(proposal), headers=AUTH
    )
    for statement in (
        "UPDATE strategy_versions SET content = '{}'",
        "DELETE FROM strategy_versions",
        "UPDATE strategy_activations SET seq = 9",
        "DELETE FROM strategy_activations",
        "UPDATE strategy_proposal_decisions SET decision = 'reject'",
        "DELETE FROM strategy_proposal_decisions",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            sql(clean_db, statement)


def test_a_tampered_proposal_outside_the_allowlist_is_never_approved(client, evaluation, clean_db):
    proposal = propose(client, evaluation)
    sql(
        clean_db,
        "UPDATE strategy_proposals SET proposal = jsonb_set(proposal, "
        "'{proposed_strategy,tone}', '\"shouty\"') WHERE id = :id RETURNING id",
        id=proposal["id"],
    )
    response = client.post(
        f"/strategy-proposals/{proposal['id']}/approve", json=approve_body(proposal), headers=AUTH
    )
    assert response.status_code == 409 and "allowlist" in response.json()["detail"]
    assert count(clean_db, "strategy_versions") == 1


# --- future runs ----------------------------------------------------------------------------


def test_future_runs_use_the_active_version_and_past_runs_keep_theirs(
    client, evaluation, clean_db, run_payload
):
    payload = run_payload()
    payload["account"] = ACCOUNT.model_dump(mode="json")
    payload["strategy"] = V1.model_dump(mode="json")
    old_run = client.post("/runs", json=payload).json()
    v1_id = active_version(client)["id"]
    assert old_run["strategy_version_ref"] == v1_id  # matched the registered v1

    proposal = propose(client, evaluation)
    approved = client.post(
        f"/strategy-proposals/{proposal['id']}/approve", json=approve_body(proposal), headers=AUTH
    ).json()
    v2_id = approved["decision"]["resulting_version_id"]
    # before activation, new runs still get v1
    run = client.post(
        "/runs", json={"account": payload["account"], "use_active_strategy": True}
    ).json()
    assert run["strategy_version_ref"] == v1_id
    client.post(
        f"/strategies/{V1.id}/activate",
        json={"version": 2, "expected_active_version_id": v1_id, "reviewer": "p"},
        headers=AUTH,
    )
    run = client.post(
        "/runs", json={"account": payload["account"], "use_active_strategy": True}
    ).json()
    assert (run["strategy_version"], run["strategy_version_ref"]) == (2, v2_id)
    [(stored,)] = sql(
        clean_db, "SELECT strategy->'preferred_hooks' FROM runs WHERE id = :id", id=run["id"]
    )
    assert stored == ["number"]
    old = client.get(f"/runs/{old_run['id']}").json()
    assert (old["run"]["strategy_version"], old["run"]["strategy_version_ref"]) == (1, v1_id)


def test_inline_strategies_that_conflict_with_a_registered_version_are_refused(client, run_payload):
    register_and_activate(client)
    payload = run_payload()
    payload["account"] = ACCOUNT.model_dump(mode="json")
    payload["strategy"] = V1.model_copy(update={"tone": "different"}).model_dump(mode="json")
    response = client.post("/runs", json=payload)
    assert response.status_code == 409 and "different body" in response.json()["detail"]
    both = {
        "account": payload["account"],
        "strategy": payload["strategy"],
        "use_active_strategy": True,
    }
    assert client.post("/runs", json=both).status_code == 422
    assert client.post("/runs", json={"account": payload["account"]}).status_code == 422
    no_active = {"account": {**payload["account"], "id": "acct_none"}, "use_active_strategy": True}
    assert client.post("/runs", json=no_active).status_code == 404


# --- failures and retries -----------------------------------------------------------------------


def test_a_failed_model_call_is_recorded_and_can_be_retried_once(runtime, client, clean_db):
    with_strategy_llm(
        runtime,
        ScriptedLLMProvider(
            {StrategyProposalDraft: fake_strategy}, fail_first=1, failure=TransientProviderError
        ),
    )
    register_and_activate(client)
    seed_comparable(runtime)
    ev = create_evaluation(client)
    failed = propose(client, ev)
    assert (failed["status"], failed["error_type"]) == ("failed", "TransientProviderError")
    assert failed["error_message"] == "model call failed (provider error)"
    assert count(clean_db, "strategy_versions") == 1
    retried = client.post(f"/strategy-proposals/{failed['id']}/retry", json={"requested_by": "p"})
    assert retried.status_code == 201
    assert (retried.json()["status"], retried.json()["retry_of"]) == (
        "pending_review",
        failed["id"],
    )
    again = client.post(f"/strategy-proposals/{failed['id']}/retry", json={})
    assert again.status_code == 409
    pending = client.post(f"/strategy-proposals/{retried.json()['id']}/retry", json={})
    assert pending.status_code == 409 and "only failed or stale" in pending.json()["detail"]


def test_output_that_fails_validation_is_stored_as_failed_with_reasons(runtime, client):
    bad = StrategyProposalDraft(
        outcome=ProposalOutcome.CHANGE,
        summary="trust me, 9000% better",
        changes=[],
        posting_time_note=None,
        posting_time_evidence_refs=[],
    )
    llm = MeteredFake(bad)
    with_strategy_llm(runtime, llm)
    register_and_activate(client)
    seed_comparable(runtime)
    proposal = propose(client, create_evaluation(client))
    assert proposal["status"] == "failed" and proposal["proposal"] is None
    assert any("without any change" in r for r in proposal["validation_errors"])
    assert any("9000" in r for r in proposal["validation_errors"])
    assert proposal["usage"]["usage"]["llm"][0]["input_tokens"] == 1000  # tokens still counted


def test_a_stale_generating_proposal_is_retried_and_its_late_result_discarded(
    runtime, client, clean_db
):
    register_and_activate(client)
    seed_comparable(runtime)
    ev = create_evaluation(client)
    proposal = propose(client, ev)
    # simulate a process that died mid-call an hour ago
    sql(
        clean_db,
        "UPDATE strategy_proposals SET status = 'generating', outcome = NULL, proposal = NULL,"
        " started_at = now() - interval '1 hour' WHERE id = :id RETURNING id",
        id=proposal["id"],
    )
    assert client.get(f"/strategy-proposals/{proposal['id']}").json()["stale"] is True
    blocked = client.post(f"/evaluations/{ev['id']}/proposals", json={})
    assert blocked.status_code == 409
    retried = client.post(f"/strategy-proposals/{proposal['id']}/retry", json={})
    assert retried.status_code == 201 and retried.json()["status"] == "pending_review"
    old = client.get(f"/strategy-proposals/{proposal['id']}").json()
    assert old["status"] == "abandoned"
    assert runtime.strategies is not None
    runtime.strategies._finish(
        proposal["id"], status=ProposalStatus.PENDING_REVIEW, call=None, latency_ms=1.0
    )
    assert client.get(f"/strategy-proposals/{proposal['id']}").json()["status"] == "abandoned"
    assert count(clean_db, "strategy_proposals", "status IN ('generating', 'pending_review')") == 1


def test_a_fresh_generating_proposal_cannot_be_retried(runtime, client, clean_db):
    register_and_activate(client)
    seed_comparable(runtime)
    proposal = propose(client, create_evaluation(client))
    sql(
        clean_db,
        "UPDATE strategy_proposals SET status = 'generating' WHERE id = :id RETURNING id",
        id=proposal["id"],
    )
    response = client.post(f"/strategy-proposals/{proposal['id']}/retry", json={})
    assert response.status_code == 409 and "still generating" in response.json()["detail"]


# --- 28. token and cost accounting ----------------------------------------------------------


def test_proposal_tokens_and_costs_are_recorded(runtime, client):
    llm = MeteredFake()
    with_strategy_llm(runtime, llm)
    register_and_activate(client)
    seed_comparable(runtime)
    proposal = propose(client, create_evaluation(client))
    assert len(llm.calls) == 1 and proposal["model"] == "gpt-test"
    [usage] = proposal["usage"]["usage"]["llm"]
    assert (usage["calls"], usage["input_tokens"], usage["output_tokens"]) == (1, 1000, 250)
    estimate = proposal["usage"]["at_proposal_pricing"]
    # TEST_PRICES: gpt-test 1.00 / 4.00 per million tokens
    assert Decimal(estimate["total"]) == Decimal("0.002")
    assert proposal["started_at"] and proposal["finished_at"] and proposal["latency_ms"] is not None


def test_an_evaluation_rolls_up_the_cost_of_every_proposal_made_from_it(runtime, client):
    register_and_activate(client)
    seed_comparable(runtime)
    ev = create_evaluation(client)
    before = client.get(f"/evaluations/{ev['id']}").json()["usage"]
    assert before["proposals"] == 0 and before["usage"]["llm"] == []
    assert Decimal(before["at_proposal_pricing_total"]) == 0

    with_strategy_llm(runtime, MeteredFake())
    first = propose(client, ev)
    rejected = client.post(
        f"/strategy-proposals/{first['id']}/reject",
        json={"reviewer": "philipp", "summary": "not now"},
        headers=AUTH,
    )
    assert rejected.status_code == 200, rejected.text
    propose(client, ev)
    usage = client.get(f"/evaluations/{ev['id']}").json()["usage"]
    assert usage["proposals"] == 2
    [llm] = usage["usage"]["llm"]
    assert (llm["calls"], llm["input_tokens"], llm["output_tokens"]) == (2, 2000, 500)
    assert Decimal(usage["at_current_pricing"]["total"]) == Decimal("0.004")
    assert Decimal(usage["at_proposal_pricing_total"]) == Decimal("0.004")

    # an unpriced model makes the total unknown instead of guessing
    second = client.get("/strategy-proposals", params={"status": "pending_review"}).json()
    [open_one] = second
    client.post(
        f"/strategy-proposals/{open_one['id']}/reject",
        json={"reviewer": "philipp", "summary": "no"},
        headers=AUTH,
    )
    with_strategy_llm(runtime, MeteredFake(model="gpt-unpriced"))
    propose(client, ev)
    usage = client.get(f"/evaluations/{ev['id']}").json()["usage"]
    assert usage["proposals"] == 3 and len(usage["usage"]["llm"]) == 2
    assert usage["at_current_pricing"]["total"] is None
    assert usage["at_proposal_pricing_total"] is None
    assert "openai:gpt-unpriced input_tokens" in usage["proposal_pricing_missing"]


def test_unpriced_models_are_reported_as_missing_prices(runtime, client):
    register_and_activate(client)
    seed_comparable(runtime)
    with_strategy_llm(runtime, MeteredFake(model="gpt-unpriced"))
    proposal = propose(client, create_evaluation(client))
    estimate = proposal["usage"]["at_current_pricing"]
    assert estimate["total"] is None
    assert estimate["missing_prices"] == [
        "openai:gpt-unpriced input_tokens",
        "openai:gpt-unpriced output_tokens",
    ]
