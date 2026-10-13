"""Strategy versions, activations, proposals and decisions (Phase 7): rows and read models.

Versions, activations and decisions are append-only (a trigger rejects UPDATE/DELETE).
Proposals change only by guarded status transitions (``WHERE status = :expected``).
The service layer decides *whether* something may happen; these helpers only read and
write rows.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from social_growth_agent.accounting import LLMUsage, PriceList, UsageCounts, estimate_cost
from social_growth_agent.analytics.evaluation import sha256_json
from social_growth_agent.errors import (
    ProposalNotFoundError,
    StrategyConflictError,
    StrategyVersionNotFoundError,
)
from social_growth_agent.models import ContentStrategy
from social_growth_agent.models.strategy import (
    ProposalStatus,
    StrategyVersionOrigin,
    ValidatedProposal,
)
from social_growth_agent.persistence.tables import (
    PricingVersionRow,
    RunRow,
    StrategyActivationRow,
    StrategyProposalDecisionRow,
    StrategyProposalRow,
    StrategyVersionRow,
)
from social_growth_agent.persistence.views import (
    ActivationView,
    EvaluationUsageView,
    ProposalDecisionView,
    ProposalUsageView,
    ProposalView,
    StrategyVersionView,
)

# --- canonical strategy bodies ---------------------------------------------------------------


def canonical_strategy(raw: dict[str, Any]) -> dict[str, Any]:
    """A strategy body in its canonical JSON form (defaults filled in), so a snapshot
    stored before Phase 7 and the same strategy stored now compare equal."""
    try:
        return ContentStrategy.model_validate(raw).model_dump(mode="json")
    except ValidationError:
        return raw


def strategy_sha256(raw: dict[str, Any]) -> str:
    return sha256_json(canonical_strategy(raw))


# --- versions ---------------------------------------------------------------------------------


def get_version(session: Session, version_id: str) -> StrategyVersionRow:
    row = session.get(StrategyVersionRow, version_id)
    if row is None:
        raise StrategyVersionNotFoundError(f"strategy version not found: {version_id}")
    return row


def find_version(session: Session, strategy_id: str, version: int) -> StrategyVersionRow | None:
    return session.scalars(
        select(StrategyVersionRow)
        .where(StrategyVersionRow.strategy_id == strategy_id)
        .where(StrategyVersionRow.version == version)
    ).first()


def versions_of(session: Session, strategy_id: str) -> list[StrategyVersionRow]:
    return list(
        session.scalars(
            select(StrategyVersionRow)
            .where(StrategyVersionRow.strategy_id == strategy_id)
            .order_by(StrategyVersionRow.version)
        )
    )


def account_of_strategy(session: Session, strategy_id: str) -> str | None:
    return session.scalars(
        select(StrategyVersionRow.account_id)
        .where(StrategyVersionRow.strategy_id == strategy_id)
        .limit(1)
    ).first()


def registered_match(session: Session, strategy: ContentStrategy) -> str | None:
    """The version id when ``strategy`` is exactly a registered version; None when its
    (id, version) is not registered. A registered (id, version) with a different body is
    a conflict: two different strategies may not share one version number."""
    row = find_version(session, strategy.id, strategy.version)
    if row is None:
        return None
    if row.content_sha256 != strategy_sha256(strategy.model_dump(mode="json")):
        raise StrategyConflictError(
            f"strategy {strategy.id} v{strategy.version} is registered with a different body; "
            "use the registered version (use_active_strategy) or a new version number"
        )
    return row.id


def next_version_number(session: Session, strategy_id: str) -> int:
    """One above every version this strategy id has used: registered ones *and* versions
    sent inline to ``POST /runs``, so a new version never collides with an ad-hoc one."""
    registered = session.scalar(
        select(func.max(StrategyVersionRow.version)).where(
            StrategyVersionRow.strategy_id == strategy_id
        )
    )
    used = session.scalar(
        select(func.max(RunRow.strategy_version)).where(RunRow.strategy_id == strategy_id)
    )
    return max(registered or 0, used or 0) + 1


def insert_version(
    session: Session,
    *,
    version_id: str,
    strategy: ContentStrategy,
    origin: StrategyVersionOrigin,
    parent_version_id: str | None,
    created_by: str | None,
    auth_method: str | None,
    note: str | None,
) -> StrategyVersionRow:
    content = strategy.model_dump(mode="json")
    row = StrategyVersionRow(
        id=version_id,
        strategy_id=strategy.id,
        account_id=strategy.account_id,
        version=strategy.version,
        content=content,
        content_sha256=strategy_sha256(content),
        parent_version_id=parent_version_id,
        origin=origin.value,
        created_by=created_by,
        auth_method=auth_method,
        note=note,
    )
    session.add(row)
    session.flush()
    return row


def version_view(row: StrategyVersionRow, active_id: str | None) -> StrategyVersionView:
    return StrategyVersionView(
        id=row.id,
        strategy_id=row.strategy_id,
        account_id=row.account_id,
        version=row.version,
        content=ContentStrategy.model_validate(row.content),
        content_sha256=row.content_sha256,
        parent_version_id=row.parent_version_id,
        origin=StrategyVersionOrigin(row.origin),
        created_by=row.created_by,
        auth_method=row.auth_method,
        note=row.note,
        created_at=row.created_at,
        active=row.id == active_id,
    )


# --- activations ------------------------------------------------------------------------------


def latest_activation(session: Session, account_id: str) -> StrategyActivationRow | None:
    return session.scalars(
        select(StrategyActivationRow)
        .where(StrategyActivationRow.account_id == account_id)
        .order_by(StrategyActivationRow.seq.desc())
        .limit(1)
    ).first()


def active_version_id(session: Session, account_id: str) -> str | None:
    activation = latest_activation(session, account_id)
    return activation.strategy_version_id if activation is not None else None


def activations_of(session: Session, version_ids: Sequence[str]) -> list[StrategyActivationRow]:
    if not version_ids:
        return []
    return list(
        session.scalars(
            select(StrategyActivationRow)
            .where(StrategyActivationRow.strategy_version_id.in_(version_ids))
            .order_by(StrategyActivationRow.account_id, StrategyActivationRow.seq)
        )
    )


def activation_view(row: StrategyActivationRow) -> ActivationView:
    return ActivationView.model_validate(row, from_attributes=True)


# --- proposals --------------------------------------------------------------------------------


def get_proposal(session: Session, proposal_id: str, *, lock: bool = False) -> StrategyProposalRow:
    query = select(StrategyProposalRow).where(StrategyProposalRow.id == proposal_id)
    if lock:
        query = query.with_for_update()
    row = session.scalars(query).first()
    if row is None:
        raise ProposalNotFoundError(f"proposal not found: {proposal_id}")
    return row


def open_proposal(session: Session, account_id: str) -> StrategyProposalRow | None:
    return session.scalars(
        select(StrategyProposalRow)
        .where(StrategyProposalRow.account_id == account_id)
        .where(
            StrategyProposalRow.status.in_(
                [ProposalStatus.GENERATING.value, ProposalStatus.PENDING_REVIEW.value]
            )
        )
    ).first()


def transition(session: Session, proposal_id: str, expected: ProposalStatus, **values: Any) -> bool:
    """Guarded status change: applies only if the proposal is still in ``expected``."""
    result = session.execute(
        update(StrategyProposalRow)
        .where(StrategyProposalRow.id == proposal_id)
        .where(StrategyProposalRow.status == expected.value)
        .values(**values)
    )
    return bool(getattr(result, "rowcount", 0))


def list_proposals(
    session: Session,
    *,
    status: ProposalStatus | None,
    account_id: str | None,
    evaluation_id: str | None,
    limit: int,
) -> list[StrategyProposalRow]:
    query = select(StrategyProposalRow).order_by(StrategyProposalRow.created_at.desc())
    if status is not None:
        query = query.where(StrategyProposalRow.status == status.value)
    if account_id is not None:
        query = query.where(StrategyProposalRow.account_id == account_id)
    if evaluation_id is not None:
        query = query.where(StrategyProposalRow.evaluation_id == evaluation_id)
    return list(session.scalars(query.limit(limit)))


def decision_of(session: Session, proposal_id: str) -> StrategyProposalDecisionRow | None:
    return session.scalars(
        select(StrategyProposalDecisionRow).where(
            StrategyProposalDecisionRow.proposal_id == proposal_id
        )
    ).first()


def proposal_usage(
    row: StrategyProposalRow, stored: PriceList | None, current: PriceList
) -> ProposalUsageView:
    llm = []
    if row.llm_calls:
        reported = row.input_tokens is not None and row.output_tokens is not None
        llm.append(
            LLMUsage(
                provider=row.provider,
                model=row.model,
                calls=row.llm_calls,
                input_tokens=row.input_tokens or 0,
                output_tokens=row.output_tokens or 0,
                calls_without_usage=0 if reported else row.llm_calls,
            )
        )
    counts = UsageCounts(llm=llm)
    return ProposalUsageView(
        usage=counts,
        at_proposal_pricing=estimate_cost(counts, stored or current, basis="run_pricing"),
        at_current_pricing=estimate_cost(counts, current, basis="current_pricing"),
    )


def evaluation_usage(
    session: Session, evaluation_id: str, current: PriceList
) -> EvaluationUsageView:
    """Roll up the usage of every proposal made from one evaluation."""
    rows = list(
        session.scalars(
            select(StrategyProposalRow)
            .where(StrategyProposalRow.evaluation_id == evaluation_id)
            .order_by(StrategyProposalRow.created_at)
        )
    )
    merged: dict[tuple[str, str], LLMUsage] = {}
    total: Decimal | None = Decimal(0)
    missing: set[str] = set()
    for row in rows:
        own = proposal_usage(row, stored_price_list(session, row.pricing_id), current)
        for llm in own.usage.llm:
            key = (llm.provider, llm.model)
            seen = merged.get(key)
            merged[key] = (
                llm
                if seen is None
                else LLMUsage(
                    provider=llm.provider,
                    model=llm.model,
                    calls=seen.calls + llm.calls,
                    input_tokens=seen.input_tokens + llm.input_tokens,
                    output_tokens=seen.output_tokens + llm.output_tokens,
                    calls_without_usage=seen.calls_without_usage + llm.calls_without_usage,
                )
            )
        missing.update(own.at_proposal_pricing.missing_prices)
        own_total = own.at_proposal_pricing.total
        total = None if total is None or own_total is None else total + own_total
    counts = UsageCounts(llm=[merged[key] for key in sorted(merged)])
    return EvaluationUsageView(
        proposals=len(rows),
        usage=counts,
        at_current_pricing=estimate_cost(counts, current, basis="current_pricing"),
        at_proposal_pricing_total=total,
        proposal_pricing_missing=sorted(missing),
    )


def stored_price_list(session: Session, pricing_id: str) -> PriceList | None:
    stored = session.get(PricingVersionRow, pricing_id)
    return None if stored is None else PriceList.model_validate(stored.content)


def proposal_view(
    session: Session,
    row: StrategyProposalRow,
    *,
    prices: PriceList,
    now: datetime,
    stale_seconds: float,
) -> ProposalView:
    decision = decision_of(session, row.id)
    stale = (
        row.status == ProposalStatus.GENERATING.value
        and (now - row.started_at).total_seconds() > stale_seconds
    )
    return ProposalView(
        id=row.id,
        evaluation_id=row.evaluation_id,
        evaluation_digest=row.evaluation_digest,
        account_id=row.account_id,
        base_version_id=row.base_version_id,
        status=ProposalStatus(row.status),
        stale=stale,
        outcome=row.outcome,
        proposer=row.proposer,
        provider=row.provider,
        model=row.model,
        prompt_version=row.prompt_version,
        proposal=ValidatedProposal.model_validate(row.proposal) if row.proposal else None,
        proposal_sha256=row.proposal_sha256,
        validation_errors=row.validation_errors,
        error_type=row.error_type,
        error_message=row.error_message,
        retry_of=row.retry_of,
        requested_by=row.requested_by,
        started_at=row.started_at,
        finished_at=row.finished_at,
        latency_ms=row.latency_ms,
        decision=(
            ProposalDecisionView.model_validate(decision, from_attributes=True)
            if decision is not None
            else None
        ),
        usage=proposal_usage(row, stored_price_list(session, row.pricing_id), prices),
    )
