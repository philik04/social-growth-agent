"""Invariant: materially edited content is critiqued again before it can be approved."""

import pytest

from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.errors import InvalidReviewError
from social_growth_agent.graph import Dependencies, RunConfig
from social_growth_agent.models import (
    CandidateOrigin,
    CritiqueVerdict,
    ReviewAction,
    ReviewDecision,
    RunStatus,
)
from social_growth_agent.providers.mocks import MockResearchProvider
from social_growth_agent.services import WorkflowService
from tests.conftest import FAST_RETRY, always, make_llm, make_service


def rule_based_service() -> WorkflowService:
    """Critic that flags unsupported claims, so edits can pass or fail on content."""
    deps = Dependencies(llm=build_fake_llm(), research_provider=MockResearchProvider())
    return WorkflowService(deps, retry_policy=FAST_RETRY)


def edit(candidate_id: str, content: str) -> ReviewDecision:
    return ReviewDecision(
        action=ReviewAction.EDIT, candidate_id=candidate_id, edited_content=content, reviewer="p"
    )


def approve(candidate_id: str) -> ReviewDecision:
    return ReviewDecision(action=ReviewAction.APPROVE, candidate_id=candidate_id, reviewer="p")


def node_path(result) -> list[str]:
    return [e.node for e in result.state.events]


def test_edit_is_recritiqued_before_it_can_be_approved(account, strategy):
    service = rule_based_service()
    paused = service.start_run(account, strategy)
    original = paused.pending_review.candidates[0]

    after_edit = service.submit_review(
        paused.state.run_id, edit(original.id, "A careful, sourced post.")
    )

    # not approved: the edit went through the critic and the run paused again
    assert after_edit.status is RunStatus.AWAITING_REVIEW
    assert node_path(after_edit)[-3:] == ["human_review", "critique_edit", "request_review"]
    edited = after_edit.state.candidates[-1]
    assert edited.origin is CandidateOrigin.HUMAN_EDIT
    assert edited.revises_candidate_id == original.id
    assert edited.content == "A careful, sourced post."
    assert original.content != edited.content  # original is untouched
    assert after_edit.state.latest_critique(edited.id).passed
    assert edited.id in after_edit.state.review.candidate_ids
    assert after_edit.pending_review.edits_remaining == 2

    approved = service.submit_review(paused.state.run_id, approve(edited.id))
    assert approved.status is RunStatus.APPROVED
    assert approved.state.review.decision.candidate_id == edited.id


def test_edit_that_fails_recritique_cannot_be_approved(account, strategy):
    service = rule_based_service()
    paused = service.start_run(account, strategy)
    original = paused.pending_review.candidates[0]

    after_edit = service.submit_review(
        paused.state.run_id, edit(original.id, "This is guaranteed to 10x your reach.")
    )

    edited = after_edit.state.candidates[-1]
    assert after_edit.status is RunStatus.AWAITING_REVIEW
    assert edited.id not in after_edit.state.review.candidate_ids
    assert after_edit.pending_review.rejected_edit.candidate_id == edited.id
    assert after_edit.pending_review.rejected_edit.verdict is CritiqueVerdict.REVISE

    with pytest.raises(InvalidReviewError):
        service.submit_review(paused.state.run_id, approve(edited.id))
    assert service.get_run(paused.state.run_id).status is RunStatus.AWAITING_REVIEW


def test_edit_violating_hard_policy_fails_even_if_model_passes_it(account, strategy):
    service = make_service(make_llm(always(CritiqueVerdict.PASS)))
    paused = service.start_run(account, strategy)
    original = paused.pending_review.candidates[0]

    after_edit = service.submit_review(paused.state.run_id, edit(original.id, "y" * 281))

    critique = after_edit.pending_review.rejected_edit
    assert critique.recommended_verdict is CritiqueVerdict.PASS
    assert critique.verdict is CritiqueVerdict.REVISE
    assert [v.rule for v in critique.policy_violations] == ["max_post_length"]


def test_non_material_edit_is_refused(account, strategy):
    service = rule_based_service()
    paused = service.start_run(account, strategy)
    original = paused.pending_review.candidates[0]

    with pytest.raises(InvalidReviewError, match="no material change"):
        service.submit_review(paused.state.run_id, edit(original.id, f"  {original.content}  "))
    assert service.get_run(paused.state.run_id).status is RunStatus.AWAITING_REVIEW


def test_edit_rounds_are_bounded(account, strategy):
    service = make_service(make_llm(always(CritiqueVerdict.PASS)))
    paused = service.start_run(account, strategy, RunConfig(max_edit_rounds=1))
    original = paused.pending_review.candidates[0]
    after_edit = service.submit_review(paused.state.run_id, edit(original.id, "first edit"))
    assert after_edit.pending_review.edits_remaining == 0

    with pytest.raises(InvalidReviewError, match="edit limit"):
        service.submit_review(paused.state.run_id, edit(original.id, "second edit"))
