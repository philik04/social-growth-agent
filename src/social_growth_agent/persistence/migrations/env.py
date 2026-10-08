"""Alembic environment. The URL comes from the caller (``sga-db``) or DATABASE_URL."""

from alembic import context
from sqlalchemy import create_engine, pool

from social_growth_agent.config import AppSettings
from social_growth_agent.errors import ConfigurationError
from social_growth_agent.persistence.db import sqlalchemy_url
from social_growth_agent.persistence.tables import Base

target_metadata = Base.metadata


def _url() -> str:
    url = context.config.attributes.get("database_url")
    if isinstance(url, str):
        return sqlalchemy_url(url)
    configured = AppSettings().database_url
    if configured is None:
        raise ConfigurationError("DATABASE_URL is not set")
    return sqlalchemy_url(configured.get_secret_value())


def _include_object(obj: object, name: str | None, type_: str, *_: object) -> bool:
    # LangGraph owns its checkpoint tables (created by PostgresSaver.setup()).
    return not (type_ == "table" and name is not None and name.startswith("checkpoint"))


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool, hide_parameters=True)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
