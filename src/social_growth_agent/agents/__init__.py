"""Agents. Each wraps one LLM task with a typed input and a validated, typed output."""

from social_growth_agent.agents.content import (
    CandidateBatch,
    CandidateDraft,
    ContentAgent,
    ContentResult,
    GenerationContext,
)
from social_growth_agent.agents.critic import (
    CandidateEvaluation,
    CriticAgent,
    CriticReport,
    CriticResult,
    IssueDraft,
)
from social_growth_agent.agents.research import (
    FindingDraft,
    OpportunityDraft,
    ResearchAgent,
    ResearchReport,
    ResearchResult,
)

LLM_OUTPUT_SCHEMAS = (ResearchReport, CandidateBatch, CriticReport)

__all__ = [
    "LLM_OUTPUT_SCHEMAS",
    "CandidateBatch",
    "CandidateDraft",
    "CandidateEvaluation",
    "ContentAgent",
    "ContentResult",
    "CriticAgent",
    "CriticReport",
    "CriticResult",
    "FindingDraft",
    "GenerationContext",
    "IssueDraft",
    "OpportunityDraft",
    "ResearchAgent",
    "ResearchReport",
    "ResearchResult",
]
