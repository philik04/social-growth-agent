"""Queries and transitions on ``publications`` and ``publication_attempts``.

Every function takes an open session; callers own the transaction. There is no provider
knowledge and no policy here: the deterministic rules live in ``policies/publishing.py``
and the orchestration in ``services/publications.py`` and ``services/publisher.py``.

Two invariants are enforced by the database, not by this code:

- one intent per (run, candidate, platform), and one per ``idempotency_key``;
- a publication's candidate belongs to its run (composite foreign key).
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from social_growth_agent.errors import PublicationNotFoundError
from social_growth_agent.models import (
    ACTIVE_PUBLICATION_STATUSES,
    AttemptOutcome,
    ContentCandidate,
    CritiqueVerdict,
    PublicationStatus,
    PublishFailureCategory,
    ReviewAction,
    RunStatus,
    new_id,
    utc_now,
)
from social_growth_agent.persistence.tables import (
    ContentCandidateRow,
    CritiqueRow,
    PublicationAttemptRow,
    PublicationRow,
    ReviewDecisionRow,
    RunRow,
)
from social_growth_agent.persistence.views import PublicationAttemptView, PublicationView
from social_growth_agent.policies import PublishFacts
from social_growth_agent.policies.content_policy import ContentPolicy

CLAIMABLE = (PublicationStatus.READY, PublicationStatus.SCHEDULED)

_MAX_FAILURE_MESSAGE = 500


# --- facts for the policy -------------------------------------------------------------


def publish_facts(
    session: Session, run_row: RunRow, candidate_row: ContentCandidateRow, platform: str
) -> PublishFacts:
    """Load everything the deterministic publish policy decides on."""
    approvals = list(
        session.scalars(
            select(ReviewDecisionRow)
            .where(ReviewDecisionRow.run_id == run_row.id)
            .where(ReviewDecisionRow.action == ReviewAction.APPROVE.value)
            .order_by(ReviewDecisionRow.decided_at)
        )
    )
    latest = session.scalars(
        select(CritiqueRow)
        .where(CritiqueRow.candidate_id == candidate_row.id)
        .order_by(CritiqueRow.position.desc())
        .limit(1)
    ).first()
    existing = find_for_target(session, run_row.id, candidate_row.id, platform)
    return PublishFacts(
        run_status=RunStatus(run_row.status),
        candidate=_candidate(session, candidate_row),
        approved_candidate_id=approvals[-1].candidate_id if approvals else None,
        approval_count=len(approvals),
        review_candidate_ids=tuple(approvals[-1].reviewed_candidate_ids) if approvals else (),
        latest_critique_verdict=CritiqueVerdict(latest.verdict) if latest else None,
        content_policy=ContentPolicy.model_validate(run_row.config.get("content_policy", {})),
        existing_publication_status=(
            None if existing is None else PublicationStatus(existing.status)
        ),
    )


def _candidate(session: Session, row: ContentCandidateRow) -> ContentCandidate:
    finding_ids = list(
        session.scalars(
            text(
                "SELECT finding_id FROM candidate_findings "
                "WHERE candidate_id = :cid ORDER BY finding_id"
            ).bindparams(cid=row.id)
        )
    )
    return ContentCandidate(
        id=row.id,
        run_id=row.run_id,
        generation_attempt=row.generation_attempt,
        strategy_id=row.strategy_id,
        strategy_version=row.strategy_version,
        research_finding_ids=finding_ids,
        topic=row.topic,
        hook_type=row.hook_type,
        format=row.format,
        target_audience=row.target_audience,
        content=row.content,
        rationale=row.rationale or "",
        origin=row.origin,
        revises_candidate_id=row.revises_candidate_id,
        created_at=row.created_at,
    )


# --- writes ---------------------------------------------------------------------------


def insert_intent(
    session: Session,
    *,
    run_id: str,
    candidate_id: str,
    platform: str,
    idempotency_key: str,
    content: str,
    content_sha256: str,
    status: PublicationStatus,
    scheduled_for: datetime | None,
    requested_by: str | None,
) -> str | None:
    """Insert the one intent for this target. Returns None if it already exists.

    ``ON CONFLICT DO NOTHING`` makes a duplicate request (or a concurrent one) a no-op
    rather than a second intent; the caller then reports the existing publication.
    """
    publication_id = new_id("pub")
    result = session.execute(
        insert(PublicationRow)
        .values(
            id=publication_id,
            run_id=run_id,
            candidate_id=candidate_id,
            platform=platform,
            idempotency_key=idempotency_key,
            content=content,
            content_sha256=content_sha256,
            status=status.value,
            scheduled_for=scheduled_for,
            requested_by=requested_by,
            attempt_count=0,
        )
        .on_conflict_do_nothing()
        .returning(PublicationRow.id)
    )
    return result.scalars().first()


def requeue_cancelled(
    session: Session,
    publication_id: str,
    *,
    status: PublicationStatus,
    scheduled_for: datetime | None,
    requested_by: str | None,
) -> None:
    """Revive a cancelled intent: the target's unique key stays the same row, so a new
    request after a cancel can never create a second intent (or a second post)."""
    session.execute(
        update(PublicationRow)
        .where(PublicationRow.id == publication_id)
        .where(PublicationRow.status == PublicationStatus.CANCELLED.value)
        .values(
            status=status.value,
            scheduled_for=scheduled_for,
            requested_by=requested_by,
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
            failure_category=None,
            failure_message=None,
            rate_limit_reset_at=None,
            retry_not_before=None,
            resolved_by=None,
            resolution_note=None,
        )
    )


def claim_due(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: int,
    limit: int,
    now: datetime | None = None,
) -> list[str]:
    """Lease up to ``limit`` due publications for this worker.

    ``FOR UPDATE SKIP LOCKED`` means two workers never claim the same row: the second
    skips rows the first has locked. An unexpired lease held by another worker is also
    skipped, so a slow worker's row is not taken from under it. A rate-limited row
    waiting for its reset (``retry_not_before`` in the future) is simply not due yet.
    """
    now = now or utc_now()
    due = (
        select(PublicationRow.id)
        .where(PublicationRow.status.in_([s.value for s in CLAIMABLE]))
        .where(
            func.coalesce(PublicationRow.scheduled_for, PublicationRow.created_at) <= now,
        )
        .where(
            (PublicationRow.lease_expires_at.is_(None)) | (PublicationRow.lease_expires_at < now)
        )
        .where(
            (PublicationRow.retry_not_before.is_(None)) | (PublicationRow.retry_not_before <= now)
        )
        .order_by(func.coalesce(PublicationRow.scheduled_for, PublicationRow.created_at))
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    ids = list(session.scalars(due))
    if not ids:
        return []
    session.execute(
        update(PublicationRow)
        .where(PublicationRow.id.in_(ids))
        .values(
            claimed_by=worker_id,
            claimed_at=now,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )
    )
    return ids


def begin_attempt(
    session: Session,
    publication_id: str,
    *,
    worker_id: str,
    provider: str,
    now: datetime | None = None,
) -> PublicationAttemptRow | None:
    """Move a claimed publication to ``publishing`` and write its started attempt row.

    Conditional on the row still being claimable *and still claimed by this worker*, so
    a lost lease or a cancellation between claim and call cannot produce a platform
    call. Returns None when the transition did not apply (nothing was sent).
    """
    now = now or utc_now()
    result = session.execute(
        update(PublicationRow)
        .where(PublicationRow.id == publication_id)
        .where(PublicationRow.claimed_by == worker_id)
        .where(PublicationRow.status.in_([s.value for s in CLAIMABLE]))
        .values(
            status=PublicationStatus.PUBLISHING.value,
            attempt_count=PublicationRow.attempt_count + 1,
            provider=provider,
            started_at=func.coalesce(PublicationRow.started_at, now),
            failure_category=None,
            failure_message=None,
            retry_not_before=None,
        )
        .returning(PublicationRow.attempt_count)
    )
    attempt = result.scalars().first()
    if attempt is None:
        return None
    row = PublicationAttemptRow(
        id=new_id("att"),
        publication_id=publication_id,
        attempt=attempt,
        worker_id=worker_id,
        provider=provider,
        started_at=now,
        outcome=AttemptOutcome.STARTED.value,
    )
    session.add(row)
    return row


def finish_attempt(
    session: Session,
    attempt_id: str,
    *,
    outcome: AttemptOutcome,
    latency_ms: float,
    http_status: int | None = None,
    failure_category: PublishFailureCategory | None = None,
    failure_message: str | None = None,
    rate_limit_reset_at: datetime | None = None,
    provider_post_id: str | None = None,
) -> None:
    session.execute(
        update(PublicationAttemptRow)
        .where(PublicationAttemptRow.id == attempt_id)
        .values(
            finished_at=utc_now(),
            latency_ms=latency_ms,
            outcome=outcome.value,
            http_status=http_status,
            failure_category=failure_category.value if failure_category else None,
            failure_message=sanitized(failure_message),
            rate_limit_reset_at=rate_limit_reset_at,
            provider_post_id=provider_post_id,
        )
    )


def mark_published(
    session: Session,
    publication_id: str,
    *,
    provider_post_id: str,
    provider_post_url: str | None,
    resolved_by: str | None = None,
    resolution_note: str | None = None,
) -> None:
    session.execute(
        update(PublicationRow)
        .where(PublicationRow.id == publication_id)
        .values(
            status=PublicationStatus.PUBLISHED.value,
            provider_post_id=provider_post_id,
            provider_post_url=provider_post_url,
            published_at=utc_now(),
            claimed_by=None,
            lease_expires_at=None,
            failure_category=None,
            failure_message=None,
            retry_not_before=None,
            resolved_by=resolved_by,
            resolution_note=resolution_note,
        )
    )


def set_outcome(
    session: Session,
    publication_id: str,
    *,
    status: PublicationStatus,
    failure_category: PublishFailureCategory | None = None,
    failure_message: str | None = None,
    rate_limit_reset_at: datetime | None = None,
    retry_not_before: datetime | None = None,
    release_lease: bool = True,
) -> None:
    """Record a non-published outcome (``ready`` again, ``failed`` or ``unknown``).

    ``retry_not_before`` is only ever set for a rate-limited row requeued to ``ready``;
    every other outcome clears it.
    """
    values: dict[str, object] = {
        "status": status.value,
        "failure_category": failure_category.value if failure_category else None,
        "failure_message": sanitized(failure_message),
        "rate_limit_reset_at": rate_limit_reset_at,
        "retry_not_before": retry_not_before,
    }
    if release_lease:
        values |= {"claimed_by": None, "claimed_at": None, "lease_expires_at": None}
    session.execute(
        update(PublicationRow).where(PublicationRow.id == publication_id).values(**values)
    )


def sweep_expired_publishing(session: Session, now: datetime | None = None) -> list[str]:
    """``publishing`` rows whose lease expired become ``unknown``, never ``ready``.

    The worker that held the lease died at some point around its platform call, so
    whether a post exists is unknown. Only a human resolve (or a later reconcile) may
    move such a row on; nothing is retried automatically.
    """
    now = now or utc_now()
    result = session.execute(
        update(PublicationRow)
        .where(PublicationRow.status == PublicationStatus.PUBLISHING.value)
        .where(PublicationRow.lease_expires_at.is_not(None))
        .where(PublicationRow.lease_expires_at < now)
        .values(
            status=PublicationStatus.UNKNOWN.value,
            failure_category=PublishFailureCategory.LEASE_EXPIRED.value,
            failure_message="the publishing worker stopped while the attempt was in flight; "
            "whether the post exists is unknown",
            claimed_by=None,
            lease_expires_at=None,
        )
        .returning(PublicationRow.id)
    )
    ids = list(result.scalars())
    if ids:
        session.execute(
            update(PublicationAttemptRow)
            .where(PublicationAttemptRow.publication_id.in_(ids))
            .where(PublicationAttemptRow.outcome == AttemptOutcome.STARTED.value)
            .values(
                outcome=AttemptOutcome.UNKNOWN.value,
                failure_category=PublishFailureCategory.LEASE_EXPIRED.value,
            )
        )
    return ids


# --- reads ----------------------------------------------------------------------------


def lock_publication(session: Session, publication_id: str) -> PublicationRow:
    row = session.scalars(
        select(PublicationRow).where(PublicationRow.id == publication_id).with_for_update()
    ).first()
    if row is None:
        raise PublicationNotFoundError(publication_id)
    return row


def get_publication_row(session: Session, publication_id: str) -> PublicationRow:
    row = session.get(PublicationRow, publication_id)
    if row is None:
        raise PublicationNotFoundError(publication_id)
    return row


def find_for_target(
    session: Session, run_id: str, candidate_id: str, platform: str
) -> PublicationRow | None:
    return session.scalars(
        select(PublicationRow)
        .where(PublicationRow.run_id == run_id)
        .where(PublicationRow.candidate_id == candidate_id)
        .where(PublicationRow.platform == platform)
    ).first()


def publication_view(session: Session, row: PublicationRow) -> PublicationView:
    attempts = session.scalars(
        select(PublicationAttemptRow)
        .where(PublicationAttemptRow.publication_id == row.id)
        .order_by(PublicationAttemptRow.attempt)
    )
    return PublicationView(
        id=row.id,
        run_id=row.run_id,
        candidate_id=row.candidate_id,
        platform=row.platform,
        status=PublicationStatus(row.status),
        content=row.content,
        scheduled_for=row.scheduled_for,
        requested_by=row.requested_by,
        attempt_count=row.attempt_count,
        claimed_at=row.claimed_at,
        lease_expires_at=row.lease_expires_at,
        provider=row.provider,
        provider_post_id=row.provider_post_id,
        provider_post_url=row.provider_post_url,
        started_at=row.started_at,
        published_at=row.published_at,
        failure_category=row.failure_category,
        failure_message=row.failure_message,
        rate_limit_reset_at=row.rate_limit_reset_at,
        retry_not_before=row.retry_not_before,
        resolved_by=row.resolved_by,
        resolution_note=row.resolution_note,
        created_at=row.created_at,
        updated_at=row.updated_at,
        attempts=[PublicationAttemptView.model_validate(a, from_attributes=True) for a in attempts],
    )


def list_publications(
    session: Session,
    *,
    status: PublicationStatus | None = None,
    run_id: str | None = None,
    limit: int = 50,
) -> list[PublicationView]:
    query = select(PublicationRow).order_by(PublicationRow.created_at.desc()).limit(limit)
    if status is not None:
        query = query.where(PublicationRow.status == status.value)
    if run_id is not None:
        query = query.where(PublicationRow.run_id == run_id)
    return [publication_view(session, row) for row in session.scalars(query)]


def run_publications(session: Session, run_id: str) -> list[PublicationView]:
    rows = session.scalars(
        select(PublicationRow)
        .where(PublicationRow.run_id == run_id)
        .order_by(PublicationRow.created_at)
    )
    return [publication_view(session, row) for row in rows]


def publish_operation_counts(session: Session, run_id: str) -> list[tuple[str, int, int]]:
    """(provider, attempts, published) per provider for one run: operations, not prices."""
    rows = session.execute(
        select(
            PublicationAttemptRow.provider,
            func.count(),
            func.count().filter(PublicationAttemptRow.outcome == AttemptOutcome.SUCCEEDED.value),
        )
        .join(PublicationRow, PublicationRow.id == PublicationAttemptRow.publication_id)
        .where(PublicationRow.run_id == run_id)
        .group_by(PublicationAttemptRow.provider)
        .order_by(PublicationAttemptRow.provider)
    )
    return [(provider, attempts, published) for provider, attempts, published in rows]


def has_active_publication(session: Session, run_id: str) -> bool:
    statuses = [s.value for s in (*ACTIVE_PUBLICATION_STATUSES, PublicationStatus.PUBLISHED)]
    return (
        session.scalars(
            select(PublicationRow.id)
            .where(PublicationRow.run_id == run_id)
            .where(PublicationRow.status.in_(statuses))
            .limit(1)
        ).first()
        is not None
    )


def sanitized(message: str | None) -> str | None:
    """Collapse to one short line. Publisher messages already exclude credentials and
    raw provider payloads; this only bounds what is stored and shown."""
    if message is None:
        return None
    return " ".join(message.split())[:_MAX_FAILURE_MESSAGE]


def attempt_rows(session: Session, publication_id: str) -> Sequence[PublicationAttemptRow]:
    return list(
        session.scalars(
            select(PublicationAttemptRow)
            .where(PublicationAttemptRow.publication_id == publication_id)
            .order_by(PublicationAttemptRow.attempt)
        )
    )
