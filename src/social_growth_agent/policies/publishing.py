"""Deterministic publish policy: may this candidate of this run be published?

Decided from persisted facts only; no LLM is consulted. The API checks it before a
publication intent is created, and the publisher worker checks it again (defence in
depth) immediately before the platform call.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from social_growth_agent.models import (
    ContentCandidate,
    CritiqueVerdict,
    PublicationStatus,
    RunStatus,
)
from social_growth_agent.policies.content_policy import ContentPolicy, find_violations


@dataclass(frozen=True)
class PublishFacts:
    """Everything the policy needs, loaded from the domain tables."""

    run_status: RunStatus
    candidate: ContentCandidate
    approved_candidate_id: str | None
    """``candidate_id`` of the run's approve decision, if any."""
    approval_count: int
    """Approve decisions recorded for the run (exactly one is valid)."""
    review_candidate_ids: tuple[str, ...]
    """The candidates of the review request the approval answered, as recorded on the
    decision. Empty for decisions made before Phase 5 recorded the snapshot, which the
    policy then cannot check (the approve decision itself was validated against the
    pending review when it was made)."""
    latest_critique_verdict: CritiqueVerdict | None
    content_policy: ContentPolicy
    existing_publication_status: PublicationStatus | None = None


def publish_refusals(facts: PublishFacts) -> list[str]:
    """Every reason the candidate may not be published; empty means publishable."""
    reasons: list[str] = []
    cand = facts.candidate
    if facts.run_status is not RunStatus.APPROVED:
        reasons.append(f"run is {facts.run_status.value}, not approved")
    if facts.approval_count != 1 or facts.approved_candidate_id is None:
        reasons.append(f"run has {facts.approval_count} approve decisions; exactly one required")
    elif facts.approved_candidate_id != cand.id:
        reasons.append(f"candidate {cand.id} is not the approved candidate")
    if facts.review_candidate_ids and cand.id not in facts.review_candidate_ids:
        reasons.append(f"candidate {cand.id} was not part of the review request")
    if facts.latest_critique_verdict is not CritiqueVerdict.PASS:
        verdict = facts.latest_critique_verdict
        reasons.append(f"latest critique is {verdict.value if verdict else 'missing'}, not pass")
    violations = find_violations(cand, facts.content_policy)
    reasons += [f"content policy: {v.rule} ({v.detail})" for v in violations]
    existing = facts.existing_publication_status
    if existing is not None and existing is not PublicationStatus.CANCELLED:
        reasons.append(f"a publication already exists ({existing.value})")
    return reasons


def idempotency_key(platform: str, run_id: str, candidate_id: str) -> str:
    """Stable key of the one publication intent for (platform, run, candidate)."""
    return hashlib.sha256(f"{platform}:{run_id}:{candidate_id}".encode()).hexdigest()


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


@dataclass(frozen=True)
class ScheduleWindow:
    max_ahead: timedelta = timedelta(days=30)
    past_tolerance: timedelta = timedelta(minutes=5)


def schedule_refusal(
    scheduled_for: datetime | None, now: datetime, window: ScheduleWindow
) -> str | None:
    """Why ``scheduled_for`` is not acceptable, or None. Naive datetimes are refused."""
    if scheduled_for is None:
        return None
    if scheduled_for.tzinfo is None or scheduled_for.utcoffset() is None:
        return "scheduled_for must include a timezone"
    if scheduled_for < now - window.past_tolerance:
        return f"scheduled_for is more than {_describe(window.past_tolerance)} in the past"
    if scheduled_for > now + window.max_ahead:
        return f"scheduled_for is more than {_describe(window.max_ahead)} ahead"
    return None


def is_due(scheduled_for: datetime | None, now: datetime) -> bool:
    """Slightly past (within tolerance, already validated) or absent means publish now."""
    return scheduled_for is None or scheduled_for <= now


def _describe(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds % 86400 == 0:
        return f"{seconds // 86400} days"
    if seconds % 60 == 0:
        return f"{seconds // 60} minutes"
    return f"{seconds} seconds"
