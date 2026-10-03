"""Offline evaluation of the Critic against labelled cases.

The same harness will score real LLM critics in Phase 2, where it becomes a
regression gate: a prompt or model change must not lower critic agreement.
"""

from collections import Counter

from pydantic import BaseModel, ConfigDict

from social_growth_agent.agents import CriticAgent
from social_growth_agent.models import ContentCandidate, ContentStrategy, CritiqueVerdict
from social_growth_agent.policies import ContentPolicy


class CriticEvalCase(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate: ContentCandidate
    expected: CritiqueVerdict


class CriticEvalReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int
    correct: int
    confusion: dict[str, int]  # "expected->actual" -> count

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


def evaluate_critic(
    critic: CriticAgent,
    strategy: ContentStrategy,
    cases: list[CriticEvalCase],
    policy: ContentPolicy | None = None,
) -> CriticEvalReport:
    """Scores final verdicts (model judgement plus hard policy), as the graph would see them."""
    result = critic.run(strategy, [case.candidate for case in cases], policy or ContentPolicy())
    critiques = result.critiques
    actual = {c.candidate_id: c.verdict for c in critiques}
    pairs = Counter(f"{case.expected}->{actual[case.candidate.id]}" for case in cases)
    correct = sum(n for key, n in pairs.items() if key.split("->")[0] == key.split("->")[1])
    return CriticEvalReport(total=len(cases), correct=correct, confusion=dict(pairs))
