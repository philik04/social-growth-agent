"""Generated post candidates."""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from social_growth_agent.models.base import DomainModel, new_id, utc_now


class HookType(StrEnum):
    QUESTION = "question"
    NUMBER = "number"
    CONTRARIAN = "contrarian"
    STORY = "story"
    HOW_TO = "how_to"


class PostFormat(StrEnum):
    SINGLE = "single"
    THREAD = "thread"


class ContentIntent(StrEnum):
    """Editorial intent of a post (Phase 9). Independent of ``HookType``: the hook is how a
    post opens (and what experiments vary); the intent is what the post is for. A
    ``grok_question`` can open with a question or a contrarian hook, for example."""

    ORIGINAL_INSIGHT = "original_insight"
    TECHNICAL_EXPLANATION = "technical_explanation"
    NEWS_COMMENTARY = "news_commentary"
    RESEARCH_SUMMARY = "research_summary"
    OPEN_QUESTION = "open_question"
    GROK_QUESTION = "grok_question"


class CandidateOrigin(StrEnum):
    GENERATED = "generated"
    HUMAN_EDIT = "human_edit"


class ContentCandidate(DomainModel):
    """A post draft plus the lineage needed to trace it back to its inputs.

    Candidates are immutable: a revision (by the model on a retry, or by a human
    edit) is a new candidate that points at the one it revises.
    """

    id: str = Field(default_factory=lambda: new_id("cand"))
    run_id: str
    generation_attempt: int = Field(ge=1)
    strategy_id: str
    strategy_version: int = Field(ge=1)
    research_finding_ids: list[str]
    topic: str
    hook_type: HookType
    format: PostFormat
    target_audience: str
    content: str = Field(min_length=1)
    rationale: str = ""
    origin: CandidateOrigin = CandidateOrigin.GENERATED
    revises_candidate_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    content_intent: ContentIntent | None = Field(
        default=None,
        description="Set by the application from the run's policy (Phase 9 discovery runs); "
        "None for every other run.",
    )
