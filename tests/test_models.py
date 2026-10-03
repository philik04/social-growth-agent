import pytest
from pydantic import ValidationError

from social_growth_agent.models import ReviewAction, ReviewDecision


def test_approve_requires_candidate():
    with pytest.raises(ValidationError):
        ReviewDecision(action=ReviewAction.APPROVE, reviewer="p")


def test_edit_requires_content():
    with pytest.raises(ValidationError):
        ReviewDecision(action=ReviewAction.EDIT, candidate_id="c", reviewer="p")


def test_edited_content_only_with_edit():
    with pytest.raises(ValidationError):
        ReviewDecision(action=ReviewAction.REJECT, edited_content="x", reviewer="p")


def test_domain_models_are_immutable():
    decision = ReviewDecision(action=ReviewAction.REJECT, reviewer="p")
    with pytest.raises(ValidationError):
        decision.reviewer = "someone else"
