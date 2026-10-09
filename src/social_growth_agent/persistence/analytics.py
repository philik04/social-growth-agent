"""Queries and transitions on the analytics tables (Phase 6).

Every function takes an open session; callers own the transaction. No provider
knowledge and no policy decisions here: the rules live in ``policies/analytics.py``,
the orchestration in ``services/analytics.py`` and ``services/analytics_worker.py``.

Invariants enforced by the database, not by this code:

- one job per (publication, snapshot age): ``uq_analytics_jobs_target``;
- one snapshot per job and per (publication, snapshot age): ``uq_post_metrics_job_id``,
  ``uq_post_metrics_target``. Inserts use ``ON CONFLICT DO NOTHING``, so a restart, a
  duplicate claim or a backfill can never produce a second snapshot.
"""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from social_growth_agent.analytics import observed_rates
from social_growth_agent.errors import (
    AnalyticsJobNotFoundError,
    InvalidRunStateError,
    NotSchedulableError,
    PublicationNotFoundError,
)
from social_growth_agent.models import (
    AnalyticsFailureCategory,
    AnalyticsFetch,
    AnalyticsJobStatus,
    AttemptResult,
    PostMetricRecord,
    PublicationStatus,
    RequestOutcome,
    ScheduleBasis,
    SnapshotAge,
    new_id,
)
from social_growth_agent.persistence.publications import sanitized
from social_growth_agent.persistence.tables import (
    AnalyticsAttemptRow,
    AnalyticsJobRow,
    AnalyticsRequestRow,
    PostMetricRow,
    PublicationRow,
)
from social_growth_agent.persistence.views import (
    AnalyticsAttemptView,
    AnalyticsJobView,
    AnalyticsSummary,
    MetricSnapshotView,
)
from social_growth_agent.policies.analytics import (
    MANUAL_RETRY,
    RetryDecision,
    is_on_target,
    scheduled_for,
)

DEFAULT_ON_TARGET_SECONDS = 120

DecideFn = Callable[[AnalyticsFailureCategory, int, datetime | None], RetryDecision]
"""``(category, attempts made since the last manual retry, reset time) -> decision``."""


# --- scheduling ------------------------------------------------------------------------


@dataclass(frozen=True)
class EnqueueResult:
    publication_id: str
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    schedule_basis: ScheduleBasis | None = None


def schedule_basis(row: PublicationRow) -> tuple[ScheduleBasis, datetime]:
    """The verified platform creation time when known, else the recorded time.
    Raises ``NotSchedulableError`` when the publication cannot have analytics."""
    if row.status != PublicationStatus.PUBLISHED.value:
        raise NotSchedulableError(
            f"publication {row.id} is {row.status}; only published posts have analytics"
        )
    if not row.provider_post_id:
        raise NotSchedulableError(f"publication {row.id} has no platform post id")
    if row.provider_created_at is not None:
        return ScheduleBasis.PROVIDER_CREATED_AT, row.provider_created_at
    if row.published_at is None:
        raise NotSchedulableError(
            f"publication {row.id} has no publication timestamp; nothing was scheduled"
        )
    return ScheduleBasis.RECORDED_PUBLISHED_AT, row.published_at


def enqueue_jobs(
    session: Session,
    publication_id: str,
    ages: Sequence[SnapshotAge],
    *,
    origin: str,
) -> EnqueueResult:
    """Create the missing snapshot jobs for one published publication. Idempotent:
    existing (publication, age) jobs are left exactly as they are."""
    row = session.scalars(
        select(PublicationRow)
        .where(PublicationRow.id == publication_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if row is None:
        raise PublicationNotFoundError(publication_id)
    basis, basis_at = schedule_basis(row)
    created: list[str] = []
    existing: list[str] = []
    for age in ages:
        job_id = session.execute(
            insert(AnalyticsJobRow)
            .values(
                id=new_id("ajob"),
                publication_id=publication_id,
                snapshot_age=age.label,
                age_seconds=age.seconds,
                schedule_basis=basis.value,
                basis_at=basis_at,
                scheduled_for=scheduled_for(basis_at, age),
                origin=origin,
                status=AnalyticsJobStatus.SCHEDULED.value,
                attempt_count=0,
                attempt_base=0,
            )
            .on_conflict_do_nothing(constraint="uq_analytics_jobs_target")
            .returning(AnalyticsJobRow.id)
        ).scalar_one_or_none()
        if job_id is not None:
            created.append(job_id)
        else:
            existing.append(
                session.scalars(
                    select(AnalyticsJobRow.id)
                    .where(AnalyticsJobRow.publication_id == publication_id)
                    .where(AnalyticsJobRow.snapshot_age == age.label)
                ).one()
            )
    return EnqueueResult(
        publication_id=publication_id, created=created, existing=existing, schedule_basis=basis
    )


def reconcile_with_creation_time(
    session: Session, publication_id: str, provider_created_at: datetime, *, now: datetime
) -> list[str]:
    """The platform reported the post's creation time: record it once, then move the
    jobs that are still waiting onto it.

    Safe by construction: only ``scheduled`` jobs that no worker holds and that were
    planned from the recorded time are moved; collected snapshots are never touched
    (their timing fields already show their actual age); no job is created or deleted,
    so no duplicate can appear. ``original_scheduled_for`` keeps the old time.
    """
    session.execute(
        update(PublicationRow)
        .where(PublicationRow.id == publication_id)
        .where(PublicationRow.provider_created_at.is_(None))
        .values(provider_created_at=provider_created_at)
        .execution_options(synchronize_session=False)
    )
    jobs = session.scalars(
        select(AnalyticsJobRow)
        .where(AnalyticsJobRow.publication_id == publication_id)
        .where(AnalyticsJobRow.status == AnalyticsJobStatus.SCHEDULED.value)
        .where(AnalyticsJobRow.schedule_basis == ScheduleBasis.RECORDED_PUBLISHED_AT.value)
        .where(
            (AnalyticsJobRow.lease_expires_at.is_(None)) | (AnalyticsJobRow.lease_expires_at < now)
        )
        .with_for_update(skip_locked=True)
    )
    moved = []
    for job in jobs:
        target = provider_created_at + timedelta(seconds=job.age_seconds)
        if job.original_scheduled_for is None and target != job.scheduled_for:
            job.original_scheduled_for = job.scheduled_for
        job.scheduled_for = target
        job.schedule_basis = ScheduleBasis.PROVIDER_CREATED_AT.value
        job.basis_at = provider_created_at
        moved.append(job.id)
    return moved


# --- worker transitions ---------------------------------------------------------------


def sweep_expired_collecting(
    session: Session,
    *,
    now: datetime,
    decide: DecideFn,
) -> list[str]:
    """``collecting`` jobs whose lease expired: their worker died around its read.

    Unlike publishing, nothing is ambiguous: a metrics read creates nothing, so the job
    simply goes back to ``scheduled`` (after the backoff) or, out of attempts, to
    ``failed``. The unfinished attempt rows are closed as ``failed/lease_expired``; the
    unfinished request row stays as it is, visible as a call whose process died.
    """
    jobs = list(
        session.scalars(
            select(AnalyticsJobRow)
            .where(AnalyticsJobRow.status == AnalyticsJobStatus.COLLECTING.value)
            .where(AnalyticsJobRow.lease_expires_at.is_not(None))
            .where(AnalyticsJobRow.lease_expires_at < now)
            .with_for_update(skip_locked=True)
        )
    )
    for job in jobs:
        decision = decide(
            AnalyticsFailureCategory.LEASE_EXPIRED, job.attempt_count - job.attempt_base, None
        )
        _apply_failure(
            job,
            category=AnalyticsFailureCategory.LEASE_EXPIRED,
            message="the analytics worker stopped while collecting; the read is repeated",
            decision=decision,
            reset_at=None,
            attempts_made=job.attempt_count - job.attempt_base,
        )
        _close_open_attempts(
            session, job.id, AttemptResult.FAILED, AnalyticsFailureCategory.LEASE_EXPIRED, now
        )
    return [j.id for j in jobs]


def claim_due(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: int,
    limit: int,
    now: datetime,
    publication_id: str | None = None,
) -> list[str]:
    """Lease up to ``limit`` due jobs. ``FOR UPDATE SKIP LOCKED``: two workers never
    claim the same job. Future jobs and jobs waiting for ``retry_not_before`` are not due.
    ``publication_id`` limits the claim to one post (the live demo)."""
    due = (
        select(AnalyticsJobRow.id)
        .where(AnalyticsJobRow.status == AnalyticsJobStatus.SCHEDULED.value)
        .where(AnalyticsJobRow.scheduled_for <= now)
        .where(
            (AnalyticsJobRow.retry_not_before.is_(None)) | (AnalyticsJobRow.retry_not_before <= now)
        )
        .where(
            (AnalyticsJobRow.lease_expires_at.is_(None)) | (AnalyticsJobRow.lease_expires_at < now)
        )
        .order_by(AnalyticsJobRow.scheduled_for, AnalyticsJobRow.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if publication_id is not None:
        due = due.where(AnalyticsJobRow.publication_id == publication_id)
    ids = list(session.scalars(due))
    if ids:
        session.execute(
            update(AnalyticsJobRow)
            .where(AnalyticsJobRow.id.in_(ids))
            .values(
                claimed_by=worker_id,
                claimed_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            .execution_options(synchronize_session=False)
        )
    return ids


@dataclass(frozen=True)
class BegunItem:
    job_id: str
    attempt_id: str
    attempt: int
    """Attempts since the last manual retry (what the automatic bound counts)."""
    publication_id: str
    provider_post_id: str


@dataclass(frozen=True)
class Begun:
    request_id: str
    items: list[BegunItem]

    @property
    def post_ids(self) -> list[str]:
        return list(dict.fromkeys(i.provider_post_id for i in self.items))


def begin_collection(
    session: Session,
    job_ids: Sequence[str],
    *,
    worker_id: str,
    provider: str,
    metrics_scope: str,
    now: datetime,
) -> Begun | None:
    """Move claimed jobs to ``collecting`` and write the started request and attempt rows.

    Committed before the provider call, so an in-flight read is always visible.
    Conditional on each job still being ``scheduled`` *and* claimed by this worker: a
    job whose lease was lost in between is skipped. Returns None when none is left.
    """
    rows = session.execute(
        update(AnalyticsJobRow)
        .where(AnalyticsJobRow.id.in_(list(job_ids)))
        .where(AnalyticsJobRow.claimed_by == worker_id)
        .where(AnalyticsJobRow.status == AnalyticsJobStatus.SCHEDULED.value)
        .values(
            status=AnalyticsJobStatus.COLLECTING.value,
            attempt_count=AnalyticsJobRow.attempt_count + 1,
            retry_not_before=None,
        )
        .returning(
            AnalyticsJobRow.id,
            AnalyticsJobRow.attempt_count,
            AnalyticsJobRow.attempt_base,
            AnalyticsJobRow.publication_id,
        )
        .execution_options(synchronize_session=False)
    ).all()
    if not rows:
        return None
    post_ids: dict[str, str | None] = {
        pid: post_id
        for pid, post_id in session.execute(
            select(PublicationRow.id, PublicationRow.provider_post_id).where(
                PublicationRow.id.in_({r.publication_id for r in rows})
            )
        )
    }
    ordered = sorted(rows, key=lambda r: list(job_ids).index(r.id))
    request_id = start_request(
        session,
        worker_id=worker_id,
        provider=provider,
        metrics_scope=metrics_scope,
        post_ids=[str(post_ids[r.publication_id]) for r in ordered],
        now=now,
    )
    items = []
    for r in ordered:
        attempt_id = new_id("aatt")
        session.add(
            AnalyticsAttemptRow(
                id=attempt_id,
                request_id=request_id,
                job_id=r.id,
                publication_id=r.publication_id,
                provider_post_id=str(post_ids[r.publication_id]),
                attempt=r.attempt_count,
                outcome=AttemptResult.STARTED.value,
                started_at=now,
            )
        )
        items.append(
            BegunItem(
                job_id=r.id,
                attempt_id=attempt_id,
                attempt=r.attempt_count - r.attempt_base,
                publication_id=r.publication_id,
                provider_post_id=str(post_ids[r.publication_id]),
            )
        )
    return Begun(request_id=request_id, items=items)


def start_request(
    session: Session,
    *,
    worker_id: str,
    provider: str,
    metrics_scope: str,
    post_ids: Sequence[str],
    now: datetime,
) -> str:
    """The ``started`` ledger row for one provider read, flushed before the call.

    Used for every job batch and, on its own (no job, no snapshot), for a read that
    only probes a post, so no metrics read is ever invisible."""
    request_id = new_id("areq")
    session.add(
        AnalyticsRequestRow(
            id=request_id,
            worker_id=worker_id,
            provider=provider,
            metrics_scope=metrics_scope,
            post_ids=list(dict.fromkeys(post_ids)),
            started_at=now,
            outcome=RequestOutcome.STARTED.value,
        )
    )
    session.flush()
    return request_id


def finish_request(
    session: Session,
    request_id: str,
    *,
    now: datetime,
    fetch: AnalyticsFetch | None = None,
    error_category: AnalyticsFailureCategory | None = None,
    error_message: str | None = None,
    http_status: int | None = None,
    latency_ms: float | None = None,
    rate_limit_reset_at: datetime | None = None,
) -> None:
    if fetch is not None:
        outcome = RequestOutcome.PARTIAL if fetch.misses else RequestOutcome.SUCCEEDED
        values: dict[str, object] = {
            "outcome": outcome.value,
            "http_status": fetch.http_status,
            "latency_ms": fetch.latency_ms,
            "posts_returned": fetch.posts_returned,
            "rate_limit_remaining": fetch.rate_limit_remaining,
            "rate_limit_reset_at": fetch.rate_limit_reset_at,
        }
    else:
        values = {
            "outcome": RequestOutcome.FAILED.value,
            "error_category": error_category.value if error_category else None,
            "error_message": sanitized(error_message),
            "http_status": http_status,
            "latency_ms": latency_ms,
            "posts_returned": 0,
            "rate_limit_reset_at": rate_limit_reset_at,
        }
    session.execute(
        update(AnalyticsRequestRow)
        .where(AnalyticsRequestRow.id == request_id)
        .values(finished_at=now, **values)
        .execution_options(synchronize_session=False)
    )


def record_snapshot(
    session: Session,
    item: BegunItem,
    record: PostMetricRecord,
    *,
    request_id: str,
    platform: str,
    provider: str,
    metrics_scope: str,
    worker_id: str,
    now: datetime,
) -> bool:
    """Store the observation and finish the job. Returns False when nothing was stored
    because this worker no longer holds the job or a snapshot for it already exists."""
    job = _lock_job(session, item.job_id)
    # ``collected`` on an attempt means the platform returned the post to it (a post
    # read happened), even when the job is no longer this worker's to finish.
    _finish_attempt(session, item.attempt_id, AttemptResult.COLLECTED, None, now)
    if job.status != AnalyticsJobStatus.COLLECTING.value or job.claimed_by != worker_id:
        return False
    publication = session.get(PublicationRow, item.publication_id)
    stored = session.execute(
        insert(PostMetricRow)
        .values(
            id=new_id("pmet"),
            job_id=job.id,
            publication_id=job.publication_id,
            request_id=request_id,
            platform=platform,
            provider=provider,
            metrics_scope=metrics_scope,
            provider_post_id=record.provider_post_id,
            snapshot_age=job.snapshot_age,
            target_age_seconds=job.age_seconds,
            scheduled_for=job.scheduled_for,
            captured_at=now,
            provider_created_at=record.provider_created_at,
            recorded_published_at=publication.published_at if publication else None,
            likes=record.likes,
            reposts=record.reposts,
            replies=record.replies,
            quotes=record.quotes,
            bookmarks=record.bookmarks,
            impressions=record.impressions,
        )
        .on_conflict_do_nothing()
        .returning(PostMetricRow.id)
    ).scalar_one_or_none()
    job.status = AnalyticsJobStatus.COLLECTED.value
    job.collected_at = now
    job.failure_category = None
    job.failure_message = None
    _release(job)
    return stored is not None


def record_job_failure(
    session: Session,
    item: BegunItem,
    *,
    category: AnalyticsFailureCategory,
    message: str,
    decision: RetryDecision,
    reset_at: datetime | None,
    worker_id: str,
    now: datetime,
) -> bool:
    """Apply the policy's decision to one job. False if the job is no longer ours."""
    job = _lock_job(session, item.job_id)
    result = (
        AttemptResult.NOT_FOUND
        if category is AnalyticsFailureCategory.NOT_FOUND
        else AttemptResult.FAILED
    )
    _finish_attempt(session, item.attempt_id, result, category, now)
    if job.status != AnalyticsJobStatus.COLLECTING.value or job.claimed_by != worker_id:
        return False
    _apply_failure(
        job,
        category=category,
        message=message,
        decision=decision,
        reset_at=reset_at,
        attempts_made=item.attempt,
    )
    return True


def cancel_waiting_jobs(session: Session, publication_id: str, *, reason: str) -> list[str]:
    """Cancel the ``scheduled`` jobs of a post the platform no longer returns."""
    rows = session.execute(
        update(AnalyticsJobRow)
        .where(AnalyticsJobRow.publication_id == publication_id)
        .where(AnalyticsJobRow.status == AnalyticsJobStatus.SCHEDULED.value)
        .values(
            status=AnalyticsJobStatus.CANCELLED.value,
            failure_category=AnalyticsFailureCategory.NOT_FOUND.value,
            failure_message=sanitized(reason),
            retry_not_before=None,
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
        )
        .returning(AnalyticsJobRow.id)
        .execution_options(synchronize_session=False)
    )
    return list(rows.scalars())


def manual_retry(session: Session, job_id: str) -> AnalyticsJobRow:
    """A human queues a failed job again (``auth`` once credentials are fixed, or a
    retryable category whose attempts ran out). Grants a fresh automatic budget."""
    job = _lock_job(session, job_id)
    if job.status != AnalyticsJobStatus.FAILED.value:
        raise InvalidRunStateError(
            f"analytics job {job_id} is {job.status}; only failed jobs can be retried"
        )
    category = AnalyticsFailureCategory(job.failure_category) if job.failure_category else None
    if category is not None and category not in MANUAL_RETRY:
        raise InvalidRunStateError(
            f"analytics job {job_id} failed with '{category.value}', which cannot succeed "
            "on a retry"
        )
    job.status = AnalyticsJobStatus.SCHEDULED.value
    job.attempt_base = job.attempt_count
    job.retry_not_before = None
    job.failure_category = None
    job.failure_message = None
    _release(job)
    return job


# --- helpers ---------------------------------------------------------------------------


def _lock_job(session: Session, job_id: str) -> AnalyticsJobRow:
    job = session.scalars(
        select(AnalyticsJobRow)
        .where(AnalyticsJobRow.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if job is None:
        raise AnalyticsJobNotFoundError(job_id)
    return job


def _release(job: AnalyticsJobRow) -> None:
    job.claimed_by = None
    job.claimed_at = None
    job.lease_expires_at = None


def _apply_failure(
    job: AnalyticsJobRow,
    *,
    category: AnalyticsFailureCategory,
    message: str,
    decision: RetryDecision,
    reset_at: datetime | None,
    attempts_made: int,
) -> None:
    job.status = decision.status.value
    job.retry_not_before = decision.retry_not_before
    job.failure_category = category.value
    suffix = f" (no attempt left after {attempts_made})" if decision.exhausted else ""
    job.failure_message = sanitized(message + suffix)
    job.rate_limit_reset_at = reset_at
    _release(job)


def _finish_attempt(
    session: Session,
    attempt_id: str,
    outcome: AttemptResult,
    category: AnalyticsFailureCategory | None,
    now: datetime,
) -> None:
    session.execute(
        update(AnalyticsAttemptRow)
        .where(AnalyticsAttemptRow.id == attempt_id)
        .where(AnalyticsAttemptRow.outcome == AttemptResult.STARTED.value)
        .values(
            outcome=outcome.value,
            failure_category=category.value if category else None,
            finished_at=now,
        )
        .execution_options(synchronize_session=False)
    )


def _close_open_attempts(
    session: Session,
    job_id: str,
    outcome: AttemptResult,
    category: AnalyticsFailureCategory,
    now: datetime,
) -> None:
    session.execute(
        update(AnalyticsAttemptRow)
        .where(AnalyticsAttemptRow.job_id == job_id)
        .where(AnalyticsAttemptRow.outcome == AttemptResult.STARTED.value)
        .values(outcome=outcome.value, failure_category=category.value, finished_at=now)
        .execution_options(synchronize_session=False)
    )


# --- reads -----------------------------------------------------------------------------


def get_job_row(session: Session, job_id: str) -> AnalyticsJobRow:
    job = session.get(AnalyticsJobRow, job_id)
    if job is None:
        raise AnalyticsJobNotFoundError(job_id)
    return job


def job_view(
    session: Session, job: AnalyticsJobRow, *, with_attempts: bool = True
) -> AnalyticsJobView:
    attempts = (
        [
            AnalyticsAttemptView.model_validate(a, from_attributes=True)
            for a in session.scalars(
                select(AnalyticsAttemptRow)
                .where(AnalyticsAttemptRow.job_id == job.id)
                .order_by(AnalyticsAttemptRow.attempt)
            )
        ]
        if with_attempts
        else []
    )
    return AnalyticsJobView(
        id=job.id,
        publication_id=job.publication_id,
        snapshot_age=job.snapshot_age,
        age_seconds=job.age_seconds,
        schedule_basis=job.schedule_basis,
        basis_at=job.basis_at,
        scheduled_for=job.scheduled_for,
        original_scheduled_for=job.original_scheduled_for,
        origin=job.origin,
        status=AnalyticsJobStatus(job.status),
        attempt_count=job.attempt_count,
        retry_not_before=job.retry_not_before,
        failure_category=job.failure_category,
        failure_message=job.failure_message,
        rate_limit_reset_at=job.rate_limit_reset_at,
        claimed_at=job.claimed_at,
        lease_expires_at=job.lease_expires_at,
        collected_at=job.collected_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
        attempts=attempts,
    )


def list_jobs(
    session: Session,
    *,
    status: AnalyticsJobStatus | None = None,
    publication_id: str | None = None,
    limit: int = 50,
) -> list[AnalyticsJobView]:
    query = select(AnalyticsJobRow).order_by(AnalyticsJobRow.scheduled_for, AnalyticsJobRow.id)
    if status is not None:
        query = query.where(AnalyticsJobRow.status == status.value)
    if publication_id is not None:
        query = query.where(AnalyticsJobRow.publication_id == publication_id)
    return [job_view(session, j) for j in session.scalars(query.limit(limit))]


def snapshot_view(
    row: PostMetricRow, *, on_target_seconds: int = DEFAULT_ON_TARGET_SECONDS
) -> MetricSnapshotView:
    basis_at: datetime | None
    if row.provider_created_at is not None:
        basis, basis_at = ScheduleBasis.PROVIDER_CREATED_AT, row.provider_created_at
    else:
        basis, basis_at = ScheduleBasis.RECORDED_PUBLISHED_AT, row.recorded_published_at
    actual = (row.captured_at - basis_at).total_seconds() if basis_at is not None else None
    return MetricSnapshotView(
        id=row.id,
        publication_id=row.publication_id,
        platform=row.platform,
        provider=row.provider,
        metrics_scope=row.metrics_scope,
        provider_post_id=row.provider_post_id,
        snapshot_age=row.snapshot_age,
        target_age_seconds=row.target_age_seconds,
        scheduled_for=row.scheduled_for,
        captured_at=row.captured_at,
        capture_delay_seconds=(row.captured_at - row.scheduled_for).total_seconds(),
        provider_created_at=row.provider_created_at,
        recorded_published_at=row.recorded_published_at,
        age_basis=basis.value,
        actual_age_seconds=actual,
        on_target=(
            None
            if actual is None
            else is_on_target(actual, row.target_age_seconds, on_target_seconds)
        ),
        likes=row.likes,
        reposts=row.reposts,
        replies=row.replies,
        quotes=row.quotes,
        bookmarks=row.bookmarks,
        impressions=row.impressions,
        derived=observed_rates(
            likes=row.likes,
            reposts=row.reposts,
            replies=row.replies,
            quotes=row.quotes,
            impressions=row.impressions,
        ),
    )


def snapshots(session: Session, publication_id: str) -> list[MetricSnapshotView]:
    """Ordered by target age, then capture time."""
    rows = session.scalars(
        select(PostMetricRow)
        .where(PostMetricRow.publication_id == publication_id)
        .order_by(PostMetricRow.target_age_seconds, PostMetricRow.captured_at)
    )
    return [snapshot_view(r) for r in rows]


def analytics_summary(session: Session, publication_id: str) -> AnalyticsSummary | None:
    statuses: Counter[str] = Counter(
        {
            status: count
            for status, count in session.execute(
                select(AnalyticsJobRow.status, func.count())
                .where(AnalyticsJobRow.publication_id == publication_id)
                .group_by(AnalyticsJobRow.status)
            )
        }
    )
    taken = snapshots(session, publication_id)
    if not statuses and not taken:
        return None
    publication = session.get(PublicationRow, publication_id)
    return AnalyticsSummary(
        jobs_by_status=dict(sorted(statuses.items())),
        snapshot_count=len(taken),
        provider_created_at=publication.provider_created_at if publication else None,
        latest=taken[-1] if taken else None,
    )


def analytics_operation_counts(
    session: Session, run_id: str
) -> list[tuple[str, int, int, int, int, int]]:
    """Per provider for one run's publications: (provider, requests, post reads,
    snapshots, failed calls, unfinished calls). Counts, not prices.

    A request batching posts of several runs counts once for each run it touched, so
    ``requests`` is not additive across runs; cost uses post reads only. A post read is
    attributed to the attempt whose job collected (or was answered for) that post.
    """
    attempts = (
        select(
            AnalyticsRequestRow.provider.label("provider"),
            AnalyticsAttemptRow.request_id.label("request_id"),
            AnalyticsAttemptRow.outcome.label("outcome"),
            AnalyticsRequestRow.outcome.label("request_outcome"),
            AnalyticsRequestRow.finished_at.label("finished_at"),
        )
        .join(AnalyticsRequestRow, AnalyticsRequestRow.id == AnalyticsAttemptRow.request_id)
        .join(PublicationRow, PublicationRow.id == AnalyticsAttemptRow.publication_id)
        .where(PublicationRow.run_id == run_id)
        .subquery()
    )
    rows = session.execute(
        select(
            attempts.c.provider,
            func.count(func.distinct(attempts.c.request_id)),
            func.count().filter(attempts.c.outcome == AttemptResult.COLLECTED.value),
            func.count(func.distinct(attempts.c.request_id)).filter(
                attempts.c.request_outcome == RequestOutcome.FAILED.value
            ),
            func.count(func.distinct(attempts.c.request_id)).filter(
                attempts.c.finished_at.is_(None)
            ),
        )
        .group_by(attempts.c.provider)
        .order_by(attempts.c.provider)
    ).all()
    snapshot_counts: dict[str, int] = {
        provider: count
        for provider, count in session.execute(
            select(PostMetricRow.provider, func.count())
            .join(PublicationRow, PublicationRow.id == PostMetricRow.publication_id)
            .where(PublicationRow.run_id == run_id)
            .group_by(PostMetricRow.provider)
        )
    }
    return [
        (provider, requests, reads, snapshot_counts.get(provider, 0), failed, unfinished)
        for provider, requests, reads, failed, unfinished in rows
    ]
