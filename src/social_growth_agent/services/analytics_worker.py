"""Analytics worker: collects due metric snapshots of published posts.

One cycle (``run_once``):

1. sweep ``collecting`` jobs whose lease expired back to ``scheduled`` (after the
   backoff) or, out of attempts, to ``failed``;
2. claim due jobs with ``FOR UPDATE SKIP LOCKED`` and a lease (two workers never
   collect the same job);
3. commit ``collecting`` plus a started request row and one attempt row per job;
4. call the analytics provider once for the batch (the only external call);
5. in one transaction: finish the request, store a snapshot per returned post
   (``ON CONFLICT DO NOTHING``), apply the retry policy to every other job.

Unlike publishing, a metrics read has no external side effect. A worker that dies
between (3) and (5) leaves the jobs ``collecting``; the sweep returns them to
``scheduled`` and the read is simply repeated. The unique constraints make a second
snapshot impossible. Nothing sleeps: a retry waits in ``retry_not_before``.

This worker never creates jobs: publishing (or an explicit backfill) does. Starting it
can only collect what was already scheduled.
"""

import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime

from social_growth_agent.errors import AnalyticsError, DatabaseUnavailableError
from social_growth_agent.models import (
    AnalyticsFailureCategory,
    AnalyticsFetch,
    AnalyticsJobStatus,
    PostMetricMiss,
    utc_now,
)
from social_growth_agent.observability import get_logger
from social_growth_agent.persistence import Database
from social_growth_agent.persistence import analytics as adb
from social_growth_agent.policies.analytics import RetryDecision, decide_after_failure
from social_growth_agent.policies.retry import Backoff
from social_growth_agent.providers import SocialAnalyticsProvider
from social_growth_agent.services.publisher import worker_id

_log = get_logger("analytics")


@dataclass(frozen=True)
class AnalyticsCycleReport:
    """What one cycle did. Returned so demos and tests can assert on it."""

    swept: tuple[str, ...] = ()
    claimed: tuple[str, ...] = ()
    collected: tuple[str, ...] = ()
    retrying: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    cancelled: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    rescheduled: tuple[str, ...] = ()
    """Jobs moved onto a newly verified creation time (reconciliation)."""
    requests: int = 0
    request_id: str | None = None
    retry_not_before: dict[str, datetime] = field(default_factory=dict)


class AnalyticsWorker:
    def __init__(
        self,
        *,
        db: Database,
        provider: SocialAnalyticsProvider,
        lease_seconds: int = 60,
        max_attempts: int = 5,
        batch_size: int = 50,
        backoff: Backoff | None = None,
        name: str | None = None,
    ) -> None:
        self._db = db
        self._provider = provider
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._batch_size = max(1, min(batch_size, provider.max_batch_size))
        self._backoff = backoff or Backoff()
        self.id = name or worker_id()
        self._now = utc_now()
        """Cycle start: claims, leases and backoff are computed from it."""
        self._at = self._now
        """After the provider call: when snapshots are captured and requests finished."""

    def decide(
        self, category: AnalyticsFailureCategory, attempts_made: int, reset_at: datetime | None
    ) -> RetryDecision:
        return decide_after_failure(
            category,
            attempt=attempts_made,
            max_attempts=self._max_attempts,
            now=self._now,
            backoff=self._backoff,
            rate_limit_reset_at=reset_at,
        )

    # --- one cycle ------------------------------------------------------------------

    def run_once(
        self, *, now: datetime | None = None, publication_id: str | None = None
    ) -> AnalyticsCycleReport:
        """``now`` pins the clock (tests and demos step time without sleeping);
        ``publication_id`` collects only that post's due jobs (the live demo)."""
        self._now = now or utc_now()
        with self._db.transaction() as session:
            swept = adb.sweep_expired_collecting(session, now=self._now, decide=self.decide)
        if swept:
            _log.warning("analytics jobs left in flight by a dead worker", extra={"ids": swept})
        with self._db.transaction() as session:
            claimed = adb.claim_due(
                session,
                worker_id=self.id,
                lease_seconds=self._lease_seconds,
                limit=self._batch_size,
                now=self._now,
                publication_id=publication_id,
            )
        if not claimed:
            return AnalyticsCycleReport(swept=tuple(swept))
        with self._db.transaction() as session:
            begun = adb.begin_collection(
                session,
                claimed,
                worker_id=self.id,
                provider=self._provider.provider_name,
                metrics_scope=self._provider.metrics_scope,
                now=self._now,
            )
        if begun is None:
            return AnalyticsCycleReport(swept=tuple(swept), claimed=tuple(claimed))

        started = time.perf_counter()
        try:
            fetch = self._provider.fetch_post_metrics(begun.post_ids)
        except AnalyticsError as exc:
            self._at = now or utc_now()
            outcome = self._request_failed(begun, exc)
        except Exception as exc:  # a bug in the provider: reads are safe to repeat
            _log.exception("analytics provider raised an unexpected error")
            unexpected = AnalyticsError(
                f"the analytics provider failed unexpectedly ({type(exc).__name__})",
                failure_category=AnalyticsFailureCategory.UNKNOWN,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            self._at = now or utc_now()
            outcome = self._request_failed(begun, unexpected)
        else:
            self._at = now or utc_now()
            outcome = self._request_succeeded(begun, fetch)
        return replace(
            outcome,
            swept=tuple(swept),
            claimed=tuple(claimed),
            requests=1,
            request_id=begun.request_id,
        )

    def _request_succeeded(self, begun: adb.Begun, fetch: AnalyticsFetch) -> AnalyticsCycleReport:
        at = self._at
        records = {r.provider_post_id: r for r in fetch.records}
        misses = {m.provider_post_id: m for m in fetch.misses}
        buckets: dict[str, list[str]] = {
            "collected": [],
            "retrying": [],
            "failed": [],
            "cancelled": [],
            "skipped": [],
            "rescheduled": [],
        }
        waits: dict[str, datetime] = {}
        with self._db.transaction() as session:
            adb.finish_request(session, begun.request_id, now=at, fetch=fetch)
            for item in begun.items:
                record = records.get(item.provider_post_id)
                if record is not None:
                    stored = adb.record_snapshot(
                        session,
                        item,
                        record,
                        request_id=begun.request_id,
                        platform=self._provider.platform,
                        provider=self._provider.provider_name,
                        metrics_scope=fetch.metrics_scope,
                        worker_id=self.id,
                        now=at,
                    )
                    buckets["collected" if stored else "skipped"].append(item.job_id)
                    if record.provider_created_at is not None:
                        buckets["rescheduled"] += adb.reconcile_with_creation_time(
                            session, item.publication_id, record.provider_created_at, now=at
                        )
                    continue
                miss = misses.get(item.provider_post_id) or PostMetricMiss(
                    provider_post_id=item.provider_post_id,
                    category=AnalyticsFailureCategory.MALFORMED_RESPONSE,
                    detail="the provider answered without this post",
                )
                decision = self.decide(miss.category, item.attempt, None)
                applied = adb.record_job_failure(
                    session,
                    item,
                    category=miss.category,
                    message=miss.detail,
                    decision=decision,
                    reset_at=None,
                    worker_id=self.id,
                    now=at,
                )
                if not applied:
                    buckets["skipped"].append(item.job_id)
                    continue
                self._bucket(buckets, waits, item.job_id, decision)
                if miss.category is AnalyticsFailureCategory.NOT_FOUND:
                    buckets["cancelled"] += adb.cancel_waiting_jobs(
                        session,
                        item.publication_id,
                        reason=f"an earlier snapshot found the post unavailable: {miss.detail}",
                    )
        _log.info(
            "analytics request finished",
            extra={
                "request_id": begun.request_id,
                "posts_returned": fetch.posts_returned,
                "collected": len(buckets["collected"]),
                "failed": len(buckets["failed"]),
            },
        )
        return _report(buckets, waits)

    def _request_failed(self, begun: adb.Begun, exc: AnalyticsError) -> AnalyticsCycleReport:
        at = self._at
        buckets: dict[str, list[str]] = {"retrying": [], "failed": [], "skipped": []}
        waits: dict[str, datetime] = {}
        with self._db.transaction() as session:
            adb.finish_request(
                session,
                begun.request_id,
                now=at,
                error_category=exc.failure_category,
                error_message=str(exc),
                http_status=exc.http_status,
                latency_ms=exc.latency_ms,
                rate_limit_reset_at=exc.rate_limit_reset_at,
            )
            for item in begun.items:
                decision = self.decide(exc.failure_category, item.attempt, exc.rate_limit_reset_at)
                if adb.record_job_failure(
                    session,
                    item,
                    category=exc.failure_category,
                    message=str(exc),
                    decision=decision,
                    reset_at=exc.rate_limit_reset_at,
                    worker_id=self.id,
                    now=at,
                ):
                    self._bucket(buckets, waits, item.job_id, decision)
                else:
                    buckets["skipped"].append(item.job_id)
        _log.warning(
            "analytics request failed",
            extra={
                "request_id": begun.request_id,
                "category": exc.failure_category.value,
                "jobs": len(begun.items),
            },
        )
        return _report(buckets, waits)

    @staticmethod
    def _bucket(
        buckets: dict[str, list[str]],
        waits: dict[str, datetime],
        job_id: str,
        decision: RetryDecision,
    ) -> None:
        if decision.status is AnalyticsJobStatus.SCHEDULED:
            buckets["retrying"].append(job_id)
            if decision.retry_not_before is not None:
                waits[job_id] = decision.retry_not_before
        else:
            buckets["failed"].append(job_id)

    # --- loop -----------------------------------------------------------------------

    def run_forever(self, *, poll_seconds: float, stop: threading.Event | None = None) -> None:
        """Poll until ``stop`` is set (or SIGINT/SIGTERM arrives in the main thread)."""
        stop = stop or threading.Event()
        _log.info("analytics worker started", extra={"worker": self.id, "poll": poll_seconds})
        while not stop.is_set():
            try:
                report = self.run_once()
            except DatabaseUnavailableError:
                _log.warning("database unavailable; retrying next cycle")
            except Exception:  # a cycle must never kill the worker
                _log.exception("analytics cycle failed")
            else:
                if report.claimed or report.swept:
                    _log.info(
                        "analytics cycle finished",
                        extra={
                            "claimed": len(report.claimed),
                            "collected": len(report.collected),
                            "retrying": len(report.retrying),
                            "failed": len(report.failed),
                        },
                    )
            stop.wait(poll_seconds)
        _log.info("analytics worker stopped", extra={"worker": self.id})


def _report(buckets: dict[str, list[str]], waits: dict[str, datetime]) -> AnalyticsCycleReport:
    return AnalyticsCycleReport(
        collected=tuple(buckets.get("collected", ())),
        retrying=tuple(buckets.get("retrying", ())),
        failed=tuple(buckets.get("failed", ())),
        cancelled=tuple(buckets.get("cancelled", ())),
        skipped=tuple(buckets.get("skipped", ())),
        rescheduled=tuple(buckets.get("rescheduled", ())),
        retry_not_before=waits,
    )
