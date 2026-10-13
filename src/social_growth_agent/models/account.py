"""The account being grown and the strategy that guides its content."""

from typing import Literal, Self

from pydantic import Field, model_validator

from social_growth_agent.models.base import DomainModel, new_id
from social_growth_agent.models.content import HookType


class Account(DomainModel):
    id: str = Field(default_factory=lambda: new_id("acct"))
    handle: str
    niche: str
    platform: Literal["x"] = "x"


class LengthGuidance(DomainModel):
    """A soft length preference for generated posts, in characters (code points).

    Guidance for the Content Agent only: ``ContentPolicy.max_post_length`` stays the one
    enforced limit, and a human edit is never checked against this range.
    """

    min_chars: int = Field(ge=1, le=4000)
    max_chars: int = Field(ge=1, le=4000)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min_chars > self.max_chars:
            raise ValueError("length_guidance.min_chars must not exceed max_chars")
        return self


class ContentStrategy(DomainModel):
    """A versioned strategy. Phase 7 stores versions in ``strategy_versions``; a new
    version is created only by a human approving a proposal (or registering one).

    Candidates record ``strategy_version`` so performance can later be attributed
    to the strategy that produced them.
    """

    id: str = Field(default_factory=lambda: new_id("strat"))
    account_id: str
    version: int = Field(default=1, ge=1)
    pillars: list[str] = Field(min_length=1, description="Core topics the account posts about.")
    tone: str
    target_audience: str
    preferred_hooks: list[HookType] = Field(default_factory=list)
    avoid_topics: list[str] = Field(default_factory=list)
    length_guidance: LengthGuidance | None = Field(
        default=None,
        description="Soft length preference (Phase 7). Never overrides the content policy.",
    )
