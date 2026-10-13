"""Request bodies. Responses reuse the persistence read models."""

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from social_growth_agent.graph import RunConfig
from social_growth_agent.models import Account, ContentStrategy, ReviewAction, ReviewDecision


class CreateRunRequest(BaseModel):
    """Either an inline ``strategy`` (as in Phases 1-6) or ``use_active_strategy: true``
    (Phase 7: the account's activated version), never both."""

    model_config = ConfigDict(extra="forbid")

    account: Account
    strategy: ContentStrategy | None = None
    use_active_strategy: bool = False
    config: RunConfig = Field(default_factory=RunConfig)

    @model_validator(mode="after")
    def _strategy_belongs_to_account(self) -> Self:
        if self.use_active_strategy == (self.strategy is not None):
            raise ValueError("give either strategy or use_active_strategy: true")
        if self.strategy is not None and self.strategy.account_id != self.account.id:
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


class PublishRequestBody(BaseModel):
    """``scheduled_for`` must carry a timezone; absent (or slightly past) means now."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1, max_length=64)
    scheduled_for: datetime | None = None
    requested_by: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _schedule_has_a_timezone(self) -> Self:
        when = self.scheduled_for
        if when is not None and (when.tzinfo is None or when.utcoffset() is None):
            raise ValueError("scheduled_for must include a timezone")
        return self


class ResolvePublicationBody(BaseModel):
    """How a human closes an ambiguous (``unknown``) publication."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["published", "not_published"]
    provider_post_id: str | None = Field(default=None, max_length=64)
    reviewer: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _published_needs_the_post_id(self) -> Self:
        if self.outcome == "published" and not self.provider_post_id:
            raise ValueError("'published' requires provider_post_id")
        if self.outcome == "not_published" and self.provider_post_id:
            raise ValueError("provider_post_id is only allowed with 'published'")
        return self


class ActorBody(BaseModel):
    """Who asked for a retry or a cancellation."""

    model_config = ConfigDict(extra="forbid")

    reviewer: str | None = Field(default=None, max_length=128)


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str


# --- strategy evaluation (Phase 7) ------------------------------------------------------


class RequestProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_by: str | None = Field(default=None, max_length=128)


class ApproveProposalBody(BaseModel):
    """``proposal_sha256`` and ``evaluation_digest`` are the values the reviewer saw; the
    approval is refused if either no longer matches the stored proposal."""

    model_config = ConfigDict(extra="forbid")

    reviewer: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=1000)
    proposal_sha256: str = Field(min_length=64, max_length=64)
    evaluation_digest: str = Field(min_length=64, max_length=64)


class RejectProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=1000)


class RegisterVersionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: ContentStrategy
    reviewer: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=1000)


class ActivateBody(BaseModel):
    """``expected_active_version_id`` is required (null when no version is active yet):
    activation is compare-and-set against what the reviewer saw."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    expected_active_version_id: str | None = Field(max_length=64)
    reviewer: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=1000)
