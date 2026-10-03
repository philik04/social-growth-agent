"""Deterministic, rule-based stand-ins for LLM behaviour.

Used by the deterministic demo and by tests (``LLM_PROVIDER=fake``). They read the
structured request payload, never randomness, so identical inputs always produce
identical outputs. The content handler deliberately writes an unsupported claim on
the first attempt, so a demo run exercises the critic -> regenerate loop, and on
retries it revises the critiqued candidates (``revises_candidate_id``).
"""

from collections import defaultdict
from typing import Any

from social_growth_agent.agents.content import CandidateBatch, CandidateDraft
from social_growth_agent.agents.critic import CandidateEvaluation, CriticReport, IssueDraft
from social_growth_agent.agents.research import FindingDraft, OpportunityDraft, ResearchReport
from social_growth_agent.models import (
    Confidence,
    CritiqueVerdict,
    HookType,
    IssueCategory,
    PostFormat,
    RiskLevel,
    ToneMatch,
)
from social_growth_agent.providers.llm import LLMRequest
from social_growth_agent.providers.mocks.llm import ScriptedLLMProvider, payload_list

UNSUPPORTED_CLAIM_MARKERS = ("guaranteed", "100%", "always works")
_HOOKS = tuple(HookType)


def fake_research(request: LLMRequest) -> ResearchReport:
    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for post in payload_list(request, "posts"):
        by_topic[str(post["topic"])].append(post)
    findings = []
    for topic in sorted(by_topic):
        posts = by_topic[topic]
        engagement = sum(int(p["likes"]) + 2 * int(p["reposts"]) + int(p["replies"]) for p in posts)
        findings.append(
            FindingDraft(
                theme=topic,
                summary=f"{len(posts)} high-engagement posts about {topic}.",
                evidence_post_ids=[str(p["id"]) for p in posts],
                signal_strength=min(1.0, engagement / 5_000),
            )
        )
    opportunities = [
        OpportunityDraft(
            angle=f"Practical lessons on {f.theme}",
            rationale="Highest-engagement theme in the supplied posts.",
            finding_indexes=[i],
        )
        for i, f in enumerate(findings)
    ]
    return ResearchReport(
        topic=", ".join(sorted(by_topic)) or "none",
        summary=f"{len(findings)} themes found in the supplied posts.",
        findings=findings,
        content_opportunities=opportunities,
        confidence=Confidence.MEDIUM if len(findings) > 1 else Confidence.LOW,
        limitations=["Small sample of supplied posts."],
    )


def fake_content(request: LLMRequest) -> CandidateBatch:
    findings = payload_list(request, "findings")
    previous = payload_list(request, "previous_critique")
    attempt = int(str(request.payload.get("attempt", 1)))
    count = int(str(request.payload.get("count", 1)))
    audience = str(_dict(request, "strategy").get("target_audience", "builders"))
    drafts = []
    for i in range(count):
        finding = findings[i % len(findings)]
        theme = str(finding["theme"])
        claim = (
            "This is guaranteed to double your reach." if attempt == 1 else "Here's what worked."
        )
        revises = str(previous[i]["candidate_id"]) if i < len(previous) else None
        drafts.append(
            CandidateDraft(
                content=f"[{attempt}.{i + 1}] What we learned about {theme}. {claim}",
                topic=theme,
                hook_type=_HOOKS[(attempt + i) % len(_HOOKS)],
                format=PostFormat.SINGLE,
                target_audience=audience,
                research_finding_ids=[str(finding["id"])],
                rationale=f"Builds on the strongest theme: {theme}.",
                revises_candidate_id=revises,
            )
        )
    return CandidateBatch(candidates=drafts)


def fake_critic(request: LLMRequest) -> CriticReport:
    avoid = {str(t).lower() for t in _dict(request, "strategy").get("avoid_topics", [])}
    evaluations = []
    for cand in payload_list(request, "candidates"):
        text = str(cand["content"]).lower()
        if str(cand["topic"]).lower() in avoid:
            evaluations.append(
                _evaluation(
                    cand, CritiqueVerdict.REJECT, IssueCategory.OFF_STRATEGY, "avoided topic"
                )
            )
        elif any(marker in text for marker in UNSUPPORTED_CLAIM_MARKERS):
            evaluations.append(
                _evaluation(
                    cand, CritiqueVerdict.REVISE, IssueCategory.UNSUPPORTED_CLAIM, "absolute claim"
                )
            )
        else:
            evaluations.append(_evaluation(cand, CritiqueVerdict.PASS, None, None))
    return CriticReport(evaluations=evaluations)


def build_fake_llm() -> ScriptedLLMProvider:
    return ScriptedLLMProvider(
        {ResearchReport: fake_research, CandidateBatch: fake_content, CriticReport: fake_critic}
    )


def _dict(request: LLMRequest, key: str) -> dict[str, Any]:
    value = request.payload.get(key, {})
    return value if isinstance(value, dict) else {}


def _evaluation(
    cand: dict[str, Any],
    verdict: CritiqueVerdict,
    category: IssueCategory | None,
    detail: str | None,
) -> CandidateEvaluation:
    passed = verdict is CritiqueVerdict.PASS
    return CandidateEvaluation(
        candidate_id=str(cand["id"]),
        recommendation=verdict,
        score=0.8 if passed else 0.4,
        tone_match=ToneMatch.STRONG,
        factual_risk=RiskLevel.LOW if passed else RiskLevel.HIGH,
        originality_risk=RiskLevel.LOW,
        issues=[] if category is None else [IssueDraft(category=category, detail=str(detail))],
        suggested_revision=None if passed else f"Remove the {detail}.",
    )
