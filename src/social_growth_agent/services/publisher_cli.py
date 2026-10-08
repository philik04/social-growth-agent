"""``sga-publisher-worker``: the canonical process that publishes approved content.

    DATABASE_URL=postgresql://... uv run sga-publisher-worker          # poll forever
    uv run sga-publisher-worker --once                                 # one cycle
    uv run sga-publisher-worker --dry-run                              # show due work only

It publishes only publications a human already requested (``POST /runs/{id}/publish``).
``PUBLISHER_PROVIDER=mock`` (the default) posts nothing anywhere; ``x`` needs the four
X_PUBLISH_* user-context credentials and will create real posts. Credentials are read
from the environment and never printed.
"""

import argparse
import sys
import threading

from social_growth_agent.config import AppSettings
from social_growth_agent.errors import ConfigurationError, SocialGrowthError
from social_growth_agent.observability import configure_logging, get_logger
from social_growth_agent.persistence import Database
from social_growth_agent.persistence.db import redacted
from social_growth_agent.services.publications import PublicationService
from social_growth_agent.services.publisher import (
    PublisherWorker,
    install_signal_handlers,
)
from social_growth_agent.services.runtime import build_publisher_worker

_log = get_logger("publisher")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sga-publisher-worker", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report due publications without publishing."
    )
    parser.add_argument("--url", help="Database URL (defaults to DATABASE_URL).")
    args = parser.parse_args(argv)

    configure_logging()
    settings = AppSettings()
    url = args.url or (settings.database_url.get_secret_value() if settings.database_url else None)
    if url is None:
        print("DATABASE_URL is not set (or pass --url).", file=sys.stderr)
        return 2

    db = Database.connect(url)
    try:
        if args.dry_run:
            due = PublicationService(db).due_count()
            print(f"{redacted(url)}: {due} publication(s) due; nothing was published")
            return 0
        try:
            worker = build_publisher_worker(settings, db)
        except ConfigurationError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(
            f"{redacted(url)}: publisher '{settings.publisher_provider}' as {worker.id} "
            f"(lease {settings.publisher_lease_seconds}s, "
            f"max {settings.publish_max_attempts} attempts)"
        )
        return _run(worker, settings, once=args.once)
    finally:
        db.dispose()


def _run(worker: PublisherWorker, settings: AppSettings, *, once: bool) -> int:
    if once:
        try:
            report = worker.run_once()
        except SocialGrowthError as exc:
            print(f"cycle failed: {exc}", file=sys.stderr)
            return 1
        print(
            f"claimed {len(report.claimed)}, published {len(report.published)}, "
            f"failed {len(report.failed)}, unknown {len(report.unknown)}, "
            f"requeued {len(report.requeued)}, swept {len(report.swept)}"
        )
        return 0
    stop = threading.Event()
    install_signal_handlers(stop)
    worker.run_forever(poll_seconds=settings.publisher_poll_seconds, stop=stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
