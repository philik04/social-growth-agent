"""Usage ledger: provider calls written to the database as they happen.

This is the ``UsageSink`` the graph calls after every node attempt, including ones
that fail, so usage survives a crash between a provider call and the next checkpoint.
Rows are keyed by the record's id and inserted with ``ON CONFLICT DO NOTHING``, so the
recorder can write the same records again from graph state without double counting.
"""

from collections.abc import Sequence

from sqlalchemy.orm import Session

from social_growth_agent.models import LLMCall, ResearchFetch
from social_growth_agent.persistence.db import Database
from social_growth_agent.persistence.rows import insert_ignore, llm_call_row, research_fetch_row
from social_growth_agent.persistence.tables import LLMCallRow, ResearchFetchRow


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


def record_usage(
    session: Session, run_id: str, fetches: Sequence[ResearchFetch], llm_calls: Sequence[LLMCall]
) -> None:
    insert_ignore(session, ResearchFetchRow, [research_fetch_row(run_id, f) for f in fetches])
    insert_ignore(session, LLMCallRow, [llm_call_row(run_id, c) for c in llm_calls])
