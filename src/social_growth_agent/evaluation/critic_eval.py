"""Offline evaluation of the Critic against labelled cases.

The same harness will score real LLM critics in Phase 2, where it becomes a
regression gate: a prompt or model change must not lower critic agreement.
"""

from collections import Counter

from pydantic import BaseModel, ConfigDict, computed_field

from social_growth_agent.agents import CriticAgent
from social_growth_agent.models import ContentCandidate, ContentStrategy, CritiqueVerdict
from social_growth_agent.policies import ContentPolicy


class CriticEvalCase(BaseModel):
    """A supplied ground-truth final verdict, not a separate policy-compliance label."""

    model_config = ConfigDict(frozen=True)

    candidate: ContentCandidate
    expected: CritiqueVerdict


class CriticVerdictMetrics(BaseModel):
    """One verdict versus all others, using expected labels and final verdicts.

    TP: expected and actual both equal this verdict. FP: actual equals it but
    expected differs. FN: expected equals it but actual differs.
    """

    model_config = ConfigDict(frozen=True)

    true_positives: int
    false_positives: int
    false_negatives: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def precision(self) -> float | None:
        """TP / (TP + FP); undefined when this verdict is never predicted."""
        predicted = self.true_positives + self.false_positives
        return self.true_positives / predicted if predicted else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def recall(self) -> float | None:
        """TP / (TP + FN); undefined when this verdict has no labelled cases."""
        expected = self.true_positives + self.false_negatives
        return self.true_positives / expected if expected else None


class CriticEvalReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int
    correct: int
    confusion: dict[str, int]  # "expected->actual" -> count

    @property
    def accuracy(self) -> float:
        """Legacy agreement rate; retains the historical 0.0 for an empty report."""
        return self.correct / self.total if self.total else 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def per_verdict(self) -> dict[str, CriticVerdictMetrics]:
        """Metrics for every verdict, including labels absent from the sparse confusion map."""
        return {
            verdict.value: CriticVerdictMetrics(
                true_positives=self.confusion.get(f"{verdict}->{verdict}", 0),
                false_positives=sum(
                    self.confusion.get(f"{other}->{verdict}", 0)
                    for other in CritiqueVerdict
                    if other != verdict
                ),
                false_negatives=sum(
                    self.confusion.get(f"{verdict}->{other}", 0)
                    for other in CritiqueVerdict
                    if other != verdict
                ),
            )
            for verdict in CritiqueVerdict
        }

    @computed_field  # type: ignore[prop-decorator]
    @property
    def false_pass_count(self) -> int:
        """Final PASS with expected REVISE or REJECT: the PASS false-positive count."""
        return self.per_verdict[CritiqueVerdict.PASS].false_positives


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
