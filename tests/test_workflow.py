"""End-to-end graph behaviour with deterministic fake agents."""

import pytest

from social_growth_agent.agents import CandidateBatch, CriticReport
from social_growth_agent.agents.fakes import fake_content
from social_growth_agent.errors import (
    InvalidModelOutputError,
    InvalidReviewError,
    ProviderError,
)
from social_growth_agent.graph import RunConfig
from social_growth_agent.models import (
    CritiqueVerdict,
    LLMCallOutcome,
    ReviewAction,
    ReviewDecision,
    ReviewStatus,
    RunStatus,
)
from social_growth_agent.policies import ContentPolicy
from social_growth_agent.providers.mocks import MockResearchProvider
from tests.conftest import always, make_llm, make_service

P, R = CritiqueVerdict.PASS, CritiqueVerdict.REVISE


def node_path(result) -> list[str]:
    return [e.node for e in result.state.events]


def test_successful_flow_pauses_for_human_review(account, strategy):
    result = make_service(make_llm(always(P))).start_run(account, strategy)

    assert result.status is RunStatus.AWAITING_REVIEW
    assert node_path(result) == ["retrieve", "research", "generate", "critic", "request_review"]
    assert result.state.generation_attempts == 1
    assert result.state.review.status is ReviewStatus.PENDING
    assert result.pending_review is not None
    assert [c.id for c in result.pending_review.candidates] == result.state.review.candidate_ids
    assert {k.candidate_id for k in result.pending_review.critiques} == set(
        result.state.review.candidate_ids
    )


def test_one_passing_candidate_is_enough_for_review(account, strategy):
    llm = make_llm(lambda _a, i: P if i == 2 else R)
    result = make_service(llm).start_run(account, strategy)

    assert result.status is RunStatus.AWAITING_REVIEW
    assert result.state.generation_attempts == 1
    assert len(result.pending_review.candidates) == 1


def test_no_passing_candidates_triggers_retry(account, strategy):
    llm = make_llm(lambda attempt, _i: P if attempt == 2 else R)
    result = make_service(llm).start_run(account, strategy)

    assert result.status is RunStatus.AWAITING_REVIEW
    assert node_path(result) == [
        "retrieve",
        "research",
        "generate",
        "critic",
        "generate",
        "critic",
        "request_review",
    ]
    assert result.state.generation_attempts == 2
    assert {c.generation_attempt for c in result.pending_review.candidates} == {2}


def test_previous_critique_is_supplied_to_next_generation(account, strategy):
    llm = make_llm(lambda attempt, _i: P if attempt == 2 else R)
    state = make_service(llm).start_run(account, strategy).state

    content_calls = [c for c in llm.calls if c.task == "content.generate"]
    assert content_calls[0].payload["previous_critique"] == []
    feedback = content_calls[1].payload["previous_critique"]
    first_attempt_ids = [c.id for c in state.candidates if c.generation_attempt == 1]
    assert [f["candidate_id"] for f in feedback] == first_attempt_ids
    assert feedback[0]["suggested_revision"] == "Lead with a concrete number."
    assert feedback[0]["issues"] == [{"category": "weak_hook", "detail": "weak hook"}]
    assert "Revise using the previous critique" in content_calls[1].prompt
    # attempt-2 candidates point back at the attempt-1 candidates they revise
    second = [c for c in state.candidates if c.generation_attempt == 2]
    assert [c.revises_candidate_id for c in second] == first_attempt_ids


def test_retry_limit_reached_ends_in_failure(account, strategy):
    config = RunConfig(max_generation_attempts=2)
    result = make_service(make_llm(always(R))).start_run(account, strategy, config)

    assert result.status is RunStatus.FAILED
    assert node_path(result) == [
        "retrieve",
        "research",
        "generate",
        "critic",
        "generate",
        "critic",
        "failed",
    ]
    assert result.pending_review is None
    assert result.state.errors[-1].node == "failed"
    assert result.state.errors[-1].error_type == "RetryLimitReached"
    assert "2 attempts" in result.state.errors[-1].message


def test_state_accumulates_history_with_traceability(account, strategy):
    llm = make_llm(lambda attempt, _i: P if attempt == 2 else R)
    state = make_service(llm).start_run(account, strategy).state

    assert state.research_attempts == 1
    assert state.research_brief is not None
    assert state.research_brief.source.provider == "mock_fixtures"
    assert len(state.candidates) == 6  # 3 per attempt, both attempts kept
    assert len(state.critiques) == 6
    finding_ids = {f.id for f in state.research}
    for cand in state.candidates:
        assert cand.run_id == state.run_id
        assert cand.strategy_id == strategy.id
        assert cand.strategy_version == strategy.version
        assert set(cand.research_finding_ids) <= finding_ids
        assert cand.rationale
    assert {k.candidate_id for k in state.critiques} == {c.id for c in state.candidates}
    assert [c.generation_attempt for c in state.current_candidates()] == [2, 2, 2]
    assert [c.agent for c in state.llm_calls] == [
        "research",
        "content",
        "critic",
        "content",
        "critic",
    ]
    assert all(c.outcome is LLMCallOutcome.SUCCESS for c in state.llm_calls)
    assert [c.generation_attempt for c in state.llm_calls] == [0, 1, 1, 2, 2]


def test_same_inputs_give_same_path_and_content(account, strategy):
    def run():
        result = make_service(make_llm(lambda a, i: P if (a, i) == (2, 1) else R)).start_run(
            account, strategy
        )
        return node_path(result), [c.content for c in result.state.candidates]

    assert run() == run()


# --- hard policy -----------------------------------------------------------------


def long_content(request):
    batch = fake_content(request)
    return CandidateBatch(
        candidates=[d.model_copy(update={"content": "x" * 300}) for d in batch.candidates]
    )


def test_overlong_candidates_fail_even_when_model_passes_them(account, strategy):
    llm = make_llm(always(P), content=long_content)
    result = make_service(llm).start_run(account, strategy, RunConfig(max_generation_attempts=2))

    assert result.status is RunStatus.FAILED
    assert all(k.recommended_verdict is P for k in result.state.critiques)
    assert all(k.verdict is R for k in result.state.critiques)
    assert all(k.policy_violations[0].rule == "max_post_length" for k in result.state.critiques)


def test_max_post_length_is_configurable_per_run(account, strategy):
    config = RunConfig(content_policy=ContentPolicy(max_post_length=400))
    result = make_service(make_llm(always(P), content=long_content)).start_run(
        account, strategy, config
    )
    assert result.status is RunStatus.AWAITING_REVIEW


# --- failure handling -------------------------------------------------------------


def test_transient_provider_failure_is_retried(account, strategy):
    research = MockResearchProvider(fail_first=2)
    result = make_service(make_llm(always(P)), research).start_run(account, strategy)

    assert result.status is RunStatus.AWAITING_REVIEW
    assert len(research.calls) == 3


def test_transient_failure_exhausting_retries_fails_run_without_raising(account, strategy):
    llm = make_llm(always(P), fail_first=10)
    result = make_service(llm).start_run(account, strategy)

    assert result.status is RunStatus.FAILED
    assert len(llm.calls) == 3  # FAST_RETRY.max_attempts, then the error handler
    assert node_path(result) == ["retrieve", "research", "failed"]
    [error] = result.state.errors
    assert (error.node, error.error_type) == ("research", "TransientProviderError")
    assert result.state.llm_calls[-1].outcome is LLMCallOutcome.PROVIDER_ERROR


def test_non_retryable_provider_exception_fails_run_predictably(account, strategy):
    llm = make_llm(always(P), fail_first=1, failure=ProviderError)
    result = make_service(llm).start_run(account, strategy)

    assert result.status is RunStatus.FAILED
    assert len(llm.calls) == 1  # not retried
    [error] = result.state.errors
    assert (error.node, error.error_type) == ("research", "ProviderError")
    assert result.pending_review is None


def test_invalid_structured_output_fails_run_with_error(account, strategy):
    llm = make_llm(always(P))

    def wrong_type(_request):
        return CriticReport(evaluations=[])  # a valid model, but not a CandidateBatch

    llm._handlers[CandidateBatch] = wrong_type
    result = make_service(llm).start_run(account, strategy)

    assert result.status is RunStatus.FAILED
    assert node_path(result)[-2:] == ["generate", "failed"]
    [error] = result.state.errors
    assert (error.node, error.error_type) == ("generate", "InvalidModelOutputError")
    assert result.state.llm_calls[-1].outcome is LLMCallOutcome.INVALID_OUTPUT


def test_model_output_violating_contract_fails_run(account, strategy):
    llm = make_llm(always(P))
    llm._handlers[CriticReport] = lambda _req: CriticReport(evaluations=[])  # drops candidates
    result = make_service(llm).start_run(account, strategy)

    assert result.status is RunStatus.FAILED
    assert node_path(result)[-2:] == ["critic", "failed"]
    assert result.state.errors[0].node == "critic"
    assert "exactly one critique per candidate" in result.state.errors[0].message


def test_domain_validation_failure_is_invalid_output(account, strategy):
    def bad_score(request):
        from tests.conftest import scripted_critic

        report = scripted_critic(always(P))(request)
        broken = [e.model_copy(update={"score": 7.0}) for e in report.evaluations]
        return CriticReport(evaluations=broken)

    llm = make_llm(always(P))
    llm._handlers[CriticReport] = bad_score
    result = make_service(llm).start_run(account, strategy)

    assert result.status is RunStatus.FAILED
    assert result.state.errors[0].error_type == "InvalidModelOutputError"


def test_invalid_output_error_type_is_an_abort_not_a_retry(account, strategy):
    llm = make_llm(always(P))

    def raise_invalid(_request):
        raise InvalidModelOutputError("truncated")

    llm._handlers[CandidateBatch] = raise_invalid
    result = make_service(llm).start_run(account, strategy)

    assert result.status is RunStatus.FAILED
    assert len([c for c in llm.calls if c.task == "content.generate"]) == 1


# --- human review -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (ReviewAction.APPROVE, RunStatus.APPROVED),
        (ReviewAction.REJECT, RunStatus.REJECTED),
    ],
)
def test_resume_after_human_review(account, strategy, action, expected):
    service = make_service(make_llm(always(P)))
    paused = service.start_run(account, strategy)
    candidate_id = paused.pending_review.candidates[0].id
    decision = ReviewDecision(
        action=action,
        candidate_id=candidate_id if action is ReviewAction.APPROVE else None,
        reviewer="philipp",
    )

    result = service.submit_review(paused.state.run_id, decision)

    assert result.status is expected
    assert result.pending_review is None
    assert result.state.review.status is ReviewStatus.DECIDED
    stored = result.state.review.decision
    # The application records which review request the decision answered.
    assert stored.reviewed_candidate_ids == paused.state.review.candidate_ids
    assert stored == decision.model_copy(
        update={"reviewed_candidate_ids": stored.reviewed_candidate_ids}
    )
    assert node_path(result)[-1] == "human_review"


def test_regenerate_starts_a_new_bounded_cycle_with_reviewer_notes(account, strategy):
    llm = make_llm(always(P))
    research = MockResearchProvider()
    service = make_service(llm, research)
    paused = service.start_run(account, strategy)
    reviewed = set(paused.state.review.candidate_ids)
    decision = ReviewDecision(
        action=ReviewAction.REGENERATE, reviewer="philipp", note="shorter, more technical"
    )

    result = service.submit_review(paused.state.run_id, decision)

    assert result.status is RunStatus.AWAITING_REVIEW
    assert len(research.calls) == 1  # no new retrieval
    new_ids = set(result.state.review.candidate_ids)
    assert new_ids and not new_ids & reviewed
    assert result.state.regeneration_rounds == 1
    [recorded] = result.state.review_decisions
    assert set(recorded.reviewed_candidate_ids) == reviewed
    assert recorded == decision.model_copy(
        update={"reviewed_candidate_ids": recorded.reviewed_candidate_ids}
    )
    content_request = [c for c in llm.calls if c.agent == "content"][-1]
    assert content_request.payload["reviewer_notes"] == ["shorter, more technical"]
    assert {i["candidate_id"] for i in content_request.payload["previous_critique"]} == reviewed
    for cand in result.state.candidates_by_ids(list(new_ids)):
        assert "shorter, more technical" in cand.content
        assert cand.revises_candidate_id in reviewed


def test_regeneration_is_bounded(account, strategy):
    service = make_service(make_llm(always(P)))
    run = service.start_run(account, strategy, RunConfig(max_regenerations=1))
    regen = ReviewDecision(action=ReviewAction.REGENERATE, reviewer="p")
    run = service.submit_review(run.state.run_id, regen)
    assert run.status is RunStatus.AWAITING_REVIEW

    with pytest.raises(InvalidReviewError, match="regeneration limit"):
        service.submit_review(run.state.run_id, regen)
    assert service.get_run(run.state.run_id).status is RunStatus.AWAITING_REVIEW


def test_review_for_unknown_candidate_keeps_run_paused(account, strategy):
    service = make_service(make_llm(always(P)))
    paused = service.start_run(account, strategy)
    bad = ReviewDecision(action=ReviewAction.APPROVE, candidate_id="cand_nope", reviewer="p")

    with pytest.raises(InvalidReviewError):
        service.submit_review(paused.state.run_id, bad)

    still = service.get_run(paused.state.run_id)
    assert still.status is RunStatus.AWAITING_REVIEW
    assert still.pending_review is not None


def test_cannot_review_a_run_that_is_not_paused(account, strategy):
    service = make_service(make_llm(always(R)))
    failed = service.start_run(account, strategy, RunConfig(max_generation_attempts=1))
    decision = ReviewDecision(action=ReviewAction.REJECT, reviewer="p")

    with pytest.raises(InvalidReviewError):
        service.submit_review(failed.state.run_id, decision)


def test_demo_runs_retry_loop_end_to_end(capsys):
    from social_growth_agent.services.demo import main

    main()
    out = capsys.readouterr().out
    assert "awaiting_review after 2 attempts" in out
    assert "route: critic -> generate" in out
    assert "resumed: approved" in out
