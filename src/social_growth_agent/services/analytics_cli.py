"""``sga-analytics-worker``: collects due metric snapshots of published posts.

    DATABASE_URL=postgresql://... uv run sga-analytics-worker           # poll forever
    uv run sga-analytics-worker --once                                  # one cycle
    uv run sga-analytics-worker --dry-run                               # show due jobs only
    uv run sga-analytics-worker backfill --publication pub_...          # explicit backfill
    uv run sga-analytics-worker backfill --all-published [--dry-run]

Starting the worker never schedules anything: it only collects jobs that publishing (or
an explicit ``backfill``) created. ``ANALYTICS_PROVIDER=mock`` (the default) reads
nothing anywhere; ``x`` reads public metrics with the app-only X_BEARER_TOKEN, which
costs post reads. Credentials are read from the environment and never printed.
"""

import argparse
import sys
import threading

from social_growth_agent.config import AppSettings
from social_growth_agent.errors import ConfigurationError, SocialGrowthError
from social_growth_agent.observability import configure_logging, get_logger
from social_growth_agent.persistence import Database
from social_growth_agent.persistence.db import redacted
from social_growth_agent.services.analytics import AnalyticsService
from social_growth_agent.services.analytics_worker import AnalyticsWorker
from social_growth_agent.services.publisher import install_signal_handlers
from social_growth_agent.services.runtime import analytics_schedule, build_analytics_worker

_log = get_logger("analytics")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sga-analytics-worker", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report due jobs (or backfill) without acting."
    )
    parser.add_argument("--url", help="Database URL (defaults to DATABASE_URL).")
    sub = parser.add_subparsers(dest="command")
    backfill = sub.add_parser("backfill", help="Create missing snapshot jobs, explicitly.")
    target = backfill.add_mutually_exclusive_group(required=True)
    target.add_argument("--publication", help="One published publication id.")
    target.add_argument("--all-published", action="store_true", help="Every published post.")
    backfill.add_argument("--dry-run", action="store_true", dest="backfill_dry_run")
    args = parser.parse_args(argv)

    configure_logging()
    settings = AppSettings()
    url = args.url or (settings.database_url.get_secret_value() if settings.database_url else None)
    if url is None:
        print("DATABASE_URL is not set (or pass --url).", file=sys.stderr)
        return 2

    db = Database.connect(url)
    try:
        service = AnalyticsService(db, schedule=analytics_schedule(settings))
        if args.command == "backfill":
            return _backfill(service, args.publication, dry_run=args.backfill_dry_run)
        if args.dry_run:
            print(f"{redacted(url)}: {service.due_count()} analytics job(s) due; nothing was read")
            return 0
        try:
            worker = build_analytics_worker(settings, db)
        except ConfigurationError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(
            f"{redacted(url)}: analytics '{settings.analytics_provider}' as {worker.id} "
            f"(lease {settings.analytics_lease_seconds}s, "
            f"max {settings.analytics_max_attempts} attempts, "
            f"ages {settings.analytics_snapshot_ages})"
        )
        return _run(worker, settings, once=args.once)
    finally:
        db.dispose()


def _backfill(service: AnalyticsService, publication_id: str | None, *, dry_run: bool) -> int:
    try:
        results = (
            [service.backfill(publication_id, dry_run=dry_run)]
            if publication_id
            else service.backfill_all_published(dry_run=dry_run)
        )
    except SocialGrowthError as exc:
        print(f"backfill refused: {exc}", file=sys.stderr)
        return 1
    verb = "would create" if dry_run else "created"
    for r in results:
        print(
            f"{r.publication_id}: {verb} {len(r.created_job_ids)}, "
            f"already had {len(r.existing_job_ids)} (basis: "
            f"{r.schedule_basis.value if r.schedule_basis else 'n/a'})"
        )
    print(f"{len(results)} publication(s); nothing was read from the platform")
    return 0


def _run(worker: AnalyticsWorker, settings: AppSettings, *, once: bool) -> int:
    if once:
        try:
            report = worker.run_once()
        except SocialGrowthError as exc:
            print(f"cycle failed: {exc}", file=sys.stderr)
            return 1
        print(
            f"claimed {len(report.claimed)}, collected {len(report.collected)}, "
            f"retrying {len(report.retrying)}, failed {len(report.failed)}, "
            f"cancelled {len(report.cancelled)}, swept {len(report.swept)}, "
            f"requests {report.requests}"
        )
        return 0
    stop = threading.Event()
    install_signal_handlers(stop)
    worker.run_forever(poll_seconds=settings.analytics_poll_seconds, stop=stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
