"""Typed graph state.

Design notes:
- ``GraphState`` is a Pydantic model so every node update is validated.
- Fields that accumulate across loop iterations (candidates, critiques, errors,
  events, llm_calls, research_fetches) use an append reducer. Each candidate and
  critique carries its ``generation_attempt``, so the full history of a run stays traceable and
  "current" views are derived rather than stored twice.
- Nodes return a partial ``StateUpdate``; they never mutate state in place.
- No credentials ever enter state: providers hold them, state holds only results
  and metadata (and state is what gets checkpointed).
"""

import operator
from datetime import datetime
from typing import Annotated, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from social_growth_agent.models import (
    Account,
    CandidateOrigin,
    ContentCandidate,
    ContentStrategy,
    Critique,
    LLMCall,
    NodeEvent,
    PerformanceInsight,
    PostMetrics,
    PublishState,
    ResearchBrief,
    ResearchFetch,
    ResearchFinding,
    ResearchQuery,
    ResearchSource,
    ReviewAction,
    ReviewDecision,
    ReviewState,
    RunError,
    RunStatus,
    SignalDecision,
    SourcePost,
    new_id,
    utc_now,
)
from social_growth_agent.policies import DEFAULT_MIN_SIGNAL_POSTS, ContentPolicy, CriticGate


class RunConfig(BaseModel):
    """Per-run limits and policies. Every loop in the graph is bounded by one of these."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_generation_attempts: int = Field(default=3, ge=1, le=10)
    candidates_per_attempt: int = Field(default=3, ge=1, le=10)
    max_research_attempts: int = Field(
        default=3, ge=1, le=5, description="Retrievals per run (retries and broadening)."
    )
    min_signal_posts: int = Field(default=DEFAULT_MIN_SIGNAL_POSTS, ge=1, le=100)
    max_edit_rounds: int = Field(default=3, ge=0, le=10)
    max_regenerations: int = Field(default=2, ge=0, le=5)
    research_query: ResearchQuery | None = Field(
        default=None, description="Explicit research query; defaults to the strategy pillars."
    )
    content_policy: ContentPolicy = Field(default_factory=ContentPolicy)
    critic_gate: CriticGate = Field(default_factory=CriticGate)


class GraphState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Identity and inputs
    run_id: str = Field(default_factory=lambda: new_id("run"))
    started_at: datetime = Field(default_factory=utc_now)
    account: Account
    strategy: ContentStrategy
    config: RunConfig = Field(default_factory=RunConfig)

    # Work products. Retrieved posts are kept so evidence ids stay checkable later.
    source_posts: list[SourcePost] = Field(default_factory=list)
    research_source: ResearchSource | None = None
    research: list[ResearchFinding] = Field(default_factory=list)
    research_brief: ResearchBrief | None = None
    candidates: Annotated[list[ContentCandidate], operator.add] = Field(default_factory=list)
    critiques: Annotated[list[Critique], operator.add] = Field(default_factory=list)

    # Decisions and downstream results (metrics/insights unused until later phases)
    review: ReviewState = Field(default_factory=ReviewState)
    publish: PublishState = Field(default_factory=PublishState)
    """Deprecated, never written: publishing happens outside the graph (Phase 5) and its
    state lives in the ``publications`` table. Kept so stored checkpoints validate."""
    metrics: list[PostMetrics] = Field(default_factory=list)
    insights: list[PerformanceInsight] = Field(default_factory=list)

    # Control
    status: RunStatus = RunStatus.RUNNING
    research_attempts: int = Field(default=0, ge=0)
    signal_decision: SignalDecision | None = None
    broaden_step: int = Field(default=0, ge=0)
    generation_attempts: int = Field(default=0, ge=0)
    generation_cycle_start: int = Field(
        default=0, ge=0, description="generation_attempts when the current cycle began."
    )
    regeneration_rounds: int = Field(default=0, ge=0)
    pending_regeneration: bool = False
    errors: Annotated[list[RunError], operator.add] = Field(default_factory=list)
    events: Annotated[list[NodeEvent], operator.add] = Field(default_factory=list)
    llm_calls: Annotated[list[LLMCall], operator.add] = Field(default_factory=list)
    research_fetches: Annotated[list[ResearchFetch], operator.add] = Field(default_factory=list)
    review_decisions: Annotated[list[ReviewDecision], operator.add] = Field(default_factory=list)

    def current_candidates(self) -> list[ContentCandidate]:
        """Model-generated candidates of the latest attempt (human edits excluded)."""
        return [
            c
            for c in self.candidates
            if c.generation_attempt == self.generation_attempts
            and c.origin is CandidateOrigin.GENERATED
        ]

    def cycle_attempts(self) -> int:
        """Generation attempts in the current cycle (a reviewer's regenerate starts a new one)."""
        return self.generation_attempts - self.generation_cycle_start

    def requests_used(self) -> int:
        return sum(f.requests_made for f in self.research_fetches)

    def reviewer_notes(self) -> list[str]:
        """Notes from every regenerate decision, oldest first."""
        return [
            d.note for d in self.review_decisions if d.action is ReviewAction.REGENERATE and d.note
        ]

    def reviewed_feedback(self) -> list[tuple[ContentCandidate, Critique]]:
        """Candidates the reviewer saw (and asked to regenerate) with their latest critiques."""
        pairs = []
        for candidate in self.candidates_by_ids(self.review.candidate_ids):
            critique = self.latest_critique(candidate.id)
            if critique is not None:
                pairs.append((candidate, critique))
        return pairs

    def current_critiques(self) -> list[Critique]:
        return [c for c in self.critiques if c.generation_attempt == self.generation_attempts]

    def reviewable_critiques(self) -> list[Critique]:
        """Passing critiques of the latest attempt, as ordered by the critic gate."""
        return self.config.critic_gate.reviewable(self.current_critiques())

    def critic_gate_open(self) -> bool:
        return self.config.critic_gate.is_open(self.current_critiques())

    def revision_feedback(self) -> list[tuple[ContentCandidate, Critique]]:
        """Non-passing generated candidates of the latest attempt with their critiques."""
        by_id = {c.id: c for c in self.current_candidates()}
        return [
            (by_id[k.candidate_id], k)
            for k in self.current_critiques()
            if not k.passed and k.candidate_id in by_id
        ]

    def candidate(self, candidate_id: str) -> ContentCandidate | None:
        return next((c for c in self.candidates if c.id == candidate_id), None)

    def candidates_by_ids(self, ids: list[str]) -> list[ContentCandidate]:
        by_id = {c.id: c for c in self.candidates}
        return [by_id[i] for i in ids if i in by_id]

    def latest_critique(self, candidate_id: str) -> Critique | None:
        """The most recent critique of a candidate's exact (immutable) content."""
        return next((k for k in reversed(self.critiques) if k.candidate_id == candidate_id), None)

    def critique(self, critique_id: str | None) -> Critique | None:
        return next((k for k in self.critiques if k.id == critique_id), None)


class StateUpdate(TypedDict, total=False):
    """The partial update a node may return. Keys mirror ``GraphState`` fields."""

    source_posts: list[SourcePost]
    research_source: ResearchSource
    research: list[ResearchFinding]
    research_brief: ResearchBrief
    candidates: list[ContentCandidate]
    critiques: list[Critique]
    review: ReviewState
    publish: PublishState
    status: RunStatus
    research_attempts: int
    signal_decision: SignalDecision | None
    broaden_step: int
    generation_attempts: int
    generation_cycle_start: int
    regeneration_rounds: int
    pending_regeneration: bool
    errors: list[RunError]
    events: list[NodeEvent]
    llm_calls: list[LLMCall]
    research_fetches: list[ResearchFetch]
    review_decisions: list[ReviewDecision]
