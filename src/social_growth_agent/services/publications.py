"""Publication service: turns an explicit human publish request into a durable intent.

Nothing here calls a platform. Approval (content approval, which ends the run) and
publishing are separate explicit actions, and the intent is always committed before any
worker may make an external call. Endpoints translate HTTP to these calls and no more.
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from social_growth_agent.errors import (
    CandidateNotFoundError,
    InvalidRunStateError,
    InvalidScheduleError,
    NotPublishableError,
    PublicationExistsError,
)
from social_growth_agent.models import (
    PublicationStatus,
    PublishFailureCategory,
    utc_now,
)
from social_growth_agent.observability import get_logger
from social_growth_agent.persistence import Database
from social_growth_agent.persistence import publications as pubs
from social_growth_agent.persistence import repository as repo
from social_growth_agent.persistence.tables import ContentCandidateRow, PublicationRow
from social_growth_agent.persistence.views import PublicationView
from social_growth_agent.policies import (
    ScheduleWindow,
    content_sha256,
    idempotency_key,
    is_due,
    publish_refusals,
    schedule_refusal,
)

if TYPE_CHECKING:
    from social_growth_agent.services.analytics import AnalyticsSchedule

_log = get_logger("publications")

DEFAULT_PLATFORM = "x"

# Where a failure may be retried by hand, once the cause is addressed.
RETRYABLE_CATEGORIES = frozenset(
    {
        PublishFailureCategory.AUTH,
        PublishFailureCategory.RATE_LIMITED,
        PublishFailureCategory.NOT_SENT,
        PublishFailureCategory.POLICY,
    }
)
CANCELLABLE = (PublicationStatus.SCHEDULED, PublicationStatus.READY)


class PublicationService:
    def __init__(
        self,
        db: Database,
        *,
        platform: str = DEFAULT_PLATFORM,
        window: ScheduleWindow | None = None,
        analytics: "AnalyticsSchedule | None" = None,
    ) -> None:
        self._db = db
        self._platform = platform
        self._window = window or ScheduleWindow()
        self._analytics = analytics

    # --- commands -------------------------------------------------------------------

    def request(
        self,
        run_id: str,
        candidate_id: str,
        *,
        scheduled_for: datetime | None = None,
        requested_by: str | None = None,
    ) -> PublicationView:
        """Create the publication intent for an approved candidate.

        Raises ``RunNotFoundError``/``CandidateNotFoundError`` (404),
        ``InvalidScheduleError`` (422), ``NotPublishableError`` (409) and
        ``PublicationExistsError`` (409, carrying the existing publication).
        """
        now = utc_now()
        refusal = schedule_refusal(scheduled_for, now, self._window)
        if refusal is not None:
            raise InvalidScheduleError(refusal)
        with self._db.transaction() as session:
            run_row = repo.lock_run(session, run_id)
            candidate = self._candidate(session, run_id, candidate_id)
            facts = pubs.publish_facts(session, run_row, candidate, self._platform)
            existing = pubs.find_for_target(session, run_id, candidate_id, self._platform)
            if existing is not None and existing.status != PublicationStatus.CANCELLED.value:
                raise _exists(existing)
            refusals = [
                r
                for r in publish_refusals(facts)
                if not r.startswith("a publication already exists")
            ]
            if refusals:
                raise NotPublishableError("; ".join(refusals))
            due = is_due(scheduled_for, now)
            status = PublicationStatus.READY if due else PublicationStatus.SCHEDULED
            if existing is not None:
                # A cancelled intent keeps the unique key, so a fresh request revives
                # that same row rather than creating a second intent for the target.
                pubs.requeue_cancelled(
                    session,
                    existing.id,
                    status=status,
                    scheduled_for=None if due else scheduled_for,
                    requested_by=requested_by,
                )
                return pubs.publication_view(
                    session, pubs.get_publication_row(session, existing.id)
                )
            publication_id = pubs.insert_intent(
                session,
                run_id=run_id,
                candidate_id=candidate_id,
                platform=self._platform,
                idempotency_key=idempotency_key(self._platform, run_id, candidate_id),
                content=candidate.content,
                content_sha256=content_sha256(candidate.content),
                status=status,
                scheduled_for=None if due else scheduled_for,
                requested_by=requested_by,
            )
            if publication_id is None:
                # A concurrent request won the unique key; report that publication.
                concurrent = pubs.find_for_target(session, run_id, candidate_id, self._platform)
                if concurrent is None:  # pragma: no cover - the key exists by definition
                    raise InvalidRunStateError("a concurrent publication request won the key")
                raise _exists(concurrent)
            _log.info(
                "publication requested",
                extra={"run_id": run_id, "publication_id": publication_id, "status": status.value},
            )
            return pubs.publication_view(session, pubs.get_publication_row(session, publication_id))

    def retry(self, publication_id: str, *, requested_by: str | None = None) -> PublicationView:
        """Queue a failed publication again. Refused for every other state, for a
        category that must not be retried (duplicate content, bad request) and before a
        rate-limit reset has passed. ``unknown`` is never retried: resolve it instead."""
        with self._db.transaction() as session:
            row = pubs.lock_publication(session, publication_id)
            status = PublicationStatus(row.status)
            if status is not PublicationStatus.FAILED:
                raise InvalidRunStateError(
                    f"publication {publication_id} is {status.value}; only failed "
                    "publications can be retried"
                )
            category = (
                PublishFailureCategory(row.failure_category) if row.failure_category else None
            )
            if category is not None and category not in RETRYABLE_CATEGORIES:
                raise InvalidRunStateError(
                    f"publication {publication_id} failed with '{category.value}', "
                    "which must not be retried"
                )
            now = utc_now()
            if row.rate_limit_reset_at is not None and row.rate_limit_reset_at > now:
                raise InvalidRunStateError(
                    f"publication {publication_id} is rate limited until "
                    f"{row.rate_limit_reset_at.isoformat()}"
                )
            self._recheck(session, row)
            pubs.set_outcome(session, publication_id, status=PublicationStatus.READY)
            row.requested_by = requested_by or row.requested_by
            return pubs.publication_view(session, pubs.get_publication_row(session, publication_id))

    def resolve(
        self,
        publication_id: str,
        *,
        published: bool,
        provider_post_id: str | None = None,
        resolved_by: str,
        note: str | None = None,
    ) -> PublicationView:
        """Close an ``unknown`` publication with what a human established.

        ``published`` records the post that was found (its id is required).
        ``not_published`` returns the row to ``ready``, which is the only way an
        ambiguous outcome is ever published again. Resolving anything else is refused.
        """
        with self._db.transaction() as session:
            row = pubs.lock_publication(session, publication_id)
            status = PublicationStatus(row.status)
            if status is not PublicationStatus.UNKNOWN:
                raise InvalidRunStateError(
                    f"publication {publication_id} is {status.value}; only unknown "
                    "publications can be resolved"
                )
            if published:
                if not provider_post_id:
                    raise InvalidRunStateError(
                        "resolving as published requires the provider_post_id of the post"
                    )
                pubs.mark_published(
                    session,
                    publication_id,
                    provider_post_id=provider_post_id,
                    provider_post_url=f"https://x.com/i/web/status/{provider_post_id}"
                    if self._platform == "x"
                    else None,
                    resolved_by=resolved_by,
                    resolution_note=note,
                )
                if self._analytics is not None:
                    # Scheduled from the *recorded* (resolution) time until the platform
                    # reports the real creation time; the first metrics read then
                    # reconciles the jobs still waiting (see persistence/analytics.py).
                    self._analytics.enqueue_on_publish_in(session, publication_id)
            else:
                self._recheck(session, row)
                pubs.set_outcome(session, publication_id, status=PublicationStatus.READY)
                row.resolved_by = resolved_by
                row.resolution_note = note
            _log.warning(
                "unknown publication resolved by hand",
                extra={"publication_id": publication_id, "published": published},
            )
            return pubs.publication_view(session, pubs.get_publication_row(session, publication_id))

    def cancel(self, publication_id: str, *, cancelled_by: str | None = None) -> PublicationView:
        """Withdraw a scheduled or ready publication that no worker holds."""
        with self._db.transaction() as session:
            row = pubs.lock_publication(session, publication_id)
            status = PublicationStatus(row.status)
            now = utc_now()
            leased = row.lease_expires_at is not None and row.lease_expires_at > now
            if status not in CANCELLABLE or leased:
                raise InvalidRunStateError(
                    f"publication {publication_id} is {status.value}"
                    f"{' and claimed by a worker' if leased else ''}; "
                    "only unclaimed scheduled or ready publications can be cancelled"
                )
            pubs.set_outcome(session, publication_id, status=PublicationStatus.CANCELLED)
            row.resolved_by = cancelled_by
            return pubs.publication_view(session, pubs.get_publication_row(session, publication_id))

    # --- queries --------------------------------------------------------------------

    def get(self, publication_id: str) -> PublicationView:
        with self._db.transaction() as session:
            return pubs.publication_view(session, pubs.get_publication_row(session, publication_id))

    def list(
        self,
        *,
        status: PublicationStatus | None = None,
        run_id: str | None = None,
        limit: int = 50,
    ) -> list[PublicationView]:
        with self._db.transaction() as session:
            return pubs.list_publications(session, status=status, run_id=run_id, limit=limit)

    def due_count(self, *, now: datetime | None = None) -> int:
        """How many publications a worker could claim right now (diagnostics only)."""
        now = now or utc_now()
        with self._db.transaction() as session:
            return len(
                [
                    p
                    for p in pubs.list_publications(session, limit=200)
                    if PublicationStatus(p.status) in pubs.CLAIMABLE
                    and is_due(p.scheduled_for, now)
                    and (p.retry_not_before is None or p.retry_not_before <= now)
                ]
            )

    # --- internals ------------------------------------------------------------------

    def _candidate(self, session: Session, run_id: str, candidate_id: str) -> ContentCandidateRow:
        row = session.get(ContentCandidateRow, candidate_id)
        if row is None or row.run_id != run_id:
            raise CandidateNotFoundError(f"{candidate_id} is not a candidate of run {run_id}")
        return row

    def _recheck(self, session: Session, row: PublicationRow) -> None:
        """The policy must still allow publishing before a row is queued again."""
        run_row = repo.get_run_row(session, row.run_id)
        candidate = self._candidate(session, row.run_id, row.candidate_id)
        facts = pubs.publish_facts(session, run_row, candidate, row.platform)
        refusals = [
            r for r in publish_refusals(facts) if not r.startswith("a publication already exists")
        ]
        if content_sha256(candidate.content) != row.content_sha256:
            refusals.append("the candidate's content changed since the intent was created")
        if refusals:
            raise NotPublishableError("; ".join(refusals))


def _exists(row: PublicationRow) -> PublicationExistsError:
    return PublicationExistsError(
        f"a publication for run {row.run_id} and candidate {row.candidate_id} already "
        f"exists ({row.status})",
        publication_id=row.id,
        status=row.status,
    )


def next_due_delay(scheduled_for: datetime | None, now: datetime) -> timedelta:
    """How long until a scheduled publication becomes due (zero if it already is)."""
    if scheduled_for is None or scheduled_for <= now:
        return timedelta(0)
    return scheduled_for - now
