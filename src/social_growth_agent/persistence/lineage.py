"""Read-only joins from a publication (and its metrics) back to what produced it.

No lineage is copied into the analytics tables: the foreign keys already carry it
(``post_metrics -> publications -> content_candidates -> critiques / candidate_findings
-> research_findings -> finding_evidence -> source_posts``, and the run's strategy
snapshot). These helpers assemble the chain for the API and for Phase 7.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from social_growth_agent.errors import PublicationNotFoundError
from social_growth_agent.models import PublicationStatus, ReviewAction
from social_growth_agent.persistence import analytics as adb
from social_growth_agent.persistence import publications as pubs
from social_growth_agent.persistence import repository as repo
from social_growth_agent.persistence.tables import (
    AnalyticsJobRow,
    ContentCandidateRow,
    PublicationRow,
    ReviewDecisionRow,
    RunRow,
)
from social_growth_agent.persistence.views import (
    AnalyticsSummary,
    DecisionView,
    LineageView,
    PublishedPostView,
)


def publication_lineage(session: Session, publication_id: str) -> LineageView:
    row = session.get(PublicationRow, publication_id)
    if row is None:
        raise PublicationNotFoundError(publication_id)
    run = repo.get_run_row(session, row.run_id)
    [candidate] = repo.candidate_views(session, row.run_id, [row.candidate_id])
    research = repo.research_view(session, row.run_id)
    finding_ids = set(candidate.research_finding_ids)
    findings = [f for f in (research.findings if research else []) if f.id in finding_ids]
    cited = {source for f in findings for source in f.evidence_source_ids}
    posts = [p for p in repo.source_post_views(session, row.run_id) if p.source_id in cited]
    approval = session.scalars(
        select(ReviewDecisionRow)
        .where(ReviewDecisionRow.run_id == row.run_id)
        .where(ReviewDecisionRow.action == ReviewAction.APPROVE.value)
        .where(ReviewDecisionRow.candidate_id == row.candidate_id)
        .order_by(ReviewDecisionRow.decided_at.desc())
    ).first()
    return LineageView(
        publication=pubs.publication_view(session, row),
        candidate=candidate,
        revision_chain=_revision_chain(session, row.candidate_id),
        approval=DecisionView.model_validate(approval, from_attributes=True) if approval else None,
        findings=findings,
        source_posts=posts,
        research_query=research.query if research else None,
        research_provider=research.provider if research else None,
        strategy_id=run.strategy_id,
        strategy_version=run.strategy_version,
        strategy=run.strategy,
        account=run.account,
        metrics=adb.snapshots(session, publication_id),
    )


def _revision_chain(session: Session, candidate_id: str) -> list[str]:
    chain: list[str] = []
    current: str | None = candidate_id
    while current is not None and current not in chain:
        chain.append(current)
        row = session.get(ContentCandidateRow, current)
        current = row.revises_candidate_id if row is not None else None
    return chain


def published_posts(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    account_id: str | None = None,
    job_status: str | None = None,
    limit: int = 50,
) -> list[PublishedPostView]:
    """Published posts, newest first, each with its latest observation. ``since`` and
    ``until`` filter on the recorded publication time; ``job_status`` keeps posts with
    at least one analytics job in that status."""
    query = (
        select(PublicationRow, RunRow, ContentCandidateRow)
        .join(RunRow, RunRow.id == PublicationRow.run_id)
        .join(ContentCandidateRow, ContentCandidateRow.id == PublicationRow.candidate_id)
        .where(PublicationRow.status == PublicationStatus.PUBLISHED.value)
        .order_by(PublicationRow.published_at.desc(), PublicationRow.id)
        .limit(limit)
    )
    if since is not None:
        query = query.where(PublicationRow.published_at >= since)
    if until is not None:
        query = query.where(PublicationRow.published_at < until)
    if account_id is not None:
        query = query.where(RunRow.account["id"].as_string() == account_id)
    if job_status is not None:
        query = query.where(
            select(AnalyticsJobRow.id)
            .where(AnalyticsJobRow.publication_id == PublicationRow.id)
            .where(AnalyticsJobRow.status == job_status)
            .exists()
        )
    views = []
    for publication, run, candidate in session.execute(query):
        summary = adb.analytics_summary(session, publication.id) or AnalyticsSummary(
            jobs_by_status={},
            snapshot_count=0,
            provider_created_at=publication.provider_created_at,
            latest=None,
        )
        account = run.account.get("id")
        views.append(
            PublishedPostView(
                publication_id=publication.id,
                run_id=publication.run_id,
                candidate_id=publication.candidate_id,
                account_id=str(account) if account is not None else None,
                platform=publication.platform,
                provider_post_id=publication.provider_post_id,
                provider_post_url=publication.provider_post_url,
                recorded_published_at=publication.published_at,
                provider_created_at=publication.provider_created_at,
                content=publication.content,
                hook_type=candidate.hook_type,
                strategy_id=candidate.strategy_id,
                strategy_version=candidate.strategy_version,
                analytics=summary,
            )
        )
    return views
