"""Opt-in live demo: read the public metrics of ONE post this application published.

    RUN_LIVE_X_ANALYTICS_TESTS=1 X_BEARER_TOKEN=... DATABASE_URL=postgresql://... \\
      uv run python -m social_growth_agent.services.x_analytics_demo <post-id> [--collect-due]

Read-only on X: one ``GET /2/tweets?ids=<post-id>`` with the app-only bearer token
(about one post read). It never posts, edits or deletes anything, and the publishing
credentials are never loaded. It refuses unless:

1. ``RUN_LIVE_X_ANALYTICS_TESTS=1``;
2. ``X_BEARER_TOKEN`` and ``DATABASE_URL`` are set, and the database is at the current
   schema revision (this demo never migrates);
3. ``<post-id>`` is the platform id of a publication this application recorded as
   ``published`` (it will not read arbitrary posts).

Without ``--collect-due`` the read is a probe: it is recorded on the analytics request
ledger like every other read, prints what X returned (and which metrics it did not
return), and stores no snapshot and changes no job. With ``--collect-due`` the read goes
through the normal worker path instead, for that publication's *due* jobs only; if
none is due, nothing is read.
"""

import argparse
import os
import sys
import time

from sqlalchemy import select

from social_growth_agent.accounting.pricing import load_price_list
from social_growth_agent.config import AppSettings
from social_growth_agent.errors import AnalyticsError, SocialGrowthError
from social_growth_agent.models import AnalyticsFetch, PublicationStatus, utc_now
from social_growth_agent.persistence import Database
from social_growth_agent.persistence import analytics as adb
from social_growth_agent.persistence.migrate import current_revision
from social_growth_agent.persistence.tables import PublicationRow
from social_growth_agent.providers.x import XAnalyticsProvider
from social_growth_agent.services.analytics import AnalyticsService
from social_growth_agent.services.runtime import build_analytics_worker

GATE_ENV = "RUN_LIVE_X_ANALYTICS_TESTS"
HEAD_REVISION = "0004"
_COUNTS = ("likes", "reposts", "replies", "quotes", "bookmarks", "impressions")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="x_analytics_demo", description=__doc__.split("\n")[0])
    parser.add_argument("post_id", help="the X post id of a publication recorded here")
    parser.add_argument(
        "--collect-due",
        action="store_true",
        help="collect this publication's due snapshot jobs through the worker path",
    )
    args = parser.parse_args(argv)

    if os.environ.get(GATE_ENV) != "1":
        print(f"{GATE_ENV} is not 1: refusing to call the X API", file=sys.stderr)
        return 2
    if not args.post_id.isdigit():
        print("the post id must be the numeric X post id", file=sys.stderr)
        return 2
    settings = AppSettings()
    if settings.x_bearer_token is None:
        print("Set X_BEARER_TOKEN (the app-only token; no other credential is used).")
        return 2
    if settings.database_url is None:
        print("Set DATABASE_URL to the database that recorded the publication.")
        return 2
    url = settings.database_url.get_secret_value()
    revision = current_revision(url)
    if revision != HEAD_REVISION:
        print(f"the database is at revision {revision}; run `sga-db upgrade` first")
        return 2

    db = Database.connect(settings.database_url)
    provider = XAnalyticsProvider.from_token(
        settings.x_bearer_token, timeout_seconds=settings.x_timeout_seconds
    )
    try:
        return _run(settings, db, provider, args.post_id, collect_due=args.collect_due)
    except SocialGrowthError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    finally:
        provider.close()
        db.dispose()


def _run(
    settings: AppSettings,
    db: Database,
    provider: XAnalyticsProvider,
    post_id: str,
    *,
    collect_due: bool,
) -> int:
    with db.transaction() as session:
        row = session.scalars(
            select(PublicationRow)
            .where(PublicationRow.platform == "x")
            .where(PublicationRow.provider_post_id == post_id)
            .where(PublicationRow.status == PublicationStatus.PUBLISHED.value)
        ).first()
        if row is None:
            print(f"no published publication with X post id {post_id}: refusing to read it")
            return 2
        publication_id, recorded = row.id, row.published_at
        print(f"publication {publication_id} (run {row.run_id}), recorded published at {recorded}")
        for job in adb.list_jobs(session, publication_id=publication_id):
            print(
                f"  job {job.snapshot_age:>3}: {job.status.value} due {job.scheduled_for}"
                f" ({job.schedule_basis})"
            )

    if collect_due:
        worker = build_analytics_worker(settings, db, provider=provider)
        report = worker.run_once(publication_id=publication_id)
        print(
            f"\nworker: claimed={len(report.claimed)} collected={len(report.collected)}"
            f" retrying={len(report.retrying)} failed={len(report.failed)}"
            f" requests={report.requests}"
        )
        if not report.claimed:
            print("no job of this publication is due: nothing was read")
        for snapshot in AnalyticsService(db).metrics(publication_id):
            counts = {name: getattr(snapshot, name) for name in _COUNTS}
            print(
                f"  {snapshot.snapshot_age:>3}: captured {snapshot.captured_at}"
                f" delay={snapshot.capture_delay_seconds:.0f}s"
                f" on_target={snapshot.on_target} {counts}"
            )
        return 0 if not report.failed else 1

    # A probe: recorded on the request ledger, no job, no snapshot.
    with db.transaction() as session:
        request_id = adb.start_request(
            session,
            worker_id="x-analytics-demo",
            provider=provider.provider_name,
            metrics_scope=provider.metrics_scope,
            post_ids=[post_id],
            now=utc_now(),
        )
    started = time.perf_counter()
    try:
        fetch = provider.fetch_post_metrics([post_id])
    except AnalyticsError as exc:
        with db.transaction() as session:
            adb.finish_request(
                session,
                request_id,
                now=utc_now(),
                error_category=exc.failure_category,
                error_message=str(exc),
                http_status=exc.http_status,
                latency_ms=exc.latency_ms,
                rate_limit_reset_at=exc.rate_limit_reset_at,
            )
        print(f"\nX refused the read ({exc.failure_category.value}): {exc}")
        return 1
    with db.transaction() as session:
        adb.finish_request(session, request_id, now=utc_now(), fetch=fetch)
    _print_probe(fetch, recorded, (time.perf_counter() - started) * 1000)
    prices = load_price_list(settings.pricing_file)
    if prices.x.post_read is None:
        print(
            f"\nestimated cost: {fetch.posts_returned} post read(s); x.post_read is not set in"
            f" {settings.pricing_file}, so no amount is shown"
        )
    else:
        cost = prices.x.post_read * fetch.posts_returned
        print(
            f"\nestimated cost: {fetch.posts_returned} post read(s) x {prices.x.post_read}"
            f" {prices.currency} = {cost} (upper bound; X may deduplicate same-day reads)"
        )
    print(f"ledger: request {request_id} (analytics_requests)")
    return 0


def _print_probe(fetch: AnalyticsFetch, recorded: object, latency_ms: float) -> None:
    print(
        f"\nX answered in {latency_ms:.0f} ms; rate limit remaining: {fetch.rate_limit_remaining}"
    )
    for record in fetch.records:
        print(f"post {record.provider_post_id}: scope={fetch.metrics_scope}")
        print(f"  created_at (X):          {record.provider_created_at}")
        print(f"  published_at (recorded): {recorded}")
        for name in _COUNTS:
            value = getattr(record, name)
            print(f"  {name:<12} {'not returned (NULL)' if value is None else value}")
    for miss in fetch.misses:
        print(f"post {miss.provider_post_id}: {miss.category.value} - {miss.detail}")


if __name__ == "__main__":
    sys.exit(main())
