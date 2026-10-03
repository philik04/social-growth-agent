"""The single place that decides whether a critiqued batch may go to human review."""

from pydantic import BaseModel, ConfigDict, Field

from social_growth_agent.models import Critique


class CriticGate(BaseModel):
    """Phase 2 default: at least one passing candidate.

    Further rules (aggregate score, per-dimension thresholds) belong here, so that
    routing and review never re-implement the decision.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_passing_candidates: int = Field(default=1, ge=1)
    min_best_score: float | None = Field(default=None, ge=0.0, le=1.0)

    def reviewable(self, critiques: list[Critique]) -> list[Critique]:
        """Critiques whose candidates may be shown to a human, best score first."""
        passing = [c for c in critiques if c.passed]
        return sorted(passing, key=lambda c: -c.assessment.score)

    def is_open(self, critiques: list[Critique]) -> bool:
        passing = self.reviewable(critiques)
        if len(passing) < self.min_passing_candidates:
            return False
        if self.min_best_score is not None:
            return passing[0].assessment.score >= self.min_best_score
        return True
