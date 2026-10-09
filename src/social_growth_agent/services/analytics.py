"""Analytics service: explicit scheduling, manual retry and read access (Phase 6).

Nothing here calls a platform; only the analytics worker does. Jobs are created when a
publication is recorded as published (``enqueue_on_publish``) or by an explicit
backfill. Nothing is ever scheduled at startup.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from social_growth_agent.errors import NotSchedulableError, PublicationNotFoundError
from social_growth_agent.models import (
    AnalyticsJobStatus,
    PublicationStatus,
    ScheduleBasis,
    SnapshotAge,
    utc_now,
)
from social_growth_agent.observability import get_logger
from social_growth_agent.persistence import Database
from social_growth_agent.persistence import analytics as adb
from social_growth_agent.persistence.lineage import publication_lineage, published_posts
from social_growth_agent.persistence.tables import AnalyticsJobRow, PublicationRow
from social_growth_agent.persistence.views import (
    AnalyticsJobView,
    LineageView,
    MetricSnapshotView,
    PublishedPostView,
)
from social_growth_agent.policies.analytics import parse_snapshot_ages

_log = get_logger("analytics")

DEFAULT_AGES = parse_snapshot_ages("1h,24h,72h")


class BackfillResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    publication_id: str
    created_job_ids: list[str]
    existing_job_ids: list[str]
    schedule_basis: ScheduleBasis | None
    dry_run: bool = False


@dataclass(frozen=True)
class AnalyticsSchedule:
    """The configured snapshot ages, and whether publishing creates the jobs."""

    ages: tuple[SnapshotAge, ...] = DEFAULT_AGES
    enqueue_on_publish: bool = True

    def enqueue_on_publish_in(self, session: Session, publication_id: str) -> list[str]:
        """Called in the same transaction that records the publication as published."""
        if not self.enqueue_on_publish:
            return []
        result = adb.enqueue_jobs(session, publication_id, self.ages, origin="publish")
        return result.created


class AnalyticsService:
    def __init__(self, db: Database, *, schedule: AnalyticsSchedule | None = None) -> None:
        self._db = db
        self._schedule = schedule or AnalyticsSchedule()

    @property
    def ages(self) -> Sequence[SnapshotAge]:
        return self._schedule.ages

    # --- commands -------------------------------------------------------------------

    def backfill(self, publication_id: str, *, dry_run: bool = False) -> BackfillResult:
        """Create the configured jobs this published publication does not have yet.

        Explicit only (endpoint or CLI): migrations and startup never schedule anything.
        Idempotent: existing jobs are reported, never duplicated or changed. Raises
        ``PublicationNotFoundError`` (404) or ``NotSchedulableError`` (409).
        """
        with self._db.transaction() as session:
            if dry_run:
                row = session.get(PublicationRow, publication_id)
                if row is None:
                    raise PublicationNotFoundError(publication_id)
                basis, _ = adb.schedule_basis(row)
                have = set(
                    session.scalars(
                        select(AnalyticsJobRow.snapshot_age).where(
                            AnalyticsJobRow.publication_id == publication_id
                        )
                    )
                )
                return BackfillResult(
                    publication_id=publication_id,
                    created_job_ids=[a.label for a in self.ages if a.label not in have],
                    existing_job_ids=sorted(have),
                    schedule_basis=basis,
                    dry_run=True,
                )
            result = adb.enqueue_jobs(session, publication_id, self.ages, origin="backfill")
        if result.created:
            _log.info(
                "analytics backfilled",
                extra={"publication_id": publication_id, "jobs_created": len(result.created)},
            )
        return BackfillResult(
            publication_id=publication_id,
            created_job_ids=result.created,
            existing_job_ids=result.existing,
            schedule_basis=result.schedule_basis,
        )

    def backfill_all_published(self, *, dry_run: bool = False) -> list[BackfillResult]:
        with self._db.transaction() as session:
            ids = list(
                session.scalars(
                    select(PublicationRow.id)
                    .where(PublicationRow.status == PublicationStatus.PUBLISHED.value)
                    .order_by(PublicationRow.published_at, PublicationRow.id)
                )
            )
        results = []
        for publication_id in ids:
            try:
                results.append(self.backfill(publication_id, dry_run=dry_run))
            except NotSchedulableError as exc:
                _log.warning(
                    "publication not schedulable", extra={"id": publication_id, "why": str(exc)}
                )
        return results

    def retry_job(self, job_id: str) -> AnalyticsJobView:
        """Queue a failed job again (409 for a terminal category or another state)."""
        with self._db.transaction() as session:
            job = adb.manual_retry(session, job_id)
            return adb.job_view(session, job)

    # --- queries --------------------------------------------------------------------

    def metrics(self, publication_id: str) -> list[MetricSnapshotView]:
        with self._db.transaction() as session:
            if session.get(PublicationRow, publication_id) is None:
                raise PublicationNotFoundError(publication_id)
            return adb.snapshots(session, publication_id)

    def jobs(
        self,
        *,
        status: AnalyticsJobStatus | None = None,
        publication_id: str | None = None,
        limit: int = 50,
    ) -> list[AnalyticsJobView]:
        with self._db.transaction() as session:
            return adb.list_jobs(session, status=status, publication_id=publication_id, limit=limit)

    def job(self, job_id: str) -> AnalyticsJobView:
        with self._db.transaction() as session:
            return adb.job_view(session, adb.get_job_row(session, job_id))

    def posts(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        account_id: str | None = None,
        job_status: AnalyticsJobStatus | None = None,
        limit: int = 50,
    ) -> list[PublishedPostView]:
        with self._db.transaction() as session:
            return published_posts(
                session,
                since=since,
                until=until,
                account_id=account_id,
                job_status=job_status.value if job_status else None,
                limit=limit,
            )

    def lineage(self, publication_id: str) -> LineageView:
        with self._db.transaction() as session:
            return publication_lineage(session, publication_id)

    def due_count(self, *, now: datetime | None = None) -> int:
        """Jobs a worker could claim right now (diagnostics only; calls nothing)."""
        now = now or utc_now()
        with self._db.transaction() as session:
            return len(
                [
                    j
                    for j in session.scalars(
                        select(AnalyticsJobRow)
                        .where(AnalyticsJobRow.status == AnalyticsJobStatus.SCHEDULED.value)
                        .where(AnalyticsJobRow.scheduled_for <= now)
                    )
                    if j.retry_not_before is None or j.retry_not_before <= now
                ]
            )
