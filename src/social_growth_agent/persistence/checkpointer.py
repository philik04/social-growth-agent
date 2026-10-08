"""LangGraph's Postgres checkpointer: the source of truth for where a run resumes.

Checkpoints use the same allowlisted serializer as the in-memory saver, so only our
own domain types can be deserialized from the database.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from social_growth_agent.graph.checkpointing import checkpoint_serializer

CHECKPOINT_TABLES = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
)

_CONNECTION_KWARGS = {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}


def open_pool(libpq_url: str, *, max_size: int = 5) -> ConnectionPool[Connection[DictRow]]:
    pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
        libpq_url,
        min_size=1,
        max_size=max_size,
        kwargs=_CONNECTION_KWARGS,
        open=True,
    )
    return pool


def postgres_checkpointer(pool: ConnectionPool[Connection[DictRow]]) -> PostgresSaver:
    return PostgresSaver(pool, serde=checkpoint_serializer())


@contextmanager
def _connection(libpq_url: str) -> Iterator[Connection[DictRow]]:
    with Connection.connect(
        libpq_url, autocommit=True, prepare_threshold=0, row_factory=dict_row
    ) as conn:
        yield conn


def setup_checkpoint_tables(libpq_url: str) -> None:
    """Create or migrate LangGraph's checkpoint tables (idempotent)."""
    with _connection(libpq_url) as conn:
        PostgresSaver(conn, serde=checkpoint_serializer()).setup()
