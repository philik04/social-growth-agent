"""Request bodies. Responses reuse the persistence read models."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from social_growth_agent.graph import RunConfig
from social_growth_agent.models import Account, ContentStrategy, ReviewAction, ReviewDecision


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: Account
    strategy: ContentStrategy
    config: RunConfig = Field(default_factory=RunConfig)

    @model_validator(mode="after")
    def _strategy_belongs_to_account(self) -> Self:
        if self.strategy.account_id != self.account.id:
            raise ValueError("strategy.account_id must match account.id")
        return self


class ReviewRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ReviewAction
    candidate_id: str | None = None
    edited_content: str | None = Field(default=None, max_length=4000)
    note: str | None = Field(default=None, max_length=1000)
    reviewer: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _valid_decision(self) -> Self:
        try:
            self.to_decision()
        except ValidationError as exc:
            message = str(exc.errors()[0]["msg"]).removeprefix("Value error, ")
            raise ValueError(message) from None
        return self

    def to_decision(self) -> ReviewDecision:
        return ReviewDecision(**self.model_dump())


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str
