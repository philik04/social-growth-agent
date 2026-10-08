"""Phase 5 publishing demo: approval, a durable intent, a worker, and every ambiguity.

    DATABASE_URL=postgresql://... uv run python -m social_growth_agent.services.publish_demo

Offline and free: mock research fixtures, the deterministic fake agents and the mock
publisher. Nothing is posted to any platform. The demo shows, in order:

1. a run approved by a human, with no publication and no platform call;
2. an explicit publish request creating one durable intent;
3. the worker publishing it exactly once, with its started/finished attempt;
4. a duplicate request refused (409) and a second worker cycle making no call;
5. a scheduled publication ignored until it is due;
6. a worker that dies mid-call leaving an ``unknown`` publication, never retried, until
   a human resolves it.

Use a scratch database: the demo runs ``sga-db upgrade`` on it.
"""

import sys
import time
from datetime import timedelta

from sqlalchemy import text

from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.config import AppSettings
from social_growth_agent.errors import PublicationExistsError
from social_growth_agent.graph import Dependencies
from social_growth_agent.models import (
    PublicationStatus,
    PublishFailureCategory,
    ReviewAction,
    ReviewDecision,
    utc_now,
)
from social_growth_agent.persistence import Database
from social_growth_agent.persistence import publications as pubs
from social_growth_agent.persistence.migrate import upgrade
from social_growth_agent.providers.mocks import MockPublisher, MockResearchProvider
from social_growth_agent.services.demo import demo_account
from social_growth_agent.services.publications import PublicationService
from social_growth_agent.services.publisher import PublisherWorker
from social_growth_agent.services.runs import InlineExecutor
from social_growth_agent.services.runtime import Runtime, build_runtime


class DieDuringPublish(BaseException):
    """Stands in for the worker process dying while the platform call is in flight."""


def build(settings: AppSettings, publisher: MockPublisher) -> Runtime:
    deps = Dependencies(llm=build_fake_llm(), research_provider=MockResearchProvider())
    return build_runtime(settings, deps=deps, executor=InlineExecutor(), publisher=publisher)


def approved_run(runtime: Runtime) -> tuple[str, str]:
    account, strategy = demo_account()
    run = runtime.service.create_run(account, strategy)
    [pending] = [p for p in runtime.service.pending_reviews() if p.run_id == run.id]
    candidate = pending.candidates[0]
    summary = runtime.service.submit_review(
        run.id,
        ReviewDecision(
            action=ReviewAction.APPROVE, candidate_id=candidate.id, reviewer="publish-demo"
        ),
    )
    print(f"    run {run.id} is {summary.status}: {candidate.content}")
    return run.id, candidate.id


def show(db: Database, publication_id: str) -> None:
    with db.transaction() as session:
        view = pubs.publication_view(session, pubs.get_publication_row(session, publication_id))
    attempts = ", ".join(
        f"#{a.attempt} {a.outcome}{'' if a.finished_at else ' (unfinished)'}" for a in view.attempts
    )
    print(
        f"    publication {view.id}: status={view.status.value} attempts={view.attempt_count}"
        f" post_id={view.provider_post_id} [{attempts or 'no attempts yet'}]"
    )
    if view.failure_category:
        print(f"      failure: {view.failure_category} - {view.failure_message}")


def main() -> int:
    settings = AppSettings()
    if settings.database_url is None:
        print("Set DATABASE_URL to a scratch PostgreSQL database (see docs/DATABASE.md).")
        return 2
    upgrade(settings.database_url.get_secret_value())

    publisher = MockPublisher()
    runtime = build(settings, publisher)
    service = PublicationService(runtime.db)
    worker = PublisherWorker(
        db=runtime.db, publisher=publisher, lease_seconds=60, max_attempts=3, name="demo-worker"
    )

    print("== 1. a human approves content (no publication, no platform call)")
    run_id, candidate_id = approved_run(runtime)
    print(f"    publisher calls so far: {len(publisher.calls)}")

    print("\n== 2. an explicit publish request creates one durable intent")
    publication = service.request(run_id, candidate_id, requested_by="publish-demo")
    show(runtime.db, publication.id)
    print(f"    publisher calls so far: {len(publisher.calls)}")

    print("\n== 3. the worker publishes it exactly once")
    report = worker.run_once()
    print(f"    claimed={len(report.claimed)} published={len(report.published)}")
    show(runtime.db, publication.id)
    print(f"    publisher calls: {len(publisher.calls)}")

    print("\n== 4. a duplicate request is refused and no further call is made")
    try:
        service.request(run_id, candidate_id)
    except PublicationExistsError as exc:
        print(f"    refused (409): {exc} -> publication {exc.publication_id}")
    again = worker.run_once()
    print(f"    second cycle: claimed={len(again.claimed)} calls={len(publisher.calls)}")

    print("\n== 5. a scheduled publication waits until it is due")
    later_run, later_candidate = approved_run(runtime)
    scheduled = service.request(
        later_run, later_candidate, scheduled_for=utc_now() + timedelta(hours=2)
    )
    show(runtime.db, scheduled.id)
    print(f"    cycle while not due: claimed={len(worker.run_once().claimed)}")
    with runtime.db.transaction() as session:
        # Only the demo moves the clock; nothing in the application rewrites a schedule.
        session.execute(
            text(
                "UPDATE publications SET scheduled_for = now() - interval '1 minute' WHERE id = :id"
            ),
            {"id": scheduled.id},
        )
    print(f"    once due: published={len(worker.run_once().published)}")
    show(runtime.db, scheduled.id)

    print("\n== 6. a worker that dies mid-call leaves an ambiguous publication")
    crash_run, crash_candidate = approved_run(runtime)
    crashing = MockPublisher([DieDuringPublish("the worker process died mid-call")])
    crash_worker = PublisherWorker(
        db=runtime.db, publisher=crashing, lease_seconds=1, max_attempts=3, name="dying-worker"
    )
    ambiguous = service.request(crash_run, crash_candidate)
    try:
        crash_worker.run_once()
    except DieDuringPublish as exc:
        print(f"    worker died: {exc}")
    show(runtime.db, ambiguous.id)

    print("    another worker finds the expired lease: unknown, never retried")
    recovery = PublisherWorker(
        db=runtime.db, publisher=publisher, lease_seconds=60, max_attempts=3, name="demo-worker-2"
    )
    time.sleep(1.2)  # let the dead worker's one-second lease expire
    swept = recovery.run_once()
    print(f"    swept={len(swept.swept)} claimed={len(swept.claimed)}")
    show(runtime.db, ambiguous.id)
    calls_before = len(publisher.calls)
    for _ in range(3):
        recovery.run_once()
    print(f"    cycles later: platform calls unchanged = {len(publisher.calls) == calls_before}")

    print("\n    a human establishes that nothing was posted and resolves it")
    service.resolve(ambiguous.id, published=False, resolved_by="publish-demo", note="not found")
    print(f"    published after the human decision: {len(recovery.run_once().published)}")
    show(runtime.db, ambiguous.id)

    with runtime.db.transaction() as session:
        states = {p.status: p.id for p in pubs.list_publications(session, limit=50)}
    print(f"\n    final states: {sorted(s.value for s in states)}")
    assert PublicationStatus.PUBLISHED in states
    assert PublishFailureCategory.LEASE_EXPIRED  # the category the sweep recorded
    runtime.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
