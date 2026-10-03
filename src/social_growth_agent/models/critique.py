"""Structured Critic output.

A critique separates three things:
- ``recommended_verdict`` and ``assessment``/``issues``: the model's subjective judgement.
- ``policy_violations``: deterministic hard-rule failures found by application code.
- ``verdict``: the final decision. Application code derives it; a hard violation
  can never produce PASS, whatever the model recommended.
"""

from enum import StrEnum

from pydantic import Field

from social_growth_agent.models.base import DomainModel, new_id


class CritiqueVerdict(StrEnum):
    PASS = "pass"
    REVISE = "revise"
    REJECT = "reject"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToneMatch(StrEnum):
    STRONG = "strong"
    ACCEPTABLE = "acceptable"
    WEAK = "weak"


class IssueCategory(StrEnum):
    """Soft (subjective) problems the model can raise."""

    WEAK_HOOK = "weak_hook"
    UNCLEAR = "unclear"
    LOW_USEFULNESS = "low_usefulness"
    BORING = "boring"
    OFF_BRAND = "off_brand"
    OFF_STRATEGY = "off_strategy"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    NOT_SUITED_TO_X = "not_suited_to_x"
    OTHER = "other"


class CritiqueIssue(DomainModel):
    category: IssueCategory
    detail: str


class PolicyViolation(DomainModel):
    """A hard rule failure detected by application code, never by the model."""

    rule: str
    detail: str


class CritiqueAssessment(DomainModel):
    score: float = Field(ge=0.0, le=1.0, description="Overall quality, 0 to 1.")
    factual_risk: RiskLevel
    originality_risk: RiskLevel
    tone_match: ToneMatch


class Critique(DomainModel):
    id: str = Field(default_factory=lambda: new_id("crit"))
    candidate_id: str
    generation_attempt: int = Field(ge=1)
    verdict: CritiqueVerdict
    recommended_verdict: CritiqueVerdict
    assessment: CritiqueAssessment
    issues: list[CritiqueIssue] = Field(default_factory=list)
    policy_violations: list[PolicyViolation] = Field(default_factory=list)
    suggested_revision: str | None = None

    @property
    def passed(self) -> bool:
        return self.verdict is CritiqueVerdict.PASS

    @property
    def revision_required(self) -> bool:
        return self.verdict is CritiqueVerdict.REVISE
