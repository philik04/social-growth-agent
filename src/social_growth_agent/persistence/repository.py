"""Queries over the domain tables. Every function takes an open session; callers own
the transaction. No graph logic lives here."""

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from social_growth_agent.accounting import LLMUsage, PriceList, UsageCounts, estimate_cost
from social_growth_agent.accounting.costs import ResearchUsage
from social_growth_agent.errors import RunNotFoundError
from social_growth_agent.models import RunStatus
from social_growth_agent.persistence.tables import (
    CandidateFindingRow,
    ContentCandidateRow,
    CritiqueRow,
    FindingEvidenceRow,
    LLMCallRow,
    PricingVersionRow,
    ResearchBriefRow,
    ResearchFetchRow,
    ResearchFindingRow,
    ReviewDecisionRow,
    RunRow,
    SourcePostRow,
)
from social_growth_agent.persistence.views import (
    CandidateView,
    CritiqueView,
    DecisionView,
    FetchView,
    FindingView,
    PendingReview,
    PostView,
    ResearchView,
    ReviewView,
    RunDetail,
    RunFailure,
    RunSummary,
)

ACTIVE_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)


# --- writes ---------------------------------------------------------------------------


def save_price_list(session: Session, prices: PriceList) -> str:
    """Store a price list snapshot once (keyed by its content-derived id)."""
    session.execute(
        insert(PricingVersionRow)
        .values(
            id=prices.id,
            version=prices.version,
            as_of=prices.as_of,
            currency=prices.currency,
            content=prices.content(),
        )
        .on_conflict_do_nothing()
    )
    return prices.id


def insert_run(session: Session, row: RunRow) -> None:
    session.add(row)


def lock_run(session: Session, run_id: str) -> RunRow:
    """Row lock for the rest of the transaction: one state transition at a time."""
    row = session.scalars(select(RunRow).where(RunRow.id == run_id).with_for_update()).first()
    if row is None:
        raise RunNotFoundError(run_id)
    return row


def set_status(session: Session, run_id: str, status: RunStatus) -> None:
    session.execute(update(RunRow).where(RunRow.id == run_id).values(status=status.value))


def mark_failed(session: Session, run_id: str, error_type: str, message: str) -> None:
    session.execute(
        update(RunRow)
        .where(RunRow.id == run_id)
        .values(
            status=RunStatus.FAILED.value,
            failure_type=error_type,
            failure_message=message,
        )
    )


def mark_stalled(session: Session) -> list[str]:
    """Runs left queued/running by a previous process. They are only flagged; nothing
    is executed (and nothing is spent) until someone calls resume."""
    result = session.execute(
        update(RunRow)
        .where(RunRow.status.in_([s.value for s in ACTIVE_STATUSES]))
        .values(status=RunStatus.STALLED.value)
        .returning(RunRow.id)
    )
    return list(result.scalars())


# --- reads ----------------------------------------------------------------------------


def get_run_row(session: Session, run_id: str) -> RunRow:
    row = session.get(RunRow, run_id)
    if row is None:
        raise RunNotFoundError(run_id)
    return row


def summary(row: RunRow) -> RunSummary:
    failure = (
        RunFailure(node=row.failure_node, error_type=row.failure_type, message=row.failure_message)
        if row.failure_type or row.failure_message
        else None
    )
    return RunSummary(
        id=row.id,
        status=RunStatus(row.status),
        current_node=row.current_node,
        research_query=row.research_query,
        strategy_id=row.strategy_id,
        strategy_version=row.strategy_version,
        research_attempts=row.research_attempts,
        generation_attempts=row.generation_attempts,
        regeneration_rounds=row.regeneration_rounds,
        pricing_id=row.pricing_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        failure=failure,
    )


def list_runs(
    session: Session, *, status: RunStatus | None = None, limit: int = 50
) -> list[RunSummary]:
    query = select(RunRow).order_by(RunRow.created_at.desc()).limit(limit)
    if status is not None:
        query = query.where(RunRow.status == status.value)
    return [summary(r) for r in session.scalars(query)]


def run_detail(session: Session, run_id: str, prices: PriceList | None) -> RunDetail:
    row = get_run_row(session, run_id)
    usage = usage_counts(session, run_id)
    run_prices = run_price_list(session, row) or prices
    if run_prices is None:
        raise RuntimeError(f"run {run_id} has no stored price list")
    return RunDetail(
        run=summary(row),
        research_fetches=_fetches(session, run_id),
        source_posts=_posts(session, run_id),
        research=_research(session, run_id),
        candidates=candidate_views(session, run_id),
        review=_review(session, row),
        usage=usage,
        cost=estimate_cost(usage, run_prices, basis="run_pricing"),
    )


def pending_reviews(session: Session, *, limit: int = 50) -> list[PendingReview]:
    rows = session.scalars(
        select(RunRow)
        .where(RunRow.status == RunStatus.AWAITING_REVIEW.value)
        .order_by(RunRow.created_at)
        .limit(limit)
    )
    pending = []
    for row in rows:
        ids = list(row.review_candidate_ids)
        by_id = {c.id: c for c in candidate_views(session, row.id, ids)}
        pending.append(
            PendingReview(
                run_id=row.id,
                created_at=row.created_at,
                edits_remaining=_edits_remaining(row),
                regenerations_remaining=_regenerations_remaining(row),
                candidates=[by_id[i] for i in ids if i in by_id],
            )
        )
    return pending


def usage_counts(session: Session, run_id: str) -> UsageCounts:
    research = session.execute(
        select(
            ResearchFetchRow.provider,
            func.count(),
            func.sum(ResearchFetchRow.requests_made),
            func.sum(ResearchFetchRow.posts_fetched),
            func.sum(ResearchFetchRow.users_fetched),
        )
        .where(ResearchFetchRow.run_id == run_id)
        .group_by(ResearchFetchRow.provider)
        .order_by(ResearchFetchRow.provider)
    )
    llm = session.execute(
        select(
            LLMCallRow.provider,
            LLMCallRow.model,
            func.count(),
            func.coalesce(func.sum(LLMCallRow.input_tokens), 0),
            func.coalesce(func.sum(LLMCallRow.output_tokens), 0),
            func.count().filter(LLMCallRow.input_tokens.is_(None)),
        )
        .where(LLMCallRow.run_id == run_id)
        .group_by(LLMCallRow.provider, LLMCallRow.model)
        .order_by(LLMCallRow.provider, LLMCallRow.model)
    )
    return UsageCounts(
        research=[
            ResearchUsage(provider=p, fetches=n, requests=req, post_reads=posts, user_reads=users)
            for p, n, req, posts, users in research
        ],
        llm=[
            LLMUsage(
                provider=p,
                model=m,
                calls=n,
                input_tokens=i,
                output_tokens=o,
                calls_without_usage=missing,
            )
            for p, m, n, i, o, missing in llm
        ],
    )


def run_price_list(session: Session, row: RunRow) -> PriceList | None:
    if row.pricing_id is None:
        return None
    stored = session.get(PricingVersionRow, row.pricing_id)
    return None if stored is None else PriceList.model_validate(stored.content)


def candidate_views(
    session: Session, run_id: str, ids: Sequence[str] | None = None
) -> list[CandidateView]:
    query = select(ContentCandidateRow).where(ContentCandidateRow.run_id == run_id)
    if ids is not None:
        query = query.where(ContentCandidateRow.id.in_(ids))
    candidates = list(session.scalars(query.order_by(ContentCandidateRow.position)))
    cand_ids = [c.id for c in candidates]
    critiques: dict[str, list[CritiqueView]] = defaultdict(list)
    for k in session.scalars(
        select(CritiqueRow)
        .where(CritiqueRow.candidate_id.in_(cand_ids))
        .order_by(CritiqueRow.position)
    ):
        critiques[k.candidate_id].append(_critique(k))
    findings: dict[str, list[str]] = defaultdict(list)
    for link in session.scalars(
        select(CandidateFindingRow).where(CandidateFindingRow.candidate_id.in_(cand_ids))
    ):
        findings[link.candidate_id].append(link.finding_id)
    return [
        CandidateView(
            id=c.id,
            generation_attempt=c.generation_attempt,
            origin=c.origin,
            content=c.content,
            topic=c.topic,
            hook_type=c.hook_type,
            format=c.format,
            revises_candidate_id=c.revises_candidate_id,
            research_finding_ids=sorted(findings[c.id]),
            critiques=critiques[c.id],
        )
        for c in candidates
    ]


def _critique(k: CritiqueRow) -> CritiqueView:
    return CritiqueView(
        id=k.id,
        generation_attempt=k.generation_attempt,
        verdict=k.verdict,
        recommended_verdict=k.recommended_verdict,
        score=k.score,
        factual_risk=k.factual_risk,
        originality_risk=k.originality_risk,
        tone_match=k.tone_match,
        issues=k.issues,
        policy_violations=k.policy_violations,
        suggested_revision=k.suggested_revision,
    )


def _fetches(session: Session, run_id: str) -> list[FetchView]:
    rows = session.scalars(
        select(ResearchFetchRow)
        .where(ResearchFetchRow.run_id == run_id)
        .order_by(ResearchFetchRow.started_at)
    )
    return [FetchView.model_validate(r, from_attributes=True) for r in rows]


def _posts(session: Session, run_id: str) -> list[PostView]:
    rows = session.scalars(
        select(SourcePostRow)
        .where(SourcePostRow.run_id == run_id)
        .order_by(SourcePostRow.source_id)
    )
    return [
        PostView(
            source_id=r.source_id,
            author_username=r.author_username,
            text=r.text,
            created_at=r.post_created_at,
            likes=r.likes,
            reposts=r.reposts,
            replies=r.replies,
            quotes=r.quotes,
            impressions=r.impressions,
            query=r.query,
        )
        for r in rows
    ]


def _research(session: Session, run_id: str) -> ResearchView | None:
    brief = session.scalars(
        select(ResearchBriefRow).where(ResearchBriefRow.run_id == run_id)
    ).first()
    if brief is None:
        return None
    evidence: dict[str, list[str]] = defaultdict(list)
    for e in session.scalars(
        select(FindingEvidenceRow)
        .where(FindingEvidenceRow.run_id == run_id)
        .order_by(FindingEvidenceRow.position)
    ):
        evidence[e.finding_id].append(e.source_id)
    findings = session.scalars(
        select(ResearchFindingRow)
        .where(ResearchFindingRow.run_id == run_id)
        .order_by(ResearchFindingRow.position)
    )
    source: dict[str, Any] = brief.source
    return ResearchView(
        query=source["query"],
        effective_query=source["effective_query"],
        broadened_queries=source.get("broadened_queries", []),
        provider=source["provider"],
        synthetic=source["synthetic"],
        topic=brief.topic,
        summary=brief.summary,
        confidence=brief.confidence,
        limitations=brief.limitations,
        opportunities=brief.opportunities,
        findings=[
            FindingView(
                id=f.id,
                theme=f.theme,
                summary=f.summary,
                claim_type=f.claim_type,
                signal_strength=f.signal_strength,
                evidence_source_ids=evidence[f.id],
            )
            for f in findings
        ],
    )


def _review(session: Session, row: RunRow) -> ReviewView:
    decisions = session.scalars(
        select(ReviewDecisionRow)
        .where(ReviewDecisionRow.run_id == row.id)
        .order_by(ReviewDecisionRow.decided_at)
    )
    return ReviewView(
        awaiting_review=row.status == RunStatus.AWAITING_REVIEW.value,
        candidate_ids=list(row.review_candidate_ids),
        edits_remaining=_edits_remaining(row),
        regenerations_remaining=_regenerations_remaining(row),
        decisions=[DecisionView.model_validate(d, from_attributes=True) for d in decisions],
    )


def _edits_remaining(row: RunRow) -> int:
    return max(0, int(row.config.get("max_edit_rounds", 0)) - row.edit_rounds)


def _regenerations_remaining(row: RunRow) -> int:
    return max(0, int(row.config.get("max_regenerations", 0)) - row.regeneration_rounds)
