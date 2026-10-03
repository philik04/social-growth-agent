"""Human-readable run trace for demos: node, attempt, key structured result, route taken."""

from collections.abc import Mapping
from typing import Any

from social_growth_agent.models import (
    ContentCandidate,
    Critique,
    LLMCall,
    ResearchBrief,
    ResearchFinding,
    ReviewState,
    RunError,
    RunStatus,
)


class TracePrinter:
    """A ``RunObserver`` that prints one block per node and the routing decision between nodes."""

    def __init__(self, *, width: int = 160) -> None:
        self._width = width
        self._previous: str | None = None

    def __call__(self, node: str, update: Mapping[str, Any]) -> None:
        name = node.removeprefix("__error_handler__")
        if node == "__interrupt__":
            print(f"  route: {self._previous} -> PAUSED for human review")
            return
        if self._previous is not None:
            print(f"  route: {self._previous} -> {name}")
        self._previous = name
        attempt = update.get("generation_attempts")
        print(f"[{name}]" + (f" attempt={attempt}" if attempt is not None else ""))
        for line in self._describe(update):
            print(f"    {line}"[: self._width])

    def _describe(self, update: Mapping[str, Any]) -> list[str]:
        lines: list[str] = []
        brief = update.get("research_brief")
        if isinstance(brief, ResearchBrief):
            lines.append(
                f"brief: {brief.topic} "
                f"(confidence={brief.confidence}, source={brief.source.provider})"
            )
            lines += [f"limitation: {x}" for x in brief.limitations]
        for f in update.get("research", []):
            if isinstance(f, ResearchFinding):
                lines.append(
                    f"finding: {f.theme} signal={f.signal_strength:.2f} "
                    f"evidence={f.evidence_post_ids}"
                )
        for c in update.get("candidates", []):
            if isinstance(c, ContentCandidate):
                revises = f" revises={c.revises_candidate_id}" if c.revises_candidate_id else ""
                lines.append(
                    f"candidate {c.id} [{c.hook_type}, {len(c.content)} chars]{revises}: "
                    f"{c.content}"
                )
        for k in update.get("critiques", []):
            if isinstance(k, Critique):
                rules = [v.rule for v in k.policy_violations]
                issues = [i.category.value for i in k.issues]
                lines.append(
                    f"critique {k.candidate_id}: {k.verdict} (model said {k.recommended_verdict}, "
                    f"score={k.assessment.score:.2f}, factual_risk={k.assessment.factual_risk}) "
                    f"issues={issues} violations={rules}"
                )
        review = update.get("review")
        if isinstance(review, ReviewState):
            lines.append(f"review: {review.status} candidates={review.candidate_ids}")
        for e in update.get("errors", []):
            if isinstance(e, RunError):
                lines.append(f"error [{e.error_type}] {e.message}")
        for call in update.get("llm_calls", []):
            if isinstance(call, LLMCall):
                tokens = f", tokens={call.usage.total_tokens}" if call.usage else ""
                lines.append(
                    f"llm: {call.provider}/{call.model} {call.outcome} "
                    f"{call.latency_ms:.0f}ms{tokens}"
                )
        status = update.get("status")
        if isinstance(status, RunStatus):
            lines.append(f"status: {status}")
        return lines
