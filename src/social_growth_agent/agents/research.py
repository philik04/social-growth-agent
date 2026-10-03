"""Research Agent: turns application-supplied posts into structured findings.

The agent never browses. It sees only what the ``SocialResearchProvider`` returned,
and the source of that material is recorded by application code, not by the model.
"""

import json
from dataclasses import dataclass

from pydantic import BaseModel, Field

from social_growth_agent.agents.base import call_llm, load_prompt, validating_output
from social_growth_agent.errors import AgentOutputError, InsufficientSignalError
from social_growth_agent.models import (
    Account,
    Confidence,
    ContentOpportunity,
    ContentStrategy,
    LLMCall,
    ResearchBrief,
    ResearchFinding,
    ResearchQuery,
    ResearchSource,
    SourcePost,
)
from social_growth_agent.providers import (
    LLMProvider,
    LLMRequest,
    LLMSettings,
    SocialResearchProvider,
)

SYNTHETIC_LIMITATION = (
    "Research material is synthetic sample data supplied by the application, not live X data."
)


class FindingDraft(BaseModel):
    theme: str
    summary: str
    evidence_post_ids: list[str] = Field(description="Ids of supplied posts only.")
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

    def __init__(
        self,
        llm: LLMProvider,
        research_provider: SocialResearchProvider,
        settings: LLMSettings | None = None,
    ) -> None:
        self._llm = llm
        self._research = research_provider
        self._settings = settings or LLMSettings()

    def run(self, account: Account, strategy: ContentStrategy) -> ResearchResult:
        query = ResearchQuery(account_id=account.id, niche=account.niche, topics=strategy.pillars)
        posts = self._research.search(query)
        if not posts:
            raise InsufficientSignalError(f"no research material found for {strategy.pillars}")

        source = ResearchSource(
            provider=self._research.source_name,
            post_ids=[p.id for p in posts],
            synthetic=self._research.synthetic,
        )
        result = call_llm(
            self._llm, self._request(account, strategy, posts, source), ResearchReport
        )
        with validating_output(result.call):
            findings, brief = _to_domain(result.output, source, known_ids=set(source.post_ids))
        return ResearchResult(findings=findings, brief=brief, call=result.call)

    def _request(
        self,
        account: Account,
        strategy: ContentStrategy,
        posts: list[SourcePost],
        source: ResearchSource,
    ) -> LLMRequest:
        post_payload = [p.model_dump(mode="json") for p in posts]
        origin = "SYNTHETIC sample data" if source.synthetic else "live platform data"
        prompt = (
            f"Account niche: {account.niche}\n"
            f"Content pillars: {', '.join(strategy.pillars)}\n"
            f"Research material: {len(posts)} posts from '{source.provider}' ({origin}), "
            "supplied by the application.\n\n"
            f"Posts:\n{json.dumps(post_payload, indent=1)}"
        )
        return LLMRequest(
            agent=self.name,
            task="research.synthesize",
            system=load_prompt("research"),
            prompt=prompt,
            payload={"posts": post_payload, "synthetic": source.synthetic},
            settings=self._settings,
        )


def _to_domain(
    report: ResearchReport, source: ResearchSource, known_ids: set[str]
) -> tuple[list[ResearchFinding], ResearchBrief]:
    if not report.findings:
        raise InsufficientSignalError("research produced no findings")
    findings = []
    for draft in report.findings:
        unknown = set(draft.evidence_post_ids) - known_ids
        if unknown:
            raise AgentOutputError(f"finding cites posts that were not supplied: {sorted(unknown)}")
        findings.append(ResearchFinding(**draft.model_dump()))

    opportunities = [_opportunity(o, findings) for o in report.content_opportunities]
    limitations = list(report.limitations)
    if source.synthetic and SYNTHETIC_LIMITATION not in limitations:
        limitations.append(SYNTHETIC_LIMITATION)
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
