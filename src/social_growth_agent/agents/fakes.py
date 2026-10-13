"""Deterministic, rule-based stand-ins for LLM behaviour.

Used by the deterministic demo and by tests (``LLM_PROVIDER=fake``). They read the
structured request payload, never randomness, so identical inputs always produce
identical outputs. The content handler deliberately writes an unsupported claim on
the first attempt, so a demo run exercises the critic -> regenerate loop, and on
retries it revises the critiqued candidates (``revises_candidate_id``). When reviewer
notes are supplied it echoes the latest one, so tests can see they reached the prompt.
"""

from collections import defaultdict
from decimal import Decimal
from typing import Any

from social_growth_agent.agents.content import CandidateBatch, CandidateDraft
from social_growth_agent.agents.critic import CandidateEvaluation, CriticReport, IssueDraft
from social_growth_agent.agents.research import FindingDraft, OpportunityDraft, ResearchReport
from social_growth_agent.agents.strategy import ChangeDraft, StrategyProposalDraft
from social_growth_agent.models import (
    ClaimKind,
    ClaimType,
    Confidence,
    ContentIntent,
    CritiqueVerdict,
    HookType,
    IssueCategory,
    PostFormat,
    RiskLevel,
    ToneMatch,
)
from social_growth_agent.models.strategy import (
    ExpectedDirection,
    Metric,
    ProposableField,
    ProposalConfidence,
    ProposalOutcome,
)
from social_growth_agent.providers.llm import LLMRequest
from social_growth_agent.providers.mocks.llm import ScriptedLLMProvider, payload_list
from social_growth_agent.providers.mocks.social import mentions

UNSUPPORTED_CLAIM_MARKERS = ("guaranteed", "100%", "always works")
_HOOKS = tuple(HookType)


def fake_research(request: LLMRequest) -> ResearchReport:
    """Groups posts by the first content pillar they mention (else by the query) and
    cites exactly the supplied ``source_id``s, so it never fabricates provenance."""
    raw_pillars = request.payload.get("pillars", [])
    pillars = [str(p) for p in raw_pillars] if isinstance(raw_pillars, list) else []
    fallback = str(request.payload.get("query", "supplied posts"))
    posts = payload_list(request, "posts")
    if any("source_category" in p for p in posts):
        return _fake_discovery_research(posts, pillars, fallback)
    by_theme: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for post in payload_list(request, "posts"):
        theme = next((p for p in pillars if mentions(str(post["text"]), p)), fallback)
        by_theme[theme].append(post)
    findings = []
    for theme in sorted(by_theme):
        posts = by_theme[theme]
        engagement = sum(
            _metric(p, "likes") + 2 * _metric(p, "reposts") + _metric(p, "replies") for p in posts
        )
        findings.append(
            FindingDraft(
                theme=theme,
                summary=f"{len(posts)} posts about {theme} showed engagement in the supplied "
                "sample.",
                claim_type=ClaimType.OBSERVATION,
                evidence_source_ids=[str(p["source_id"]) for p in posts],
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
        topic=", ".join(sorted(by_theme)) or "none",
        summary=f"{len(findings)} themes found in the supplied posts.",
        findings=findings,
        content_opportunities=opportunities,
        confidence=Confidence.MEDIUM if len(findings) > 1 else Confidence.LOW,
        limitations=["Small sample of supplied posts."],
    )


def _fake_discovery_research(
    posts: list[dict[str, Any]], pillars: list[str], fallback: str
) -> ResearchReport:
    """Discovery evidence (Phase 9): labels claim kinds the way the prompt asks. Primary
    items become reported facts (their title is the claim, so anchors match), questions
    in discussions become open questions, other discussion becomes opinion."""
    groups: dict[ClaimKind, list[dict[str, Any]]] = defaultdict(list)
    for post in posts:
        body = f"{post.get('title') or ''} {post['text']}"
        if post.get("source_category") == "primary":
            groups[ClaimKind.REPORTED_FACT].append(post)
        elif "?" in body:
            groups[ClaimKind.OPEN_QUESTION].append(post)
        else:
            groups[ClaimKind.OPINION].append(post)
    findings = []
    for kind in (ClaimKind.REPORTED_FACT, ClaimKind.OPEN_QUESTION, ClaimKind.OPINION):
        cited = groups.get(kind, [])
        if not cited:
            continue
        first = cited[0]
        body = f"{first.get('title') or ''} {first['text']}"
        theme = next((p for p in pillars if mentions(body, p)), fallback)
        summary = {
            ClaimKind.REPORTED_FACT: f"{first.get('publisher') or 'A primary source'} "
            f"reported: {str(first.get('title') or first['text'])[:160]}",
            ClaimKind.OPEN_QUESTION: f"Practitioners are asking about {theme}; the evidence "
            "does not settle it.",
            ClaimKind.OPINION: f"Practitioners shared views on {theme}.",
        }[kind]
        findings.append(
            FindingDraft(
                theme=theme,
                summary=summary,
                claim_type=ClaimType.OBSERVATION,
                evidence_source_ids=[str(p["source_id"]) for p in cited],
                signal_strength=min(1.0, 0.3 + 0.2 * len(cited)),
                claim_kind=kind,
            )
        )
    return ResearchReport(
        topic=", ".join(dict.fromkeys(f.theme for f in findings)),
        summary=f"{len(findings)} kinds of claims in the supplied evidence.",
        findings=findings,
        content_opportunities=[
            OpportunityDraft(
                angle=f"Explain {f.theme}",
                rationale="Grounded in the evidence.",
                finding_indexes=[i],
            )
            for i, f in enumerate(findings)
        ],
        confidence=Confidence.MEDIUM if len(findings) > 1 else Confidence.LOW,
        limitations=["Small set of discovery evidence; claim kinds are model labels, checked."],
    )


def _metric(post: dict[str, Any], name: str) -> int:
    value = post.get(name)
    return int(value) if isinstance(value, int) else 0


def fake_content(request: LLMRequest) -> CandidateBatch:
    findings = payload_list(request, "findings")
    previous = payload_list(request, "previous_critique")
    attempt = int(str(request.payload.get("attempt", 1)))
    count = int(str(request.payload.get("count", 1)))
    audience = str(_dict(request, "strategy").get("target_audience", "builders"))
    raw_notes = request.payload.get("reviewer_notes", [])
    notes = [str(n) for n in raw_notes] if isinstance(raw_notes, list) else []
    policy = _dict(request, "policy")
    intent = policy.get("content_intent")
    if intent is not None:
        return _fake_intent_content(
            ContentIntent(str(intent)), findings, policy, attempt, count, audience, previous
        )
    drafts = []
    for i in range(count):
        finding = findings[i % len(findings)]
        theme = str(finding["theme"])
        claim = (
            "This is guaranteed to double your reach." if attempt == 1 else "Here's what worked."
        )
        if notes:
            claim = f"Here's what worked (reviewer: {notes[-1][:40]})."
        revises = str(previous[i]["candidate_id"]) if i < len(previous) else None
        content = f"[{attempt}.{i + 1}] What we learned about {theme}. {claim}"
        hook = _HOOKS[(attempt + i) % len(_HOOKS)]
        content, hook = _experiment_constraints(_dict(request, "policy"), content, hook, theme)
        drafts.append(
            CandidateDraft(
                content=content,
                topic=theme,
                hook_type=hook,
                format=PostFormat.SINGLE,
                target_audience=audience,
                research_finding_ids=[str(finding["id"])],
                rationale=f"Builds on the strongest theme: {theme}.",
                revises_candidate_id=revises,
            )
        )
    return CandidateBatch(candidates=drafts)


_INTENT_OPENERS = {
    ContentIntent.ORIGINAL_INSIGHT: "One pattern worth noticing in {theme}: ",
    ContentIntent.TECHNICAL_EXPLANATION: "How {theme} works, briefly: ",
    ContentIntent.NEWS_COMMENTARY: "News on {theme}. ",
    ContentIntent.RESEARCH_SUMMARY: "Research summary on {theme}: ",
    ContentIntent.OPEN_QUESTION: "An open question on {theme}: ",
}


def _fake_intent_content(
    intent: ContentIntent,
    findings: list[dict[str, Any]],
    policy: dict[str, Any],
    attempt: int,
    count: int,
    audience: str,
    previous: list[dict[str, Any]],
) -> CandidateBatch:
    """Discovery runs (Phase 9): text shaped by the content intent, using only the links
    the policy allows and no numbers of its own. A Grok question mentions @grok once and
    says why asking adds value; every other intent mentions nobody."""
    links = [str(u) for u in policy.get("allowed_links", [])]
    question_findings = [
        f for f in findings if f.get("claim_kind") in ("open_question", "disputed")
    ]
    drafts = []
    for i in range(count):
        pool = question_findings if intent is ContentIntent.GROK_QUESTION else findings
        finding = (pool or findings)[i % len(pool or findings)]
        theme = str(finding["theme"])
        if intent is ContentIntent.GROK_QUESTION:
            content = (
                f"@grok an open question on {theme}: what public evidence bears on it, "
                "and what is still unknown?"
            )
            rationale = (
                "The evidence leaves this open; asking Grok publicly invites a sourced "
                "summary of both sides that readers can check."
            )
        else:
            opener = _INTENT_OPENERS[intent].format(theme=theme)
            claim = "It is guaranteed to change everything." if attempt == 1 else ""
            content = f"{opener}{str(finding['summary'])[:150]} {claim}".strip()
            if links and intent in (ContentIntent.NEWS_COMMENTARY, ContentIntent.RESEARCH_SUMMARY):
                content = f"{content} Source: {links[0]}"
            rationale = f"Grounded in the {finding.get('claim_kind') or 'supplied'} finding."
        revises = str(previous[i]["candidate_id"]) if i < len(previous) else None
        drafts.append(
            CandidateDraft(
                content=content[: int(str(policy.get("max_post_length", 280)))],
                topic=theme,
                hook_type=HookType.QUESTION
                if intent in (ContentIntent.GROK_QUESTION, ContentIntent.OPEN_QUESTION)
                else HookType.HOW_TO,
                format=PostFormat.SINGLE,
                target_audience=audience,
                research_finding_ids=[str(finding["id"])],
                rationale=rationale,
                revises_candidate_id=revises,
            )
        )
    return CandidateBatch(candidates=drafts)


_HOOK_OPENERS = {
    HookType.QUESTION: "Ever wondered what works for {theme}? ",
    HookType.NUMBER: "3 things we learned about {theme}. ",
    HookType.HOW_TO: "How to get better at {theme}: ",
    HookType.STORY: "Last week we tried something new with {theme}. ",
    HookType.CONTRARIAN: "Unpopular opinion about {theme}: ",
}


def _experiment_constraints(
    policy: dict[str, Any], content: str, hook: HookType, theme: str
) -> tuple[str, HookType]:
    """Follow experiment treatments (Phase 8) when the policy sets them. Without those
    keys the output is exactly as before, so Phase 1-7 runs are unchanged."""
    required = policy.get("required_hook_type")
    if required is not None:
        hook = HookType(str(required))
        content = _HOOK_OPENERS[hook].format(theme=theme) + content
    minimum = policy.get("min_post_length")
    if minimum is not None or required is not None:
        maximum = int(str(policy.get("max_post_length", 280)))
        low = int(str(minimum)) if minimum is not None else 1
        filler = f" More notes on {theme}."
        while len(content) < low:
            content += filler
        content = content[:maximum].rstrip()
        while len(content) < low:
            content += "."
    return content, hook


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


def fake_strategy(request: LLMRequest) -> StrategyProposalDraft:
    """Proposes the hook type with the higher median engagement rate in the hook
    comparison with the largest such difference, as a low-confidence hypothesis. No
    hook comparison (or no rate data), or that hook already preferred: no change. Its
    text contains no numbers, so it never trips the invented-statistics check."""
    evidence = _dict(request, "evidence")
    current = _dict(request, "current_strategy").get("preferred_hooks", [])
    best: tuple[Decimal, str, str] | None = None
    for comparison in evidence.get("comparisons", []):
        ref = str(comparison.get("id", ""))
        if not ref.startswith("cmp:hook_type:"):
            continue
        value = comparison.get("rates", {}).get("engagement_rate", [None])[0]
        if value is None:
            continue
        delta = Decimal(str(value))
        leader = comparison["a"] if delta > 0 else comparison["b"]
        if delta != 0 and (best is None or abs(delta) > best[0]):
            best = (abs(delta), ref, str(leader))
    if best is None or current == [best[2]]:
        return StrategyProposalDraft(
            outcome=ProposalOutcome.NO_CHANGE,
            summary="The evaluation does not support a strategy change. No change recommended.",
            changes=[],
            posting_time_note=None,
            posting_time_evidence_refs=[],
        )
    _, ref, hook = best
    return StrategyProposalDraft(
        outcome=ProposalOutcome.CHANGE,
        summary=f"Prefer {hook} hooks, as a hypothesis to test in future posts.",
        changes=[
            ChangeDraft(
                field=ProposableField.PREFERRED_HOOKS,
                proposed_hooks=[HookType(hook)],
                proposed_length=None,
                evidence_refs=[ref],
                expected_metric=Metric.ENGAGEMENT_RATE,
                expected_direction=ExpectedDirection.INCREASE,
                confidence=ProposalConfidence.LOW,
                rationale=f"In the cited comparison the {hook} group had the higher median "
                "engagement rate.",
                limitations=["descriptive comparison of a small sample"],
                confounders=["time trend and topic differ between the groups"],
            )
        ],
        posting_time_note=None,
        posting_time_evidence_refs=[],
    )


def build_fake_llm() -> ScriptedLLMProvider:
    return ScriptedLLMProvider(
        {
            ResearchReport: fake_research,
            CandidateBatch: fake_content,
            CriticReport: fake_critic,
            StrategyProposalDraft: fake_strategy,
        }
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
