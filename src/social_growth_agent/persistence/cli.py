"""``sga-db``: database schema commands. Reads DATABASE_URL (or ``--url``).

    uv run sga-db upgrade     # Alembic migrations + LangGraph checkpoint tables
    uv run sga-db current     # show the applied Alembic revision and tables
    uv run sga-db reset --yes # drop and recreate every table (localhost databases only)

Prints host/port/database only, never credentials.
"""

import argparse
import sys

from sqlalchemy import create_engine, make_url, pool, text

from social_growth_agent.config import AppSettings
from social_growth_agent.errors import ConfigurationError
from social_growth_agent.persistence.checkpointer import CHECKPOINT_TABLES
from social_growth_agent.persistence.db import redacted, sqlalchemy_url
from social_growth_agent.persistence.migrate import current_revision, table_names, upgrade
from social_growth_agent.persistence.tables import APP_TABLES

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sga-db", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["upgrade", "current", "reset"])
    parser.add_argument("--url", help="Database URL (defaults to DATABASE_URL).")
    parser.add_argument("--yes", action="store_true", help="Confirm 'reset'.")
    args = parser.parse_args(argv)

    url = args.url or _configured_url()
    if url is None:
        print("DATABASE_URL is not set (or pass --url).", file=sys.stderr)
        return 2
    where = redacted(url)

    if args.command == "reset":
        if make_url(sqlalchemy_url(url)).host not in _LOCAL_HOSTS:
            print(f"refusing to reset non-local database {where}", file=sys.stderr)
            return 2
        if not args.yes:
            print("reset drops every table; pass --yes to confirm", file=sys.stderr)
            return 2
        _drop_all(url)
        print(f"dropped all tables in {where}")
        upgrade(url)
        print(f"recreated schema in {where}")
    elif args.command == "upgrade":
        upgrade(url)
        print(f"{where}: upgraded to {current_revision(url)}; checkpoint tables ready")
    else:
        print(f"{where}: revision {current_revision(url)}")
        print("tables: " + ", ".join(table_names(url)))
    return 0


def _configured_url() -> str | None:
    try:
        configured = AppSettings().database_url
    except ConfigurationError:
        return None
    return None if configured is None else configured.get_secret_value()


def _drop_all(url: str) -> None:
    engine = create_engine(sqlalchemy_url(url), poolclass=pool.NullPool)
    tables = [*APP_TABLES, *CHECKPOINT_TABLES, "alembic_version"]
    try:
        with engine.begin() as conn:
            for name in tables:
                conn.execute(text(f'DROP TABLE IF EXISTS "{name}" CASCADE'))
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
