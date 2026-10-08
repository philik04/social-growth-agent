"""Content Agent: drafts post candidates from research, strategy, policy and critique."""

import json
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, JsonValue

from social_growth_agent.agents.base import call_llm, load_prompt, validating_output
from social_growth_agent.errors import AgentOutputError, EmptyModelOutputError
from social_growth_agent.models import (
    ContentCandidate,
    ContentStrategy,
    Critique,
    HookType,
    LLMCall,
    PostFormat,
    ResearchBrief,
    ResearchFinding,
)
from social_growth_agent.policies import ContentPolicy
from social_growth_agent.providers import LLMProvider, LLMRequest, LLMSettings


class CandidateDraft(BaseModel):
    content: str
    topic: str
    hook_type: HookType
    format: PostFormat
    target_audience: str
    research_finding_ids: list[str]
    rationale: str
    revises_candidate_id: str | None = Field(
        description="Previous candidate this improves on, or null for a new idea."
    )


class CandidateBatch(BaseModel):
    candidates: list[CandidateDraft]


@dataclass(frozen=True)
class GenerationContext:
    """Everything the Content Agent sees for one attempt."""

    run_id: str
    strategy: ContentStrategy
    findings: list[ResearchFinding]
    brief: ResearchBrief | None
    policy: ContentPolicy
    attempt: int
    count: int
    feedback: list[tuple[ContentCandidate, Critique]]
    reviewer_notes: list[str] = field(default_factory=list)
    """Human reviewer guidance from 'regenerate' decisions (Phase 4), oldest first."""


@dataclass(frozen=True)
class ContentResult:
    candidates: list[ContentCandidate]
    call: LLMCall


class ContentAgent:
    name = "content"

    def __init__(self, llm: LLMProvider, settings: LLMSettings | None = None) -> None:
        self._llm = llm
        self._settings = settings or LLMSettings(temperature=0.7)

    def run(self, ctx: GenerationContext) -> ContentResult:
        result = call_llm(self._llm, self._request(ctx), CandidateBatch)
        with validating_output(result.call):
            candidates = _to_candidates(result.output, ctx)
        return ContentResult(candidates=candidates, call=result.call)

    def _request(self, ctx: GenerationContext) -> LLMRequest:
        payload: dict[str, JsonValue] = {
            "attempt": ctx.attempt,
            "count": ctx.count,
            "strategy": ctx.strategy.model_dump(mode="json"),
            "policy": ctx.policy.prompt_payload(),
            "findings": [f.model_dump(mode="json") for f in ctx.findings],
            "opportunities": (
                [o.model_dump(mode="json") for o in ctx.brief.opportunities] if ctx.brief else []
            ),
            "previous_critique": [_feedback_item(c, k) for c, k in ctx.feedback],
            "reviewer_notes": list(ctx.reviewer_notes),
        }
        header = f"Generation attempt {ctx.attempt}. Write exactly {ctx.count} candidates."
        if ctx.feedback:
            header += " Revise using the previous critique below."
        if ctx.reviewer_notes:
            header += " A human reviewer asked for changes; follow the reviewer notes below."
        return LLMRequest(
            agent=self.name,
            task="content.generate",
            system=load_prompt("content"),
            prompt=f"{header}\n{json.dumps(payload, indent=1)}",
            payload=payload,
            generation_attempt=ctx.attempt,
            settings=self._settings,
        )


def _feedback_item(candidate: ContentCandidate, critique: Critique) -> dict[str, JsonValue]:
    return {
        "candidate_id": candidate.id,
        "previous_content": candidate.content,
        "verdict": critique.verdict.value,
        "issues": [i.model_dump(mode="json") for i in critique.issues],
        "policy_violations": [v.model_dump(mode="json") for v in critique.policy_violations],
        "suggested_revision": critique.suggested_revision,
    }


def _to_candidates(batch: CandidateBatch, ctx: GenerationContext) -> list[ContentCandidate]:
    if not batch.candidates:
        raise EmptyModelOutputError("content agent returned no candidates")
    if len(batch.candidates) > ctx.count:
        raise AgentOutputError(f"asked for {ctx.count} candidates, got {len(batch.candidates)}")
    known_findings = {f.id for f in ctx.findings}
    previous_ids = {c.id for c, _ in ctx.feedback}
    return [_to_candidate(d, ctx, known_findings, previous_ids) for d in batch.candidates]


def _to_candidate(
    draft: CandidateDraft, ctx: GenerationContext, known_findings: set[str], previous_ids: set[str]
) -> ContentCandidate:
    if not draft.research_finding_ids:
        raise AgentOutputError("candidate cites no research findings")
    unknown = set(draft.research_finding_ids) - known_findings
    if unknown:
        raise AgentOutputError(f"candidate cites unknown findings: {sorted(unknown)}")
    revises = draft.revises_candidate_id
    if revises is not None and revises not in previous_ids:
        raise AgentOutputError(f"candidate revises unknown candidate {revises}")
    return ContentCandidate(
        run_id=ctx.run_id,
        generation_attempt=ctx.attempt,
        strategy_id=ctx.strategy.id,
        strategy_version=ctx.strategy.version,
        **draft.model_dump(),
    )
