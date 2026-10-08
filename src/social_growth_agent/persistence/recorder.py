"""Projects graph state into the domain tables.

The checkpoint is the source of truth for execution; these tables are the queryable
record. Projection is idempotent: records are immutable and keyed by their ids, so the
full state can be written again after every step (or re-synced from a checkpoint after
a crash) without duplicates. Rows are written in foreign-key order inside one
transaction, so a provenance violation (evidence citing a post the run never
retrieved) fails loudly instead of being stored.
"""

from sqlalchemy import update
from sqlalchemy.orm import Session

from social_growth_agent.graph import GraphState
from social_growth_agent.models import RunStatus
from social_growth_agent.persistence.db import Database
from social_growth_agent.persistence.rows import (
    brief_row,
    candidate_rows,
    critique_row,
    decision_row,
    error_row,
    event_row,
    finding_rows,
    insert_ignore,
    source_post_row,
)
from social_growth_agent.persistence.tables import (
    CandidateFindingRow,
    ContentCandidateRow,
    CritiqueRow,
    FindingEvidenceRow,
    ResearchBriefRow,
    ResearchFindingRow,
    ReviewDecisionRow,
    RunErrorRow,
    RunEventRow,
    RunRow,
    SourcePostRow,
)
from social_growth_agent.persistence.usage import record_usage


class RunRecorder:
    def __init__(self, db: Database) -> None:
        self._db = db

    def project(self, state: GraphState, *, status: RunStatus | None = None) -> None:
        """Write ``state`` to the tables. ``status`` overrides the graph's status (e.g.
        RUNNING while a resumed run is still executing)."""
        with self._db.transaction() as session:
            project_state(session, state, status=status)


def project_state(session: Session, state: GraphState, *, status: RunStatus | None = None) -> None:
    run_id = state.run_id
    failure = state.errors[-1] if state.status is RunStatus.FAILED and state.errors else None
    session.execute(
        update(RunRow)
        .where(RunRow.id == run_id)
        .values(
            status=(status or state.status).value,
            current_node=state.events[-1].node if state.events else None,
            research_attempts=state.research_attempts,
            generation_attempts=state.generation_attempts,
            regeneration_rounds=state.regeneration_rounds,
            edit_rounds=state.review.edit_rounds,
            review_candidate_ids=(
                state.review.candidate_ids if state.status is RunStatus.AWAITING_REVIEW else []
            ),
            failure_node=failure.node if failure else None,
            failure_type=failure.error_type if failure else None,
            failure_message=failure.message if failure else None,
        )
    )
    record_usage(session, run_id, state.research_fetches, state.llm_calls)
    insert_ignore(session, SourcePostRow, [source_post_row(run_id, p) for p in state.source_posts])

    brief = state.research_brief
    if brief is not None:
        insert_ignore(session, ResearchBriefRow, [brief_row(run_id, brief)])
    findings, evidence = finding_rows(run_id, state.research, brief.id if brief else None)
    insert_ignore(session, ResearchFindingRow, findings)
    insert_ignore(session, FindingEvidenceRow, evidence)

    candidates, links = candidate_rows(run_id, state.candidates)
    insert_ignore(session, ContentCandidateRow, candidates)
    insert_ignore(session, CandidateFindingRow, links)
    insert_ignore(
        session, CritiqueRow, [critique_row(run_id, i, k) for i, k in enumerate(state.critiques)]
    )
    insert_ignore(
        session, ReviewDecisionRow, [decision_row(run_id, d) for d in state.review_decisions]
    )
    insert_ignore(session, RunEventRow, [event_row(run_id, e) for e in state.events])
    insert_ignore(session, RunErrorRow, [error_row(run_id, e) for e in state.errors])
