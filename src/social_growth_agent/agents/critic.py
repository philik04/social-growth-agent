"""Critic Agent: LLM judgement first, deterministic policy last.

The model returns one structured evaluation per candidate. Application code then
runs the content policy's hard rules and derives the final verdict. A hard
violation can never result in PASS, whatever the model recommended.
"""

import json
from dataclasses import dataclass

from pydantic import BaseModel, Field, JsonValue

from social_growth_agent.agents.base import call_llm, load_prompt, validating_output
from social_growth_agent.errors import AgentOutputError
from social_growth_agent.models import (
    ContentCandidate,
    ContentStrategy,
    Critique,
    CritiqueAssessment,
    CritiqueIssue,
    CritiqueVerdict,
    IssueCategory,
    LLMCall,
    RiskLevel,
    ToneMatch,
)
from social_growth_agent.policies import ContentPolicy, final_verdict, find_violations
from social_growth_agent.providers import LLMProvider, LLMRequest, LLMSettings


class IssueDraft(BaseModel):
    category: IssueCategory
    detail: str


class CandidateEvaluation(BaseModel):
    candidate_id: str
    recommendation: CritiqueVerdict
    score: float = Field(description="Overall quality from 0 to 1.")
    tone_match: ToneMatch
    factual_risk: RiskLevel
    originality_risk: RiskLevel
    issues: list[IssueDraft]
    suggested_revision: str | None


class CriticReport(BaseModel):
    evaluations: list[CandidateEvaluation]


@dataclass(frozen=True)
class CriticResult:
    critiques: list[Critique]
    call: LLMCall


class CriticAgent:
    name = "critic"

    def __init__(self, llm: LLMProvider, settings: LLMSettings | None = None) -> None:
        self._llm = llm
        self._settings = settings or LLMSettings(temperature=0.0)

    def run(
        self,
        strategy: ContentStrategy,
        candidates: list[ContentCandidate],
        policy: ContentPolicy,
    ) -> CriticResult:
        result = call_llm(self._llm, self._request(strategy, candidates, policy), CriticReport)
        with validating_output(result.call):
            evaluations = _match_one_to_one(result.output.evaluations, candidates)
            critiques = [_to_critique(evaluations[c.id], c, policy) for c in candidates]
        return CriticResult(critiques=critiques, call=result.call)

    def _request(
        self, strategy: ContentStrategy, candidates: list[ContentCandidate], policy: ContentPolicy
    ) -> LLMRequest:
        payload: dict[str, JsonValue] = {
            "strategy": strategy.model_dump(mode="json"),
            "policy": policy.prompt_payload(),
            "candidates": [
                {**c.model_dump(mode="json"), "length": len(c.content)} for c in candidates
            ],
        }
        return LLMRequest(
            agent=self.name,
            task="critic.review",
            system=load_prompt("critic"),
            prompt=f"Evaluate these {len(candidates)} candidates.\n{json.dumps(payload, indent=1)}",
            payload=payload,
            generation_attempt=max((c.generation_attempt for c in candidates), default=0),
            settings=self._settings,
        )


def _match_one_to_one(
    evaluations: list[CandidateEvaluation], candidates: list[ContentCandidate]
) -> dict[str, CandidateEvaluation]:
    by_id = {e.candidate_id: e for e in evaluations}
    expected = {c.id for c in candidates}
    if len(by_id) != len(evaluations) or set(by_id) != expected:
        raise AgentOutputError(
            "critic must return exactly one critique per candidate; "
            f"expected {sorted(expected)}, got {[e.candidate_id for e in evaluations]}"
        )
    return by_id


def _to_critique(
    evaluation: CandidateEvaluation, candidate: ContentCandidate, policy: ContentPolicy
) -> Critique:
    violations = find_violations(candidate, policy)
    suggestion = evaluation.suggested_revision
    if violations and suggestion is None:
        suggestion = "Fix the policy violations: " + "; ".join(v.detail for v in violations)
    return Critique(
        candidate_id=candidate.id,
        generation_attempt=candidate.generation_attempt,
        verdict=final_verdict(evaluation.recommendation, violations),
        recommended_verdict=evaluation.recommendation,
        assessment=CritiqueAssessment(
            score=evaluation.score,
            factual_risk=evaluation.factual_risk,
            originality_risk=evaluation.originality_risk,
            tone_match=evaluation.tone_match,
        ),
        issues=[CritiqueIssue(category=i.category, detail=i.detail) for i in evaluation.issues],
        policy_violations=violations,
        suggested_revision=suggestion,
    )
