"""Graph nodes. Each node reads state, calls at most one agent, and returns a partial update.

Nodes contain no prompt text and make no routing decisions; routing lives in
``routing.py`` and business rules in ``policies``.
"""

from langgraph.types import interrupt

from social_growth_agent.agents import ContentAgent, CriticAgent, GenerationContext, ResearchAgent
from social_growth_agent.errors import (
    InsufficientSignalError,
    InvalidReviewError,
    SocialGrowthError,
    TransientProviderError,
)
from social_growth_agent.graph.dependencies import Dependencies
from social_growth_agent.graph.state import GraphState, StateUpdate
from social_growth_agent.models import (
    FetchOutcome,
    ResearchFetch,
    ResearchQuery,
    ResearchSource,
    ReviewAction,
    ReviewDecision,
    ReviewRequest,
    ReviewStatus,
    RunError,
    RunStatus,
    SignalDecision,
    SourcePost,
)
from social_growth_agent.policies import (
    assess_signal,
    broadened_queries,
    default_research_query,
    edited_candidate,
)

_STATUS_BY_ACTION = {
    ReviewAction.APPROVE: RunStatus.APPROVED,
    ReviewAction.REJECT: RunStatus.REJECTED,
}


class WorkflowNodes:
    def __init__(self, deps: Dependencies) -> None:
        settings = deps.agent_settings
        self._research_provider = deps.research_provider
        self._research = ResearchAgent(deps.llm_for("research"), settings.research)
        self._content = ContentAgent(deps.llm_for("content"), settings.content)
        self._critic = CriticAgent(deps.llm_for("critic"), settings.critic)

    def retrieve(self, state: GraphState) -> StateUpdate:
        """Retrieval only: fetch posts, then apply the deterministic signal check.

        Every attempt (success, empty or transient failure) is recorded as a
        ``ResearchFetch`` in state, so the request budget is checked against what was
        actually sent and no usage is invisible. Transient failures are retried, and
        thin samples broadened, only while both budgets allow:
        ``RunConfig.max_research_attempts`` (retrievals) and the provider's
        ``max_requests_per_run`` (platform requests). Rate limits and other
        non-transient errors are raised and end the run; nothing sleeps.
        """
        provider = self._research_provider
        steps = broadened_queries(self._original_query(state), state.strategy)
        query = steps[min(state.broaden_step, len(steps) - 1)]
        attempts = state.research_attempts + 1
        try:
            result = provider.search(query)
        except TransientProviderError as exc:
            fetch = exc.research_fetch or _failed_fetch(provider.source_name, query, exc)
            if self._can_fetch_again(state, attempts, [fetch]):
                return {
                    "research_fetches": [fetch],
                    "research_attempts": attempts,
                    "signal_decision": SignalDecision.RETRY,
                }
            exc.research_fetch = fetch
            raise

        posts = _merge_posts(state.source_posts, result.posts)
        decision = assess_signal(
            posts=len(posts),
            min_posts=state.config.min_signal_posts,
            can_fetch_again=self._can_fetch_again(state, attempts, [result.fetch]),
            can_broaden=state.broaden_step + 1 < len(steps),
        )
        if decision is None:
            error = InsufficientSignalError(f"no posts found for query {query.text!r}")
            error.research_fetch = result.fetch
            raise error
        update: StateUpdate = {
            "source_posts": posts,
            "research_fetches": [result.fetch],
            "research_attempts": attempts,
            "signal_decision": decision,
        }
        if decision is SignalDecision.BROADEN:
            update["broaden_step"] = state.broaden_step + 1
        if posts:
            update["research_source"] = self._source(state, posts, steps, result.fetch)
        return update

    def _original_query(self, state: GraphState) -> ResearchQuery:
        return state.config.research_query or default_research_query(state.strategy)

    def _can_fetch_again(
        self, state: GraphState, attempts: int, new_fetches: list[ResearchFetch]
    ) -> bool:
        if attempts >= state.config.max_research_attempts:
            return False
        cap = self._research_provider.max_requests_per_run
        used = state.requests_used() + sum(f.requests_made for f in new_fetches)
        return cap is None or used < cap

    def _source(
        self,
        state: GraphState,
        posts: list[SourcePost],
        steps: list[ResearchQuery],
        fetch: ResearchFetch,
    ) -> ResearchSource:
        first = next(
            (f for f in state.research_fetches if f.outcome is not FetchOutcome.ERROR), fetch
        )
        used_steps = steps[1 : state.broaden_step + 1]
        return ResearchSource(
            provider=self._research_provider.source_name,
            source_ids=[p.source_id for p in posts],
            synthetic=self._research_provider.synthetic,
            query=steps[0].text,
            effective_query=first.effective_query,
            retrieved_at=first.started_at,
            broadened_queries=[q.text for q in used_steps],
        )

    def research(self, state: GraphState) -> StateUpdate:
        """Interpretation only: the Research Agent reads the retrieved posts."""
        if state.research_source is None:
            raise InsufficientSignalError("no research material was retrieved")
        result = self._research.run(
            state.account, state.strategy, state.source_posts, state.research_source
        )
        return {
            "research": result.findings,
            "research_brief": result.brief,
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
            feedback=(
                state.reviewed_feedback()
                if state.pending_regeneration
                else state.revision_feedback()
            ),
            reviewer_notes=state.reviewer_notes(),
        )
        result = self._content.run(ctx)
        return {
            "candidates": result.candidates,
            "generation_attempts": attempt,
            "pending_regeneration": False,
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
        validate_review(state, decision)
        if decision.action is ReviewAction.EDIT:
            return _apply_edit(state, decision)
        if decision.action is ReviewAction.REGENERATE:
            return _apply_regenerate(state, decision)

        decided = state.review.model_copy(
            update={
                "status": ReviewStatus.DECIDED,
                "decision": decision,
                "rejected_edit_critique_id": None,
            }
        )
        return {
            "review": decided,
            "review_decisions": [decision],
            "status": _STATUS_BY_ACTION[decision.action],
        }

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


def validate_review(state: GraphState, decision: ReviewDecision) -> None:
    """Every rule a review decision must satisfy against the paused run.

    Called by ``human_review`` and, before resuming, by the run service, so an invalid
    decision is rejected up front and never consumes the pending interrupt.
    """
    review = state.review
    if review.status is not ReviewStatus.PENDING:
        raise InvalidReviewError(f"run {state.run_id} is not awaiting review")
    if decision.candidate_id is not None and decision.candidate_id not in review.candidate_ids:
        raise InvalidReviewError(f"candidate {decision.candidate_id} is not under review")
    if decision.action is ReviewAction.APPROVE:
        _ensure_passed_critique(state, decision.candidate_id)
    elif decision.action is ReviewAction.EDIT:
        if review.edit_rounds >= state.config.max_edit_rounds:
            raise InvalidReviewError("edit limit reached; approve, reject or regenerate instead")
        if decision.candidate_id is None or decision.edited_content is None:
            raise InvalidReviewError("edit requires an existing candidate and edited content")
    elif (
        decision.action is ReviewAction.REGENERATE
        and state.regeneration_rounds >= state.config.max_regenerations
    ):
        raise InvalidReviewError("regeneration limit reached; approve, edit or reject instead")


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
    original = state.candidate(decision.candidate_id or "")
    if original is None or decision.edited_content is None:
        raise InvalidReviewError("edit requires an existing candidate and edited content")
    edited = edited_candidate(original, decision.edited_content)
    decision = decision.model_copy(update={"resulting_candidate_id": edited.id})
    updated = review.model_copy(
        update={
            "status": ReviewStatus.DECIDED,
            "decision": decision,
            "edit_rounds": review.edit_rounds + 1,
            "pending_edit_candidate_id": edited.id,
            "rejected_edit_critique_id": None,
        }
    )
    return {
        "candidates": [edited],
        "review": updated,
        "review_decisions": [decision],
        "status": RunStatus.RUNNING,
    }


def _apply_regenerate(state: GraphState, decision: ReviewDecision) -> StateUpdate:
    """Start a new, bounded generation cycle guided by the reviewer's notes.

    Research is not repeated: the next node is ``generate``, never ``retrieve``.
    """
    review = state.review.model_copy(
        update={
            "status": ReviewStatus.DECIDED,
            "decision": decision,
            "rejected_edit_critique_id": None,
        }
    )
    return {
        "review": review,
        "review_decisions": [decision],
        "status": RunStatus.RUNNING,
        "regeneration_rounds": state.regeneration_rounds + 1,
        "generation_cycle_start": state.generation_attempts,
        "pending_regeneration": True,
    }


def _merge_posts(existing: list[SourcePost], new: list[SourcePost]) -> list[SourcePost]:
    """Posts from all retrievals of a run, first occurrence wins (stable source ids)."""
    seen = {p.source_id for p in existing}
    merged = list(existing)
    for post in new:
        if post.source_id not in seen:
            seen.add(post.source_id)
            merged.append(post)
    return merged


def _failed_fetch(provider: str, query: ResearchQuery, exc: SocialGrowthError) -> ResearchFetch:
    """Fetch record for a provider that raised without attaching one (e.g. the mock)."""
    return ResearchFetch(
        provider=provider,
        query=query.text,
        effective_query=query.text,
        max_results=query.max_results or 0,
        requests_made=0,
        posts_fetched=0,
        users_fetched=0,
        latency_ms=0.0,
        outcome=FetchOutcome.ERROR,
        error_category=getattr(exc, "category", None),
    )


def _ensure_passed_critique(state: GraphState, candidate_id: str | None) -> None:
    """Defence in depth: approval requires a passing critique of this exact content."""
    critique = state.latest_critique(candidate_id or "")
    if critique is None or not critique.passed:
        raise InvalidReviewError(f"candidate {candidate_id} has no passing critique")
