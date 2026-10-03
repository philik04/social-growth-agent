"""The account being grown and the strategy that guides its content."""

from typing import Literal

from pydantic import Field

from social_growth_agent.models.base import DomainModel, new_id
from social_growth_agent.models.content import HookType


class Account(DomainModel):
    id: str = Field(default_factory=lambda: new_id("acct"))
    handle: str
    niche: str
    platform: Literal["x"] = "x"


class ContentStrategy(DomainModel):
    """A versioned strategy. The Strategy Agent will produce new versions from analytics.

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
