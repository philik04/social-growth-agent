"""Performance evaluations (Phase 7): load the inputs, store the evidence package, read it.

Read-only against everything Phases 1-6 wrote. The only writes are the two append-only
evaluation tables.
"""

from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from social_growth_agent.accounting import PriceList
from social_growth_agent.analytics.evaluation import PostInput, SnapshotInput
from social_growth_agent.errors import EvaluationNotFoundError
from social_growth_agent.models import PublicationStatus
from social_growth_agent.models.strategy import EvaluationStatus, EvaluationSummary, Observation
from social_growth_agent.persistence.strategies import evaluation_usage, strategy_sha256
from social_growth_agent.persistence.tables import (
    ContentCandidateRow,
    EvaluationObservationRow,
    PerformanceEvaluationRow,
    PostMetricRow,
    PublicationRow,
    RunRow,
    StrategyProposalRow,
)
from social_growth_agent.persistence.views import EvaluationView


def load_posts(
    session: Session,
    *,
    account_id: str,
    snapshot_age: str,
    cutoff: datetime,
    since: datetime | None,
    until: datetime | None,
    strategy_id: str | None,
) -> list[PostInput]:
    """Every published post of the account in the window (recorded publication time, at
    or before the cutoff), with its snapshot at ``snapshot_age`` if one exists. The cutoff
    on ``captured_at`` is applied by the evaluation itself."""
    query = (
        select(PublicationRow, RunRow, ContentCandidateRow, PostMetricRow)
        .join(RunRow, RunRow.id == PublicationRow.run_id)
        .join(ContentCandidateRow, ContentCandidateRow.id == PublicationRow.candidate_id)
        .outerjoin(
            PostMetricRow,
            and_(
                PostMetricRow.publication_id == PublicationRow.id,
                PostMetricRow.snapshot_age == snapshot_age,
            ),
        )
        .where(PublicationRow.status == PublicationStatus.PUBLISHED.value)
        .where(PublicationRow.published_at.is_not(None))
        .where(PublicationRow.published_at <= cutoff)
        .where(RunRow.account["id"].as_string() == account_id)
        .order_by(PublicationRow.id)
    )
    if since is not None:
        query = query.where(PublicationRow.published_at >= since)
    if until is not None:
        query = query.where(PublicationRow.published_at < until)
    if strategy_id is not None:
        query = query.where(RunRow.strategy_id == strategy_id)
    digests: dict[str, str] = {}
    posts = []
    for pub, run, cand, metric in session.execute(query):
        if run.id not in digests:
            digests[run.id] = strategy_sha256(run.strategy)
        assert pub.published_at is not None
        posts.append(
            PostInput(
                publication_id=pub.id,
                run_id=run.id,
                candidate_id=cand.id,
                content=pub.content,
                published_at=pub.published_at,
                hook_type=cand.hook_type,
                origin=cand.origin,
                format=cand.format,
                strategy_id=run.strategy_id,
                strategy_version=run.strategy_version,
                strategy_digest=digests[run.id],
                snapshot=None if metric is None else snapshot_input(metric),
            )
        )
    return posts


def snapshot_input(m: PostMetricRow) -> SnapshotInput:
    return SnapshotInput(
        post_metric_id=m.id,
        provider=m.provider,
        captured_at=m.captured_at,
        provider_created_at=m.provider_created_at,
        likes=m.likes,
        replies=m.replies,
        reposts=m.reposts,
        quotes=m.quotes,
        bookmarks=m.bookmarks,
        impressions=m.impressions,
    )


def find_by_input_digest(session: Session, input_digest: str) -> PerformanceEvaluationRow | None:
    return session.scalars(
        select(PerformanceEvaluationRow).where(
            PerformanceEvaluationRow.input_digest == input_digest
        )
    ).first()


def insert(
    session: Session, row: PerformanceEvaluationRow, observations: list[Observation]
) -> None:
    session.add(row)
    session.flush()
    session.add_all(
        EvaluationObservationRow(evaluation_id=row.id, **o.model_dump(mode="json"))
        for o in observations
    )


def get(session: Session, evaluation_id: str) -> PerformanceEvaluationRow:
    row = session.get(PerformanceEvaluationRow, evaluation_id)
    if row is None:
        raise EvaluationNotFoundError(f"evaluation not found: {evaluation_id}")
    return row


def list_rows(
    session: Session, *, account_id: str | None, limit: int
) -> list[PerformanceEvaluationRow]:
    query = select(PerformanceEvaluationRow).order_by(PerformanceEvaluationRow.created_at.desc())
    if account_id is not None:
        query = query.where(PerformanceEvaluationRow.account_id == account_id)
    return list(session.scalars(query.limit(limit)))


def observations(
    session: Session,
    evaluation_id: str,
    *,
    eligible: bool | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[Observation]:
    query = (
        select(EvaluationObservationRow)
        .where(EvaluationObservationRow.evaluation_id == evaluation_id)
        .order_by(EvaluationObservationRow.publication_id)
        .offset(offset)
    )
    if eligible is not None:
        query = query.where(EvaluationObservationRow.eligible == eligible)
    if limit is not None:
        query = query.limit(limit)
    return [
        Observation.model_validate(
            {c: getattr(r, c) for c in Observation.model_fields}, from_attributes=False
        )
        for r in session.scalars(query)
    ]


def view(session: Session, row: PerformanceEvaluationRow, *, prices: PriceList) -> EvaluationView:
    proposal_ids = list(
        session.scalars(
            select(StrategyProposalRow.id)
            .where(StrategyProposalRow.evaluation_id == row.id)
            .order_by(StrategyProposalRow.created_at)
        )
    )
    return EvaluationView(
        id=row.id,
        account_id=row.account_id,
        strategy_id=row.strategy_id,
        snapshot_age=row.snapshot_age,
        published_since=row.published_since,
        published_until=row.published_until,
        data_cutoff=row.data_cutoff,
        synthetic=row.synthetic,
        config=row.config,
        config_version=row.config_version,
        input_digest=row.input_digest,
        digest=row.digest,
        status=EvaluationStatus(row.status),
        eligible_count=row.eligible_count,
        excluded_count=row.excluded_count,
        posts_considered=row.posts_considered,
        snapshots_used=row.snapshots_used,
        summary=EvaluationSummary.model_validate(row.summary),
        created_by=row.created_by,
        created_at=row.created_at,
        proposal_ids=proposal_ids,
        usage=evaluation_usage(session, row.id, prices),
    )
