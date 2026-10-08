"""Human-in-the-loop review types."""

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from social_growth_agent.models.base import DomainModel, new_id, utc_now
from social_growth_agent.models.content import ContentCandidate
from social_growth_agent.models.critique import Critique


class ReviewAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"
    REGENERATE = "regenerate"


class ReviewStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    DECIDED = "decided"


class ReviewRequest(DomainModel):
    """Payload surfaced to the human when the graph pauses.

    Every candidate listed here has a passing critique of its exact content.
    ``rejected_edit`` explains why the reviewer's last edit did not pass re-critique.
    """

    run_id: str
    candidates: list[ContentCandidate] = Field(min_length=1)
    critiques: list[Critique]
    rejected_edit: Critique | None = None
    edits_remaining: int = Field(ge=0)


class ReviewDecision(DomainModel):
    id: str = Field(default_factory=lambda: new_id("dec"))
    action: ReviewAction
    candidate_id: str | None = None
    edited_content: str | None = None
    reviewer: str
    note: str | None = Field(default=None, max_length=1000)
    decided_at: datetime = Field(default_factory=utc_now)
    resulting_candidate_id: str | None = Field(
        default=None, description="Set by the application for 'edit': the new candidate's id."
    )
    reviewed_candidate_ids: list[str] = Field(
        default_factory=list,
        description="Snapshot of the review request this decision answered, set by the "
        "application (Phase 5). Empty on decisions recorded before it existed.",
    )

    @model_validator(mode="after")
    def _check_action_fields(self) -> Self:
        needs_candidate = self.action in (ReviewAction.APPROVE, ReviewAction.EDIT)
        if needs_candidate and self.candidate_id is None:
            raise ValueError(f"'{self.action}' requires candidate_id")
        if self.action is ReviewAction.EDIT and not self.edited_content:
            raise ValueError("'edit' requires edited_content")
        if self.action is not ReviewAction.EDIT and self.edited_content is not None:
            raise ValueError("edited_content is only allowed with 'edit'")
        return self


class ReviewState(DomainModel):
    status: ReviewStatus = ReviewStatus.NOT_REQUESTED
    candidate_ids: list[str] = Field(default_factory=list)
    decision: ReviewDecision | None = None
    edit_rounds: int = Field(default=0, ge=0)
    pending_edit_candidate_id: str | None = None
    rejected_edit_critique_id: str | None = None
