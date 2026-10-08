"""Database connection handling.

The URL is held as ``SecretStr`` (it may contain a password) and is only revealed when
an engine or connection pool is created. It never appears in reprs, logs or errors.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from social_growth_agent.errors import ConfigurationError, DatabaseUnavailableError


def sqlalchemy_url(url: str) -> str:
    """``postgresql://`` (psycopg/libpq form) -> ``postgresql+psycopg://`` (SQLAlchemy form)."""
    for prefix in ("postgresql+psycopg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url.removeprefix(prefix)
    raise ConfigurationError("DATABASE_URL must be a postgresql:// URL")


def libpq_url(url: str) -> str:
    """The plain form psycopg (and LangGraph's PostgresSaver) expects."""
    return "postgresql://" + sqlalchemy_url(url).removeprefix("postgresql+psycopg://")


def redacted(url: str) -> str:
    """Host, port and database only, for messages: no user name or password."""
    parsed = make_url(sqlalchemy_url(url))
    return f"{parsed.host or 'localhost'}:{parsed.port or 5432}/{parsed.database or ''}"


@dataclass(frozen=True)
class Database:
    engine: Engine
    sessions: sessionmaker[Session]
    url: SecretStr

    @classmethod
    def connect(cls, url: SecretStr | str, *, pool_size: int = 5) -> "Database":
        secret = url if isinstance(url, SecretStr) else SecretStr(url)
        engine = create_engine(
            sqlalchemy_url(secret.get_secret_value()),
            pool_size=pool_size,
            pool_pre_ping=True,
            hide_parameters=True,
        )
        return cls(engine=engine, sessions=sessionmaker(engine, expire_on_commit=False), url=secret)

    @property
    def libpq_url(self) -> str:
        return libpq_url(self.url.get_secret_value())

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """A session in a transaction: committed on success, rolled back on error.

        Connection-level failures become ``DatabaseUnavailableError`` (HTTP 503).
        """
        try:
            with self.sessions.begin() as session:
                yield session
        except SQLAlchemyError as exc:
            if _is_connection_error(exc):
                raise DatabaseUnavailableError("database unavailable") from None
            raise

    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return False
        return True

    def dispose(self) -> None:
        self.engine.dispose()


def _is_connection_error(exc: SQLAlchemyError) -> bool:
    from sqlalchemy.exc import DBAPIError, OperationalError

    return isinstance(exc, OperationalError) or (
        isinstance(exc, DBAPIError) and exc.connection_invalidated
    )
