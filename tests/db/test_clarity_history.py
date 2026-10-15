"""The new clarity safeguard never regrades stored critiques or published content."""

from social_growth_agent.agents import CriticReport, IssueDraft
from social_growth_agent.models import CritiqueVerdict, IssueCategory
from social_growth_agent.policies import final_verdict
from tests.conftest import always, make_llm
from tests.db.conftest import sql
from tests.db.publishing_helpers import approved_run


def test_legacy_critique_and_publication_survive_restart_without_regrading(
    make_runtime, client_for, run_payload, monkeypatch
):
    llm = make_llm(always(CritiqueVerdict.PASS))
    base_critic = llm._handlers[CriticReport]

    def confusing_but_high_scoring(request):
        report = base_critic(request)
        assert isinstance(report, CriticReport)
        for item in report.evaluations:
            item.score = 0.99
            item.issues = [
                IssueDraft(
                    category=IssueCategory.UNCLEAR,
                    detail="The apparent message is to compare models. The reader cannot tell "
                    "which comparison matters.",
                )
            ]
        return report

    llm._handlers[CriticReport] = confusing_but_high_scoring

    def legacy_verdict(recommended, violations, *, issues=()):
        # Simulate the previous adapter: hard rules applied, soft issues not passed
        # to the verdict policy. No historical row is updated to manufacture a fixture.
        return final_verdict(recommended, violations)

    first = make_runtime(llm=llm)
    client = client_for(first)
    with monkeypatch.context() as legacy:
        legacy.setattr("social_growth_agent.agents.critic.final_verdict", legacy_verdict)
        run_id, candidate_id = approved_run(client, run_payload)
    requested = client.post(f"/runs/{run_id}/publish", json={"candidate_id": candidate_id})
    assert requested.status_code == 202
    publication_id = requested.json()["id"]
    assert first.worker.run_once().published == (publication_id,)  # disposable DB, mock publisher
    before = sql(
        first.db.libpq_url,
        "SELECT to_jsonb(k) FROM critiques k WHERE run_id = :id ORDER BY position",
        id=run_id,
    )
    publication_before = sql(
        first.db.libpq_url,
        "SELECT to_jsonb(p) FROM publications p WHERE id = :id",
        id=publication_id,
    )

    # A new runtime uses the new policy; reads and startup must preserve old judgements.
    restarted_llm = make_llm(always(CritiqueVerdict.PASS))
    second = make_runtime(llm=restarted_llm)
    restarted = client_for(second)
    stored = restarted.get(f"/runs/{run_id}").json()
    old = next(c for c in stored["candidates"] if c["id"] == candidate_id)
    assert old["critiques"][-1]["verdict"] == "pass"
    assert old["critiques"][-1]["score"] == 0.99
    assert old["critiques"][-1]["issues"][0]["category"] == "unclear"
    assert restarted_llm.calls == []
    assert (
        sql(
            second.db.libpq_url,
            "SELECT to_jsonb(k) FROM critiques k WHERE run_id = :id ORDER BY position",
            id=run_id,
        )
        == before
    )
    assert (
        sql(
            second.db.libpq_url,
            "SELECT to_jsonb(p) FROM publications p WHERE id = :id",
            id=publication_id,
        )
        == publication_before
    )

    # The same contradictory judgement is refused for a newly generated run.
    new = client.post(
        "/runs", json=run_payload(max_generation_attempts=1, candidates_per_attempt=1)
    )
    assert new.status_code == 202 and new.json()["status"] == "failed"
    result = client.get(f"/runs/{new.json()['id']}").json()
    assert result["candidates"][0]["critiques"][-1]["verdict"] == "revise"
