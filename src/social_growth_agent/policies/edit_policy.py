"""Human edit invariant: materially edited content must be critiqued again.

An edit never modifies a candidate. It creates a new candidate (origin
``human_edit``) that must earn its own passing critique before it can be approved.
"""

from social_growth_agent.errors import InvalidReviewError
from social_growth_agent.models import CandidateOrigin, ContentCandidate, new_id, utc_now


def _normalise(text: str) -> str:
    return " ".join(text.split())


def is_material_edit(original: str, edited: str) -> bool:
    """Whitespace-only changes are not material; anything else is."""
    return _normalise(original) != _normalise(edited)


def edited_candidate(original: ContentCandidate, edited_content: str) -> ContentCandidate:
    if not is_material_edit(original.content, edited_content):
        raise InvalidReviewError("edit makes no material change; approve the candidate instead")
    return original.model_copy(
        update={
            "id": new_id("cand"),
            "content": edited_content,
            "origin": CandidateOrigin.HUMAN_EDIT,
            "revises_candidate_id": original.id,
            "rationale": "Edited by a human reviewer.",
            "created_at": utc_now(),
        }
    )
