"""Research Agent: interprets application-supplied posts into structured findings.

The agent never browses and never fetches. Retrieval happens before it, in the graph's
``retrieve`` node; the agent sees only the ``SourcePost``s it is given. Provenance is
enforced in code: every evidence id a finding cites must be one of the supplied
``source_id``s, or the output is rejected.
"""

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from social_growth_agent.agents.base import call_llm, load_prompt, validating_output
from social_growth_agent.errors import AgentOutputError, InsufficientSignalError
from social_growth_agent.models import (
    Account,
    ClaimType,
    Confidence,
    ContentOpportunity,
    ContentStrategy,
    LLMCall,
    ResearchBrief,
    ResearchFinding,
    ResearchSource,
    SourcePost,
)
from social_growth_agent.policies import SYNTHETIC_LIMITATION, deterministic_limitations
from social_growth_agent.providers import LLMProvider, LLMRequest, LLMSettings

__all__ = [
    "SYNTHETIC_LIMITATION",
    "FindingDraft",
    "OpportunityDraft",
    "ResearchAgent",
    "ResearchReport",
    "ResearchResult",
]


class FindingDraft(BaseModel):
    theme: str
    summary: str
    claim_type: ClaimType = Field(
        description="observation: directly visible in the posts; hypothesis: a tentative "
        "explanation. Causal claims are not allowed."
    )
    evidence_source_ids: list[str] = Field(description="source_id values of supplied posts only.")
    signal_strength: float = Field(description="0 to 1.")


class OpportunityDraft(BaseModel):
    angle: str
    rationale: str
    finding_indexes: list[int] = Field(description="0-based indexes into `findings`.")


class ResearchReport(BaseModel):
    """The LLM output contract. Ids and source are assigned by the application."""

    topic: str
    summary: str
    findings: list[FindingDraft]
    content_opportunities: list[OpportunityDraft]
    confidence: Confidence
    limitations: list[str]


@dataclass(frozen=True)
class ResearchResult:
    findings: list[ResearchFinding]
    brief: ResearchBrief
    call: LLMCall


class ResearchAgent:
    name = "research"

    def __init__(self, llm: LLMProvider, settings: LLMSettings | None = None) -> None:
        self._llm = llm
        self._settings = settings or LLMSettings()

    def run(
        self,
        account: Account,
        strategy: ContentStrategy,
        posts: list[SourcePost],
        source: ResearchSource,
    ) -> ResearchResult:
        if not posts:
            raise InsufficientSignalError(f"no research material for query {source.query!r}")
        result = call_llm(
            self._llm, self._request(account, strategy, posts, source), ResearchReport
        )
        with validating_output(result.call):
            findings, brief = _to_domain(result.output, posts, source)
        return ResearchResult(findings=findings, brief=brief, call=result.call)

    def _request(
        self,
        account: Account,
        strategy: ContentStrategy,
        posts: list[SourcePost],
        source: ResearchSource,
    ) -> LLMRequest:
        post_payload = [_post_payload(p) for p in posts]
        origin = "SYNTHETIC sample data" if source.synthetic else "live platform data"
        with_impressions = sum(1 for p in posts if p.impressions is not None)
        prompt = (
            f"Account niche: {account.niche}\n"
            f"Content pillars: {', '.join(strategy.pillars)}\n"
            f"Research material: {len(posts)} posts from '{source.provider}' ({origin}), "
            "supplied by the application.\n"
            f"Retrieved with query: {source.query}\n"
            f"Impressions available for {with_impressions} of {len(posts)} posts.\n\n"
            f"Posts:\n{json.dumps(post_payload, indent=1)}"
        )
        return LLMRequest(
            agent=self.name,
            task="research.synthesize",
            system=load_prompt("research"),
            prompt=prompt,
            payload={
                "posts": post_payload,
                "synthetic": source.synthetic,
                "query": source.query,
                "pillars": list(strategy.pillars),
            },
            settings=self._settings,
        )


def _post_payload(post: SourcePost) -> dict[str, Any]:
    """What the model sees of a post: content, author, time, present metrics, gaps."""
    data = post.model_dump(mode="json", exclude={"platform", "retrieved_at", "author_id"})
    data["missing_metrics"] = post.missing_metrics()
    return data


def _to_domain(
    report: ResearchReport, posts: list[SourcePost], source: ResearchSource
) -> tuple[list[ResearchFinding], ResearchBrief]:
    if not report.findings:
        raise InsufficientSignalError("research produced no findings")
    known_ids = {p.source_id for p in posts}
    findings = []
    for draft in report.findings:
        unknown = set(draft.evidence_source_ids) - known_ids
        if unknown:
            raise AgentOutputError(
                f"finding cites source ids that were not supplied: {sorted(unknown)}"
            )
        findings.append(ResearchFinding(**draft.model_dump()))

    opportunities = [_opportunity(o, findings) for o in report.content_opportunities]
    limitations = list(report.limitations)
    for limitation in deterministic_limitations(posts, synthetic=source.synthetic):
        if limitation not in limitations:
            limitations.append(limitation)
    brief = ResearchBrief(
        topic=report.topic,
        summary=report.summary,
        opportunities=opportunities,
        confidence=report.confidence,
        limitations=limitations,
        source=source,
    )
    return findings, brief


def _opportunity(draft: OpportunityDraft, findings: list[ResearchFinding]) -> ContentOpportunity:
    if any(i < 0 or i >= len(findings) for i in draft.finding_indexes):
        raise AgentOutputError(
            f"opportunity references unknown finding index {draft.finding_indexes}"
        )
    return ContentOpportunity(
        angle=draft.angle,
        rationale=draft.rationale,
        finding_ids=[findings[i].id for i in draft.finding_indexes],
    )
