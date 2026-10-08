"""Usage ledgers: provider calls written to the database as they happen.

This is the ``UsageSink`` the graph calls after every node attempt, including ones
that fail, so usage survives a crash between a provider call and the next checkpoint.
Rows are keyed by the record's id and inserted with ``ON CONFLICT DO NOTHING``, so the
recorder can write the same records again from graph state without double counting.
"""

from collections.abc import Sequence

from sqlalchemy import update
from sqlalchemy.orm import Session

from social_growth_agent.models import LLMCall, ResearchFetch, new_id, utc_now
from social_growth_agent.persistence.db import Database
from social_growth_agent.persistence.rows import insert_ignore, llm_call_row, research_fetch_row
from social_growth_agent.persistence.tables import (
    LLMCallRow,
    ProviderOperationRow,
    ResearchFetchRow,
)


class UsageLedger:
    """Implements ``providers.UsageSink``. Errors propagate to the graph's instrumentation,
    which logs them and carries on (the checkpoint still holds the usage)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self, run_id: str, fetches: Sequence[ResearchFetch], llm_calls: Sequence[LLMCall]
    ) -> None:
        with self._db.transaction() as session:
            record_usage(session, run_id, fetches, llm_calls)


class OperationRecorder:
    """Implements ``providers.OperationLedger`` on ``provider_operations``.

    ``started`` commits its own transaction before the node calls a provider, so the row
    survives a process crash mid-call. Nothing about usage or cost is inferred here.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def started(
        self, run_id: str, *, node: str, provider: str, operation: str, generation_attempt: int
    ) -> str:
        op_id = new_id("op")
        with self._db.transaction() as session:
            session.add(
                ProviderOperationRow(
                    id=op_id,
                    run_id=run_id,
                    node=node,
                    provider=provider,
                    operation=operation,
                    generation_attempt=generation_attempt,
                    started_at=utc_now(),
                    outcome="started",
                )
            )
        return op_id

    def finished(
        self,
        operation_id: str,
        *,
        outcome: str,
        error_type: str | None,
        latency_ms: float,
        usage_records: int,
    ) -> None:
        with self._db.transaction() as session:
            session.execute(
                update(ProviderOperationRow)
                .where(ProviderOperationRow.id == operation_id)
                .values(
                    finished_at=utc_now(),
                    outcome=outcome,
                    error_type=error_type,
                    latency_ms=latency_ms,
                    usage_records=usage_records,
                )
            )


def record_usage(
    session: Session, run_id: str, fetches: Sequence[ResearchFetch], llm_calls: Sequence[LLMCall]
) -> None:
    insert_ignore(session, ResearchFetchRow, [research_fetch_row(run_id, f) for f in fetches])
    insert_ignore(session, LLMCallRow, [llm_call_row(run_id, c) for c in llm_calls])
