"""Publisher worker: the only process that calls a platform to create a post.

One cycle (``run_once``):

1. sweep ``publishing`` rows whose lease expired into ``unknown`` (their worker died
   around its call, so the outcome is not knowable from here);
2. claim due ``ready``/``scheduled`` rows with ``FOR UPDATE SKIP LOCKED`` and a lease,
   so two workers never publish the same row;
3. per claimed row, in separate transactions:
   a. re-check the deterministic publish policy and the content hash,
   b. commit ``status=publishing`` plus a started attempt row,
   c. call the publisher (the only external call),
   d. commit the outcome.

Between (b) and (d) the publication is visible as ``publishing`` with an unfinished
attempt. If this process dies there, no one knows whether the post exists, so the row
becomes ``unknown`` and is never retried automatically. Only a failure proven to have
happened before the request was sent returns to ``ready``, bounded by
``PUBLISH_MAX_ATTEMPTS`` and (since Phase 6) not claimable before a deterministic
backoff has passed (``retry_not_before``; nothing sleeps).

When analytics scheduling is configured, the transaction that records a post as
published also creates its snapshot jobs (Phase 6); no platform call is involved.
"""

import os
import signal
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from types import FrameType
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from social_growth_agent.errors import (
    DatabaseUnavailableError,
    NotPublishableError,
    PublishError,
    PublishNotSentError,
    PublishOutcomeUnknownError,
    PublishRejectedError,
    SocialGrowthError,
)
from social_growth_agent.models import (
    AttemptOutcome,
    PublicationStatus,
    PublishFailureCategory,
    PublishRequest,
    utc_now,
)
from social_growth_agent.observability import get_logger
from social_growth_agent.persistence import Database
from social_growth_agent.persistence import publications as pubs
from social_growth_agent.persistence import repository as repo
from social_growth_agent.persistence.tables import ContentCandidateRow, PublicationRow
from social_growth_agent.policies import content_sha256, publish_refusals
from social_growth_agent.policies.retry import Backoff
from social_growth_agent.providers import SocialPublisher

if TYPE_CHECKING:
    from social_growth_agent.services.analytics import AnalyticsSchedule

_log = get_logger("publisher")


def worker_id() -> str:
    """Host and pid: enough to tell two workers apart in the lease column."""
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True)
class CycleReport:
    """What one cycle did. Returned so demos and tests can assert on it."""

    swept: tuple[str, ...] = ()
    claimed: tuple[str, ...] = ()
    published: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    requeued: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()

    @property
    def calls_made(self) -> int:
        return len(self.published) + len(self.failed) + len(self.unknown) + len(self.requeued)


class PublisherWorker:
    def __init__(
        self,
        *,
        db: Database,
        publisher: SocialPublisher,
        lease_seconds: int = 60,
        max_attempts: int = 3,
        batch_size: int = 5,
        name: str | None = None,
        analytics: "AnalyticsSchedule | None" = None,
        backoff: Backoff | None = None,
    ) -> None:
        self._db = db
        self._analytics = analytics
        self._backoff = backoff
        self._publisher = publisher
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._batch_size = batch_size
        self.id = name or worker_id()

    # --- one cycle ------------------------------------------------------------------

    def run_once(self, *, now: datetime | None = None) -> CycleReport:
        now = now or utc_now()
        with self._db.transaction() as session:
            swept = pubs.sweep_expired_publishing(session, now)
        with self._db.transaction() as session:
            claimed = pubs.claim_due(
                session,
                worker_id=self.id,
                lease_seconds=self._lease_seconds,
                limit=self._batch_size,
                now=now,
            )
        if swept:
            _log.warning("publications left in flight by a dead worker", extra={"ids": swept})
        outcomes: dict[str, list[str]] = {
            "published": [],
            "failed": [],
            "unknown": [],
            "requeued": [],
            "skipped": [],
        }
        for publication_id in claimed:
            outcomes[self._publish_one(publication_id)].append(publication_id)
        return CycleReport(
            swept=tuple(swept),
            claimed=tuple(claimed),
            published=tuple(outcomes["published"]),
            failed=tuple(outcomes["failed"]),
            unknown=tuple(outcomes["unknown"]),
            requeued=tuple(outcomes["requeued"]),
            skipped=tuple(outcomes["skipped"]),
        )

    def _publish_one(self, publication_id: str) -> str:
        """Returns the bucket this publication ended in. Never raises for a publish
        failure: every outcome becomes an explicit state."""
        try:
            prepared = self._prepare(publication_id)
        except NotPublishableError as exc:
            self._record_failure(
                publication_id,
                attempt_id=None,
                category=PublishFailureCategory.POLICY,
                message=str(exc),
            )
            return "failed"
        except SocialGrowthError:
            _log.exception("could not prepare publication", extra={"id": publication_id})
            return "skipped"
        if prepared is None:
            return "skipped"
        attempt_id, request, attempt = prepared

        started = time.perf_counter()
        try:
            result = self._publisher.publish(request)
        except PublishError as exc:
            return self._handle_error(publication_id, attempt_id, attempt, exc, started)
        except Exception as exc:  # a bug in the publisher: the call may have happened
            _log.exception("publisher raised an unexpected error", extra={"id": publication_id})
            unknown = PublishOutcomeUnknownError(
                f"the publisher failed unexpectedly ({type(exc).__name__}); "
                "whether the post exists is unknown",
                failure_category=PublishFailureCategory.INTERNAL,
            )
            return self._handle_error(publication_id, attempt_id, attempt, unknown, started)

        with self._db.transaction() as session:
            pubs.finish_attempt(
                session,
                attempt_id,
                outcome=AttemptOutcome.SUCCEEDED,
                latency_ms=result.latency_ms or (time.perf_counter() - started) * 1000,
                http_status=result.http_status,
                provider_post_id=result.provider_post_id,
            )
            pubs.mark_published(
                session,
                publication_id,
                provider_post_id=result.provider_post_id,
                provider_post_url=result.provider_post_url,
            )
            if self._analytics is not None:
                # Same transaction: a published post always has its snapshot jobs.
                self._analytics.enqueue_on_publish_in(session, publication_id)
        _log.info(
            "published",
            extra={"publication_id": publication_id, "post_id": result.provider_post_id},
        )
        return "published"

    def _prepare(self, publication_id: str) -> tuple[str, PublishRequest, int] | None:
        """Policy re-check, then commit ``publishing`` and the started attempt row.

        Everything here is committed *before* the platform call, so an in-flight attempt
        is always visible. Returns None when the row is no longer this worker's to
        publish (lease lost, cancelled, already published).
        """
        with self._db.transaction() as session:
            row = pubs.lock_publication(session, publication_id)
            if row.claimed_by != self.id or PublicationStatus(row.status) not in pubs.CLAIMABLE:
                return None
            if row.attempt_count >= self._max_attempts:
                pubs.set_outcome(
                    session,
                    publication_id,
                    status=PublicationStatus.FAILED,
                    failure_category=PublishFailureCategory.NOT_SENT,
                    failure_message=f"no attempt left after {row.attempt_count} tries",
                )
                return None
            self._check_policy(session, row)
            attempt_row = pubs.begin_attempt(
                session, publication_id, worker_id=self.id, provider=self._publisher.provider_name
            )
            if attempt_row is None:
                return None
            account_id = str(repo.get_run_row(session, row.run_id).account.get("id", ""))
            request = PublishRequest(
                account_id=account_id,
                candidate_id=row.candidate_id,
                content=row.content,
                idempotency_key=row.idempotency_key,
            )
            return attempt_row.id, request, attempt_row.attempt

    def _check_policy(self, session: Session, row: PublicationRow) -> None:
        run_row = repo.get_run_row(session, row.run_id)
        candidate = session.get(ContentCandidateRow, row.candidate_id)
        if candidate is None or candidate.run_id != row.run_id:
            raise NotPublishableError(f"candidate {row.candidate_id} no longer belongs to the run")
        refusals = [
            r
            for r in publish_refusals(pubs.publish_facts(session, run_row, candidate, row.platform))
            if not r.startswith("a publication already exists")
        ]
        if content_sha256(candidate.content) != row.content_sha256:
            refusals.append("the candidate's content changed since the intent was created")
        if content_sha256(row.content) != row.content_sha256:
            refusals.append("the stored publication content does not match its hash")
        if refusals:
            raise NotPublishableError("; ".join(refusals))

    def _handle_error(
        self,
        publication_id: str,
        attempt_id: str,
        attempt: int,
        exc: PublishError,
        started: float,
    ) -> str:
        latency_ms = exc.latency_ms or (time.perf_counter() - started) * 1000
        reset_at = getattr(exc, "rate_limit_reset_at", None)
        retry_not_before = None
        if isinstance(exc, PublishNotSentError):
            outcome, bucket = AttemptOutcome.NOT_SENT, "requeued"
            # Nothing reached the platform: retry, but not on the very next poll.
            if self._backoff is not None:
                retry_not_before = self._backoff.not_before(attempt, utc_now())
        elif isinstance(exc, PublishRejectedError):
            outcome, bucket = AttemptOutcome.REJECTED, "failed"
            # A rate limit with a known reset is the one rejection retried on its own:
            # the row goes back to ``ready`` but is not claimable before the reset.
            # Nothing sleeps; the normal poll simply skips it until then. A rate limit
            # without a reset time stays ``failed`` for a manual retry.
            if exc.failure_category is PublishFailureCategory.RATE_LIMITED and reset_at:
                bucket, retry_not_before = "requeued", reset_at
        else:
            outcome, bucket = AttemptOutcome.UNKNOWN, "unknown"
        exhausted = bucket == "requeued" and attempt >= self._max_attempts
        if exhausted:
            bucket, retry_not_before = "failed", None
        status = {
            "requeued": PublicationStatus.READY,
            "failed": PublicationStatus.FAILED,
            "unknown": PublicationStatus.UNKNOWN,
        }[bucket]
        with self._db.transaction() as session:
            pubs.finish_attempt(
                session,
                attempt_id,
                outcome=outcome,
                latency_ms=latency_ms,
                http_status=exc.http_status,
                failure_category=exc.failure_category,
                failure_message=str(exc),
                rate_limit_reset_at=reset_at,
            )
            pubs.set_outcome(
                session,
                publication_id,
                status=status,
                failure_category=exc.failure_category,
                failure_message=str(exc)
                + (
                    f" (no attempt left after {attempt} of {self._max_attempts})"
                    if exhausted
                    else ""
                ),
                rate_limit_reset_at=reset_at,
                retry_not_before=retry_not_before,
            )
        _log.warning(
            "publish attempt did not succeed",
            extra={
                "publication_id": publication_id,
                "attempt": attempt,
                "category": exc.failure_category.value,
                "status": status.value,
            },
        )
        return bucket

    def _record_failure(
        self,
        publication_id: str,
        *,
        attempt_id: str | None,
        category: PublishFailureCategory,
        message: str,
    ) -> None:
        with self._db.transaction() as session:
            if attempt_id is not None:
                pubs.finish_attempt(
                    session,
                    attempt_id,
                    outcome=AttemptOutcome.REJECTED,
                    latency_ms=0.0,
                    failure_category=category,
                    failure_message=message,
                )
            pubs.set_outcome(
                session,
                publication_id,
                status=PublicationStatus.FAILED,
                failure_category=category,
                failure_message=message,
            )

    # --- loop -----------------------------------------------------------------------

    def run_forever(self, *, poll_seconds: float, stop: threading.Event | None = None) -> None:
        """Poll until ``stop`` is set (or SIGINT/SIGTERM arrives in the main thread)."""
        stop = stop or threading.Event()
        _log.info("publisher worker started", extra={"worker": self.id, "poll": poll_seconds})
        while not stop.is_set():
            try:
                report = self.run_once()
            except DatabaseUnavailableError:
                _log.warning("database unavailable; retrying next cycle")
            except Exception:  # a cycle must never kill the worker
                _log.exception("publisher cycle failed")
            else:
                if report.claimed or report.swept:
                    _log.info(
                        "cycle finished",
                        extra={
                            "claimed": len(report.claimed),
                            "published": len(report.published),
                            "failed": len(report.failed),
                            "unknown": len(report.unknown),
                        },
                    )
            stop.wait(poll_seconds)
        _log.info("publisher worker stopped", extra={"worker": self.id})


class EmbeddedWorker:
    """Runs a ``PublisherWorker`` on a thread inside another process (the API).

    Off by default (``PUBLISHER_EMBEDDED_WORKER=false``): starting or restarting the API
    should not by itself trigger queued external side effects. The standalone
    ``sga-publisher-worker`` is the canonical publishing process.
    """

    def __init__(self, worker: PublisherWorker, *, poll_seconds: float) -> None:
        self._worker = worker
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._worker.run_forever,
            kwargs={"poll_seconds": self._poll_seconds, "stop": self._stop},
            name="publisher",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None


def install_signal_handlers(stop: threading.Event) -> None:
    """Graceful shutdown: finish the current cycle, then exit."""

    def handle(signum: int, _frame: FrameType | None) -> None:
        _log.info("shutdown signal received", extra={"signal": signum})
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle)
