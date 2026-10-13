"""``sga-strategy``: explicit strategy registry maintenance.

    DATABASE_URL=postgresql://... uv run sga-strategy import-runs --dry-run   # report only
    uv run sga-strategy import-runs                                           # register

``import-runs`` registers the distinct strategy bodies recorded in past runs as versions
(origin ``imported_from_run``). It never activates anything, never starts a run and
never calls a model or a platform. An ``id@version`` that past runs used with more than
one body, or that is already registered with a different body, is a **conflict**: it is
listed and left unregistered, never resolved by picking one. Conflicts make the exit
code 1 so scripts notice them; resolve them by registering the intended body as a new
version through ``POST /strategies/versions``.

Exit codes: 0 no conflicts, 1 conflicts reported, 2 configuration error.
"""

import argparse
import sys

from social_growth_agent.accounting import load_price_list
from social_growth_agent.config import AppSettings
from social_growth_agent.errors import ConfigurationError
from social_growth_agent.persistence import Database
from social_growth_agent.persistence.db import redacted
from social_growth_agent.persistence.migrate import current_revision, head_revision
from social_growth_agent.services.strategies import ImportReport, StrategyService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sga-strategy", description=__doc__.splitlines()[0])
    parser.add_argument("--url", help="Database URL (defaults to DATABASE_URL).")
    sub = parser.add_subparsers(dest="command", required=True)
    imports = sub.add_parser("import-runs", help="Register strategies recorded in past runs.")
    imports.add_argument(
        "--dry-run", action="store_true", help="Report what would be registered; write nothing."
    )
    args = parser.parse_args(argv)

    try:
        settings = AppSettings()
    except (ConfigurationError, ValueError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    url = args.url or (settings.database_url.get_secret_value() if settings.database_url else None)
    if url is None:
        print("DATABASE_URL is not set (or pass --url).", file=sys.stderr)
        return 2
    revision = current_revision(url)
    if revision != head_revision():
        print(
            f"{redacted(url)} is at revision {revision}; run `sga-db upgrade` first",
            file=sys.stderr,
        )
        return 2

    db = Database.connect(url)
    try:
        service = StrategyService(
            db,
            prices=load_price_list(settings.pricing_file),
            agent=None,
            proposer=None,
            stale_seconds=2 * settings.llm_timeout_seconds,
        )
        report = service.import_from_runs(dry_run=args.dry_run)
    finally:
        db.dispose()
    _print(report, dry_run=args.dry_run)
    return 1 if report.conflicts else 0


def _print(report: ImportReport, *, dry_run: bool) -> None:
    verb = "would register" if dry_run else "registered"
    for label in report.registered:
        print(f"{verb}: {label}")
    for label in report.already_registered:
        print(f"already registered: {label}")
    for conflict in report.conflicts:
        print(f"CONFLICT (not registered): {conflict}")
    print(
        f"{len(report.registered)} {verb}, {len(report.already_registered)} already registered, "
        f"{len(report.conflicts)} conflicts" + (" (dry run: nothing written)" if dry_run else "")
    )
    if report.conflicts:
        print(
            "Conflicts are never resolved automatically. Register the intended body as a new "
            "version via POST /strategies/versions."
        )


if __name__ == "__main__":
    sys.exit(main())
