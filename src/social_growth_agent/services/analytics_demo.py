"""Phase 6 analytics demo: scheduled snapshots, retries, a deleted post and a restart.

    DATABASE_URL=postgresql://... uv run python -m social_growth_agent.services.analytics_demo

Offline and free: mock research fixtures, the fake agents, the mock publisher and the
mock analytics provider (its numbers are synthetic and labelled ``mock_analytics``).
Nothing is read from or posted to any platform. The worker's clock is stepped with
``run_once(now=...)``, so nothing sleeps. The demo shows, in order:

1. publishing two posts, which creates their 1h/24h/72h jobs in the same transaction;
2. a worker started before anything is due: it reads nothing and creates nothing;
3. a rate limit at 1h: both jobs wait for the reset (no sleep), then are collected;
   one post is gone (not found), so its later jobs are cancelled; the other reports its
   creation time, which moves its waiting jobs onto it (reconciliation);
4. a timeout at 24h: exponential backoff in ``retry_not_before``, then collected late
   and labelled with its real delay;
5. a worker that dies mid-collection at 72h: its request stays visible as unfinished,
   the lease expires, another worker repeats the read, and one snapshot is stored;
6. the snapshots with their timing, and the usage counts.

Use a scratch database: the demo runs ``sga-db upgrade`` on it.
"""

import sys
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import select

from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.config import AppSettings
from social_growth_agent.errors import AnalyticsError
from social_growth_agent.graph import Dependencies
from social_growth_agent.models import (
    AnalyticsFailureCategory,
    ReviewAction,
    ReviewDecision,
)
from social_growth_agent.persistence import Database
from social_growth_agent.persistence import analytics as adb
from social_growth_agent.persistence.migrate import upgrade
from social_growth_agent.persistence.tables import AnalyticsRequestRow, PublicationRow
from social_growth_agent.policies.retry import Backoff
from social_growth_agent.providers.mocks import (
    MockAnalyticsProvider,
    MockPublisher,
    MockResearchProvider,
)
from social_growth_agent.services.analytics_worker import AnalyticsCycleReport, AnalyticsWorker
from social_growth_agent.services.demo import demo_account
from social_growth_agent.services.runs import InlineExecutor
from social_growth_agent.services.runtime import Runtime, build_runtime

LEASE_SECONDS = 60


class DieDuringCollection(BaseException):
    """Stands in for the analytics worker process dying while the read is in flight."""


def build(settings: AppSettings, publisher: MockPublisher) -> Runtime:
    deps = Dependencies(llm=build_fake_llm(), research_provider=MockResearchProvider())
    return build_runtime(settings, deps=deps, executor=InlineExecutor(), publisher=publisher)


def publish_one(runtime: Runtime) -> str:
    account, strategy = demo_account()
    run = runtime.service.create_run(account, strategy)
    [pending] = [p for p in runtime.service.pending_reviews() if p.run_id == run.id]
    candidate = pending.candidates[0]
    runtime.service.submit_review(
        run.id,
        ReviewDecision(
            action=ReviewAction.APPROVE, candidate_id=candidate.id, reviewer="analytics-demo"
        ),
    )
    publication = runtime.publications.request(run.id, candidate.id, requested_by="demo")
    assert runtime.worker is not None
    assert runtime.worker.run_once().published == (publication.id,)
    return publication.id


def worker(db: Database, provider: MockAnalyticsProvider, name: str) -> AnalyticsWorker:
    return AnalyticsWorker(
        db=db,
        provider=provider,
        lease_seconds=LEASE_SECONDS,
        max_attempts=5,
        batch_size=50,
        backoff=Backoff(base_seconds=60, max_seconds=3600),
        name=name,
    )


def clock(at: datetime, origin: datetime) -> str:
    seconds = int((at - origin).total_seconds())
    sign = "-" if seconds < 0 else "+"
    hours, rest = divmod(abs(seconds), 3600)
    return f"T{sign}{hours}h{rest // 60:02d}m{rest % 60:02d}s"


def trace(label: str, report: AnalyticsCycleReport, at: datetime, origin: datetime) -> None:
    parts = [
        f"{name}={len(value)}"
        for name, value in (
            ("swept", report.swept),
            ("claimed", report.claimed),
            ("collected", report.collected),
            ("retrying", report.retrying),
            ("failed", report.failed),
            ("cancelled", report.cancelled),
            ("rescheduled", report.rescheduled),
            ("skipped", report.skipped),
        )
        if value
    ]
    print(f"    [{clock(at, origin)}] {label}: requests={report.requests} {' '.join(parts)}")
    for job_id, wait in report.retry_not_before.items():
        print(f"      retry {job_id} not before {clock(wait, origin)}")


def show_jobs(db: Database, publication_ids: Sequence[str], origin: datetime) -> None:
    with db.transaction() as session:
        for publication_id in publication_ids:
            for job in adb.list_jobs(session, publication_id=publication_id):
                moved = (
                    f" (was {clock(job.original_scheduled_for, origin)})"
                    if job.original_scheduled_for
                    else ""
                )
                failure = f" {job.failure_category}" if job.failure_category else ""
                print(
                    f"    {publication_id} {job.snapshot_age:>3}: {job.status.value:<10}"
                    f" due {clock(job.scheduled_for, origin)}{moved}"
                    f" basis={job.schedule_basis} attempts={job.attempt_count}{failure}"
                )


def main() -> int:
    settings = AppSettings()
    if settings.database_url is None:
        print("Set DATABASE_URL to a scratch PostgreSQL database (see docs/DATABASE.md).")
        return 2
    upgrade(settings.database_url.get_secret_value())
    runtime = build(settings, MockPublisher())
    db = runtime.db

    print("== 1. publishing creates the snapshot jobs (no platform read)")
    kept, deleted = publish_one(runtime), publish_one(runtime)
    with db.transaction() as session:
        rows = {
            r.id: r
            for r in session.scalars(
                select(PublicationRow).where(PublicationRow.id.in_([kept, deleted]))
            )
        }
        origin = rows[kept].published_at
        kept_post, gone_post = rows[kept].provider_post_id, rows[deleted].provider_post_id
    assert origin is not None and kept_post is not None and gone_post is not None
    # The platform's own creation time: a few seconds before the app recorded it.
    created = origin - timedelta(seconds=4)
    print(f"    T+0 is {origin.isoformat()} (the recorded publication time of {kept})")
    show_jobs(db, [kept, deleted], origin)

    t1 = origin + timedelta(hours=1, seconds=5)
    reset = t1 + timedelta(minutes=10)
    timeout_at = created + timedelta(hours=24)
    provider = MockAnalyticsProvider(
        [
            AnalyticsError(
                "X API rate limit or usage cap reached (HTTP 429)",
                failure_category=AnalyticsFailureCategory.RATE_LIMITED,
                http_status=429,
                rate_limit_reset_at=reset,
            ),
            None,  # after the reset: the normal answer
            AnalyticsError(
                "X API request timed out", failure_category=AnalyticsFailureCategory.TIMEOUT
            ),
            None,
            _die,
            None,
        ],
        created_at={kept_post: created},
        deleted=[gone_post],
    )
    first = worker(db, provider, "analytics-1")

    print("\n== 2. a worker that starts before anything is due reads nothing")
    trace("startup cycle", first.run_once(now=origin + timedelta(minutes=5)), origin, origin)
    print(f"    provider calls: {len(provider.calls)}")

    print("\n== 3. 1h: a rate limit, then the reset; one post is gone")
    trace("cycle", first.run_once(now=t1), t1, origin)
    almost = reset - timedelta(seconds=1)
    trace("before the reset", first.run_once(now=almost), almost, origin)
    trace("at the reset", first.run_once(now=reset), reset, origin)
    print(f"    {kept} reports created_at = T-4s; its waiting jobs move onto it:")
    show_jobs(db, [kept, deleted], origin)

    print("\n== 4. 24h: a timeout, an exponential backoff, a late but labelled capture")
    trace("cycle", first.run_once(now=timeout_at), timeout_at, origin)
    backed_off = timeout_at + timedelta(seconds=60)
    trace("after the backoff", first.run_once(now=backed_off), backed_off, origin)

    print("\n== 5. 72h: the worker dies mid-collection")
    t72 = created + timedelta(hours=72)
    try:
        first.run_once(now=t72)
    except DieDuringCollection as exc:
        print(f"    worker died: {exc}")
    with db.transaction() as session:
        unfinished = session.scalars(
            select(AnalyticsRequestRow).where(AnalyticsRequestRow.finished_at.is_(None))
        ).all()
        print(f"    unfinished requests on the ledger: {[r.id for r in unfinished]}")
    show_jobs(db, [kept], origin)
    second = worker(db, provider, "analytics-2")
    held = t72 + timedelta(seconds=30)
    trace("restart, lease held", second.run_once(now=held), held, origin)
    expired = t72 + timedelta(seconds=LEASE_SECONDS + 1)
    trace("lease expired", second.run_once(now=expired), expired, origin)
    retried = expired + timedelta(seconds=60)
    trace("after the backoff", second.run_once(now=retried), retried, origin)

    print("\n== 6. snapshots (timing is what makes late captures honest)")
    snapshots = runtime.analytics.metrics(kept)
    for s in snapshots:
        rate = s.derived.observed_engagement_rate_by_impressions
        print(
            f"    {s.snapshot_age:>3}: captured {clock(s.captured_at, origin)}"
            f" delay={s.capture_delay_seconds:.0f}s actual_age={s.actual_age_seconds}s"
            f" ({s.age_basis}) on_target={s.on_target}"
            f" likes={s.likes} impressions={s.impressions}"
            f" engagement/impression={'n/a' if rate is None else f'{rate:.4f}'}"
        )
    show_jobs(db, [kept, deleted], origin)
    with db.transaction() as session:
        run_id = rows[kept].run_id
        counts = adb.analytics_operation_counts(session, run_id)
    for provider_name, requests, reads, stored, failed, open_ in counts:
        print(
            f"    usage for {run_id}: provider={provider_name} requests={requests}"
            f" post_reads={reads} snapshots={stored} failed={failed} unfinished={open_}"
        )
    print(f"    provider calls in total: {len(provider.calls)} (mock: unbilled)")
    assert len(snapshots) == 3
    runtime.close()
    return 0


def _die(post_ids: Sequence[str]) -> None:
    raise DieDuringCollection("the analytics worker process died mid-read")


if __name__ == "__main__":
    sys.exit(main())
