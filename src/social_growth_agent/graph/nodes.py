"""Graph nodes. Each node reads state, calls at most one agent, and returns a partial update.

Nodes contain no prompt text and make no routing decisions; routing lives in
``routing.py`` and business rules in ``policies``.
"""

from langgraph.types import interrupt

from social_growth_agent.agents import ContentAgent, CriticAgent, GenerationContext, ResearchAgent
from social_growth_agent.errors import InvalidReviewError
from social_growth_agent.graph.dependencies import Dependencies
from social_growth_agent.graph.state import GraphState, StateUpdate
from social_growth_agent.models import (
    ReviewAction,
    ReviewDecision,
    ReviewRequest,
    ReviewStatus,
    RunError,
    RunStatus,
)
from social_growth_agent.policies import edited_candidate

_STATUS_BY_ACTION = {
    ReviewAction.APPROVE: RunStatus.APPROVED,
    ReviewAction.REJECT: RunStatus.REJECTED,
    ReviewAction.REGENERATE: RunStatus.REGENERATION_REQUESTED,
}


class WorkflowNodes:
    def __init__(self, deps: Dependencies) -> None:
        settings = deps.agent_settings
        self._research = ResearchAgent(
            deps.llm_for("research"), deps.research_provider, settings.research
        )
        self._content = ContentAgent(deps.llm_for("content"), settings.content)
        self._critic = CriticAgent(deps.llm_for("critic"), settings.critic)

    def research(self, state: GraphState) -> StateUpdate:
        result = self._research.run(state.account, state.strategy)
        return {
            "research": result.findings,
            "research_brief": result.brief,
            "research_attempts": state.research_attempts + 1,
            "llm_calls": [result.call],
        }

    def generate(self, state: GraphState) -> StateUpdate:
        attempt = state.generation_attempts + 1
        ctx = GenerationContext(
            run_id=state.run_id,
            strategy=state.strategy,
            findings=state.research,
            brief=state.research_brief,
            policy=state.config.content_policy,
            attempt=attempt,
            count=state.config.candidates_per_attempt,
            feedback=state.revision_feedback(),
        )
        result = self._content.run(ctx)
        return {
            "candidates": result.candidates,
            "generation_attempts": attempt,
            "llm_calls": [result.call],
        }

    def critic(self, state: GraphState) -> StateUpdate:
        result = self._critic.run(
            state.strategy, state.current_candidates(), state.config.content_policy
        )
        return {"critiques": result.critiques, "llm_calls": [result.call]}

    def critique_edit(self, state: GraphState) -> StateUpdate:
        """Re-critique a human-edited candidate. Only a pass makes it approvable."""
        candidate = state.candidate(state.review.pending_edit_candidate_id or "")
        if candidate is None:
            raise InvalidReviewError("no edited candidate awaiting critique")
        result = self._critic.run(state.strategy, [candidate], state.config.content_policy)
        [critique] = result.critiques
        review = state.review.model_copy(
            update={"rejected_edit_critique_id": None if critique.passed else critique.id}
        )
        return {"critiques": [critique], "review": review, "llm_calls": [result.call]}

    @staticmethod
    def request_review(state: GraphState) -> StateUpdate:
        """Mark the run as awaiting review. Separate from ``human_review`` so a
        paused run's checkpoint already shows the pending status and candidates."""
        ids = [k.candidate_id for k in state.reviewable_critiques()]
        review = state.review.model_copy(
            update={
                "status": ReviewStatus.PENDING,
                "candidate_ids": ids,
                "pending_edit_candidate_id": None,
            }
        )
        return {"review": review, "status": RunStatus.AWAITING_REVIEW}

    @staticmethod
    def human_review(state: GraphState) -> StateUpdate:
        """Pause for a human decision.

        ``interrupt`` checkpoints the run and returns control to the caller. When
        the caller resumes with ``Command(resume=decision)``, this node re-runs and
        ``interrupt`` returns the decision. Everything before it must be side-effect free.
        """
        decision = ReviewDecision.model_validate(
            interrupt(_review_request(state).model_dump(mode="json"))
        )
        review = state.review
        if decision.candidate_id is not None and decision.candidate_id not in review.candidate_ids:
            raise InvalidReviewError(f"candidate {decision.candidate_id} is not under review")

        if decision.action is ReviewAction.EDIT:
            return _apply_edit(state, decision)

        if decision.action is ReviewAction.APPROVE:
            _ensure_passed_critique(state, decision.candidate_id)
        decided = review.model_copy(
            update={
                "status": ReviewStatus.DECIDED,
                "decision": decision,
                "rejected_edit_critique_id": None,
            }
        )
        return {"review": decided, "status": _STATUS_BY_ACTION[decision.action]}

    @staticmethod
    def failed(state: GraphState) -> StateUpdate:
        update: StateUpdate = {"status": RunStatus.FAILED}
        if not state.errors:
            message = f"no candidate passed critique after {state.generation_attempts} attempts"
            update["errors"] = [
                RunError(
                    node="failed",
                    message=message,
                    generation_attempt=state.generation_attempts,
                    error_type="RetryLimitReached",
                )
            ]
        return update


def _review_request(state: GraphState) -> ReviewRequest:
    ids = state.review.candidate_ids
    critiques = [k for i in ids if (k := state.latest_critique(i)) is not None]
    return ReviewRequest(
        run_id=state.run_id,
        candidates=state.candidates_by_ids(ids),
        critiques=critiques,
        rejected_edit=state.critique(state.review.rejected_edit_critique_id),
        edits_remaining=max(0, state.config.max_edit_rounds - state.review.edit_rounds),
    )


def _apply_edit(state: GraphState, decision: ReviewDecision) -> StateUpdate:
    review = state.review
    if review.edit_rounds >= state.config.max_edit_rounds:
        raise InvalidReviewError("edit limit reached; approve, reject or regenerate instead")
    original = state.candidate(decision.candidate_id or "")
    if original is None or decision.edited_content is None:
        raise InvalidReviewError("edit requires an existing candidate and edited content")
    edited = edited_candidate(original, decision.edited_content)
    updated = review.model_copy(
        update={
            "status": ReviewStatus.DECIDED,
            "decision": decision,
            "edit_rounds": review.edit_rounds + 1,
            "pending_edit_candidate_id": edited.id,
            "rejected_edit_critique_id": None,
        }
    )
    return {"candidates": [edited], "review": updated, "status": RunStatus.RUNNING}


def _ensure_passed_critique(state: GraphState, candidate_id: str | None) -> None:
    """Defence in depth: approval requires a passing critique of this exact content."""
    critique = state.latest_critique(candidate_id or "")
    if critique is None or not critique.passed:
        raise InvalidReviewError(f"candidate {candidate_id} has no passing critique")
