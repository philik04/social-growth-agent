"""Schema management: Alembic for the domain tables, LangGraph setup() for checkpoints."""

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, pool

from social_growth_agent.persistence.checkpointer import setup_checkpoint_tables
from social_growth_agent.persistence.db import libpq_url, sqlalchemy_url

SCRIPT_LOCATION = "social_growth_agent.persistence:migrations"


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    config.attributes["database_url"] = database_url
    return config


def upgrade(database_url: str) -> None:
    """Bring a database fully up to date (idempotent): domain tables, then checkpoints."""
    command.upgrade(alembic_config(database_url), "head")
    setup_checkpoint_tables(libpq_url(database_url))


def current_revision(database_url: str) -> str | None:
    from alembic.runtime.migration import MigrationContext

    engine = create_engine(sqlalchemy_url(database_url), poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def table_names(database_url: str) -> list[str]:
    engine = create_engine(sqlalchemy_url(database_url), poolclass=pool.NullPool)
    try:
        return sorted(inspect(engine).get_table_names())
    finally:
        engine.dispose()
