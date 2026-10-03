"""Learning-loop objects: insights from analytics and planned experiments."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field

from social_growth_agent.models.base import DomainModel, new_id, utc_now


class PerformanceInsight(DomainModel):
    """E.g. 'numerical hooks get 1.8x the median likes' with the posts that support it."""

    id: str = Field(default_factory=lambda: new_id("ins"))
    account_id: str
    dimension: str = Field(description="What varied: hook_type, topic, format, length, window.")
    observation: str
    direction: Literal["positive", "negative", "neutral"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_post_ids: list[str] = Field(min_length=1)
    derived_at: datetime = Field(default_factory=utc_now)


class ExperimentStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    CONCLUDED = "concluded"


class Experiment(DomainModel):
    id: str = Field(default_factory=lambda: new_id("exp"))
    account_id: str
    hypothesis: str
    variable: str
    variants: list[str] = Field(min_length=2)
    status: ExperimentStatus = ExperimentStatus.PLANNED
    candidate_ids: list[str] = Field(default_factory=list)
