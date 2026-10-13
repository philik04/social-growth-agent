"""Experiments (Phase 8): rows, randomization slots, analysis facts and read models.

Only the experiment service writes here. Arms, assignments, events and results are
append-only (database triggers); an experiment row changes status and timestamps only.
"""

import random
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import and_, exists, select
from sqlalchemy.orm import Session

from social_growth_agent.analytics.evaluation import PostInput
from social_growth_agent.analytics.experiment import ArmInput, RunFacts
from social_growth_agent.analytics.feasibility import feasibility
from social_growth_agent.errors import ExperimentNotFoundError
from social_growth_agent.models import PublicationStatus, ReviewAction, new_id
from social_growth_agent.models.experiment import (
    EvidenceClass,
    ExperimentDesign,
    ExperimentEventKind,
    ExperimentFactor,
    ExperimentStatus,
    ResultFlag,
    StopReason,
    Treatment,
    Verdict,
)
from social_growth_agent.persistence.evaluations import snapshot_input
from social_growth_agent.persistence.strategies import active_version_id, strategy_sha256
from social_growth_agent.persistence.tables import (
    ContentCandidateRow,
    ExperimentArmRow,
    ExperimentAssignmentRow,
    ExperimentEventRow,
    ExperimentResultRow,
    ExperimentRow,
    PostMetricRow,
    PublicationRow,
    ReviewDecisionRow,
    RunRow,
    StrategyVersionRow,
)
from social_growth_agent.persistence.views import (
    AssignmentView,
    ExperimentArmView,
    ExperimentEventView,
    ExperimentLineageView,
    ExperimentResultView,
    ExperimentView,
    LineageEntryView,
)

PER_ARM_PER_BLOCK = 2
"""Permuted blocks of ``2 x arms`` slots: each arm gets exactly two slots per block."""


# --- rows ---------------------------------------------------------------------------------


def get(session: Session, experiment_id: str, *, lock: bool = False) -> ExperimentRow:
    query = select(ExperimentRow).where(ExperimentRow.id == experiment_id)
    if lock:
        query = query.with_for_update()
    row = session.scalars(query).first()
    if row is None:
        raise ExperimentNotFoundError(f"experiment not found: {experiment_id}")
    return row


def list_rows(
    session: Session,
    *,
    account_id: str | None,
    status: ExperimentStatus | None,
    limit: int,
) -> list[ExperimentRow]:
    query = select(ExperimentRow).order_by(ExperimentRow.created_at.desc(), ExperimentRow.id)
    if account_id is not None:
        query = query.where(ExperimentRow.account_id == account_id)
    if status is not None:
        query = query.where(ExperimentRow.status == status.value)
    return list(session.scalars(query.limit(limit)))


def open_experiment(session: Session, account_id: str) -> ExperimentRow | None:
    return session.scalars(
        select(ExperimentRow).where(
            ExperimentRow.account_id == account_id,
            ExperimentRow.status.in_(
                [ExperimentStatus.APPROVED.value, ExperimentStatus.RUNNING.value]
            ),
        )
    ).first()


def arms(session: Session, experiment_id: str) -> list[ExperimentArmRow]:
    return list(
        session.scalars(
            select(ExperimentArmRow)
            .where(ExperimentArmRow.experiment_id == experiment_id)
            .order_by(ExperimentArmRow.position)
        )
    )


def assignments(session: Session, experiment_id: str) -> list[ExperimentAssignmentRow]:
    return list(
        session.scalars(
            select(ExperimentAssignmentRow)
            .where(ExperimentAssignmentRow.experiment_id == experiment_id)
            .order_by(ExperimentAssignmentRow.block_no, ExperimentAssignmentRow.position)
        )
    )


def assignment_of_run(session: Session, run_id: str) -> ExperimentAssignmentRow | None:
    return session.scalars(
        select(ExperimentAssignmentRow).where(ExperimentAssignmentRow.run_id == run_id)
    ).first()


def result_of(session: Session, experiment_id: str) -> ExperimentResultRow | None:
    return session.scalars(
        select(ExperimentResultRow).where(ExperimentResultRow.experiment_id == experiment_id)
    ).first()


def adopted_version_id(session: Session, result_id: str) -> str | None:
    return session.scalar(
        select(StrategyVersionRow.id).where(
            StrategyVersionRow.source_experiment_result_id == result_id
        )
    )


def add_event(
    session: Session,
    experiment_id: str,
    kind: ExperimentEventKind,
    *,
    actor: str,
    auth_method: str,
    at: datetime,
    reason: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    session.add(
        ExperimentEventRow(
            id=new_id("xev"),
            experiment_id=experiment_id,
            kind=kind.value,
            actor=actor,
            auth_method=auth_method,
            reason=reason,
            detail=detail,
            created_at=at,
        )
    )


def events(session: Session, experiment_id: str) -> list[ExperimentEventRow]:
    return list(
        session.scalars(
            select(ExperimentEventRow)
            .where(ExperimentEventRow.experiment_id == experiment_id)
            .order_by(ExperimentEventRow.created_at, ExperimentEventRow.id)
        )
    )


# --- randomization ------------------------------------------------------------------------


class _Slot(Protocol):
    @property
    def arm_id(self) -> str: ...
    @property
    def block_no(self) -> int: ...


class _Arm(Protocol):
    @property
    def id(self) -> str: ...


def draw_slot[A: _Arm](
    taken: Sequence[_Slot],
    arm_rows: Sequence[A],
    rng: random.Random,
) -> tuple[int, int, A]:
    """The next (block_no, position, arm) under permuted-block randomization.

    Call with the experiment row locked (``SELECT ... FOR UPDATE``) so draws are
    serialized; ``UNIQUE (experiment_id, block_no, position)`` refuses a duplicate slot
    if that is ever violated. Within a block, the arm is drawn uniformly from the arms
    that still have slots left, which is the same as shuffling the block in advance but
    keeps nothing about future assignments in the database.
    """
    size = PER_ARM_PER_BLOCK * len(arm_rows)
    n = len(taken)
    block_no, position = divmod(n, size)
    used = Counter(a.arm_id for a in taken if a.block_no == block_no)
    open_arms = [a for a in arm_rows if used[a.id] < PER_ARM_PER_BLOCK]
    return block_no, position, rng.choice(open_arms)


# --- analysis facts -----------------------------------------------------------------------


def load_facts(
    session: Session, experiment: ExperimentRow, cutoff: datetime
) -> tuple[tuple[ArmInput, ...], tuple[RunFacts, ...]]:
    """Every assignment made at or before the cutoff, with what was recorded for its run
    at or before the cutoff. Snapshots captured after the cutoff are excluded by the
    analysis (``classify``), so the result for a past cutoff does not change later."""
    design = ExperimentDesign.model_validate(experiment.design)
    arm_rows = arms(session, experiment.id)
    arm_inputs = tuple(
        ArmInput(
            arm_id=a.id,
            label=a.label,
            is_control=a.is_control,
            treatment=Treatment.model_validate(a.treatment),
        )
        for a in arm_rows
    )
    taken = [a for a in assignments(session, experiment.id) if a.assigned_at <= cutoff]
    run_ids = [a.run_id for a in taken]
    if not run_ids:
        return arm_inputs, ()
    runs = {r.id: r for r in session.scalars(select(RunRow).where(RunRow.id.in_(run_ids)))}
    generated = set(
        session.scalars(
            select(ContentCandidateRow.run_id).where(
                ContentCandidateRow.run_id.in_(run_ids), ContentCandidateRow.created_at <= cutoff
            )
        )
    )
    reviewed: set[str] = set()
    approved: set[str] = set()
    for run_id, action in session.execute(
        select(ReviewDecisionRow.run_id, ReviewDecisionRow.action).where(
            ReviewDecisionRow.run_id.in_(run_ids), ReviewDecisionRow.decided_at <= cutoff
        )
    ):
        reviewed.add(run_id)
        if action == ReviewAction.APPROVE.value:
            approved.add(run_id)
    posts: dict[str, list[PostInput]] = defaultdict(list)
    query = (
        select(PublicationRow, ContentCandidateRow, PostMetricRow)
        .join(ContentCandidateRow, ContentCandidateRow.id == PublicationRow.candidate_id)
        .outerjoin(
            PostMetricRow,
            and_(
                PostMetricRow.publication_id == PublicationRow.id,
                PostMetricRow.snapshot_age == design.snapshot_age,
            ),
        )
        .where(PublicationRow.run_id.in_(run_ids))
        .where(PublicationRow.status == PublicationStatus.PUBLISHED.value)
        .where(PublicationRow.published_at.is_not(None))
        .where(PublicationRow.published_at <= cutoff)
        .order_by(PublicationRow.id)
    )
    digests: dict[str, str] = {}
    for pub, cand, metric in session.execute(query):
        run = runs[pub.run_id]
        if run.id not in digests:
            digests[run.id] = strategy_sha256(run.strategy)
        assert pub.published_at is not None
        posts[run.id].append(
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
    facts = tuple(
        RunFacts(
            assignment_id=a.id,
            arm_id=a.arm_id,
            run_id=a.run_id,
            assigned_at=a.assigned_at,
            generated=a.run_id in generated,
            reviewed=a.run_id in reviewed,
            approved=a.run_id in approved,
            posts=tuple(posts.get(a.run_id, ())),
        )
        for a in taken
    )
    return arm_inputs, facts


# --- read models --------------------------------------------------------------------------


def adoption_blockers(
    session: Session, experiment: ExperimentRow, result: ExperimentResultRow
) -> list[str]:
    """Why this result cannot be adopted (empty when it can)."""
    blockers = []
    if experiment.synthetic:
        blockers.append("synthetic experiments are never adoptable")
    if result.evidence_class != EvidenceClass.CONFIRMATORY.value:
        blockers.append("exploratory result (outcomes were unblinded early)")
    if result.verdict != Verdict.FAVOURS_VARIANT.value:
        blockers.append(f"verdict is {result.verdict}, not favours_variant")
    if active_version_id(session, experiment.account_id) != experiment.base_version_id:
        blockers.append("the base strategy version is no longer the active one")
    if adopted_version_id(session, result.id) is not None:
        blockers.append("already adopted")
    return blockers


def result_view(
    session: Session, experiment: ExperimentRow, row: ExperimentResultRow
) -> ExperimentResultView:
    blockers = adoption_blockers(session, experiment, row)
    return ExperimentResultView(
        id=row.id,
        verdict=Verdict(row.verdict),
        evidence_class=EvidenceClass(row.evidence_class),
        peeked=row.peeked,
        cutoff=row.cutoff,
        analysis_version=row.analysis_version,
        input_digest=row.input_digest,
        digest=row.digest,
        flags=[ResultFlag(f) for f in row.flags],
        concluded_by=row.concluded_by,
        auth_method=row.auth_method,
        created_at=row.created_at,
        adoptable=not blockers,
        adoption_blockers=blockers,
        adopted_version_id=adopted_version_id(session, row.id),
    )


def arm_view(row: ExperimentArmRow) -> ExperimentArmView:
    treatment = Treatment.model_validate(row.treatment)
    return ExperimentArmView(
        id=row.id,
        label=row.label,
        position=row.position,
        is_control=row.is_control,
        treatment=treatment,
        description=treatment.describe(),
    )


def experiment_view(session: Session, row: ExperimentRow) -> ExperimentView:
    design = ExperimentDesign.model_validate(row.design)
    arm_rows = arms(session, row.id)
    counts = Counter(a.arm_id for a in assignments(session, row.id))
    result = result_of(session, row.id)
    return ExperimentView(
        id=row.id,
        account_id=row.account_id,
        strategy_id=row.strategy_id,
        base_version_id=row.base_version_id,
        factor=ExperimentFactor(row.factor),
        synthetic=row.synthetic,
        design=design,
        design_sha256=row.design_sha256,
        snapshot_age_seconds=row.snapshot_age_seconds,
        status=ExperimentStatus(row.status),
        peeked=row.peeked,
        stop_reason=None if row.stop_reason is None else StopReason(row.stop_reason),
        created_by=row.created_by,
        auth_method=row.auth_method,
        created_at=row.created_at,
        approved_at=row.approved_at,
        started_at=row.started_at,
        stopped_at=row.stopped_at,
        unblinded_at=row.unblinded_at,
        concluded_at=row.concluded_at,
        cancelled_at=row.cancelled_at,
        planned_end_at=planned_end(row, design),
        arms=[arm_view(a) for a in arm_rows],
        assigned_per_arm={a.label: counts[a.id] for a in arm_rows},
        runs_cap_per_arm=design.runs_cap_per_arm,
        feasibility=feasibility(design),
        result=None if result is None else result_view(session, row, result),
        events=[
            ExperimentEventView(
                id=e.id,
                kind=ExperimentEventKind(e.kind),
                actor=e.actor,
                auth_method=e.auth_method,
                reason=e.reason,
                detail=e.detail,
                created_at=e.created_at,
            )
            for e in events(session, row.id)
        ],
    )


def planned_end(row: ExperimentRow, design: ExperimentDesign) -> datetime | None:
    if row.started_at is None:
        return None
    return row.started_at + timedelta(days=design.max_duration_days)


def assignment_views(session: Session, experiment_id: str) -> list[AssignmentView]:
    labels = {a.id: a.label for a in arms(session, experiment_id)}
    return [assignment_view(a, labels[a.arm_id]) for a in assignments(session, experiment_id)]


def assignment_view(row: ExperimentAssignmentRow, label: str) -> AssignmentView:
    return AssignmentView(
        id=row.id,
        arm=label,
        run_id=row.run_id,
        block_no=row.block_no,
        position=row.position,
        assigned_by=row.assigned_by,
        auth_method=row.auth_method,
        assigned_at=row.assigned_at,
    )


def lineage(session: Session, experiment: ExperimentRow) -> ExperimentLineageView:
    labels = {a.id: a.label for a in arms(session, experiment.id)}
    taken = assignments(session, experiment.id)
    run_ids = [a.run_id for a in taken]
    candidates: dict[str, list[str]] = defaultdict(list)
    for run_id, cand_id in session.execute(
        select(ContentCandidateRow.run_id, ContentCandidateRow.id)
        .where(ContentCandidateRow.run_id.in_(run_ids))
        .order_by(ContentCandidateRow.position, ContentCandidateRow.id)
    ):
        candidates[run_id].append(cand_id)
    publications: dict[str, list[str]] = defaultdict(list)
    metrics: dict[str, list[str]] = defaultdict(list)
    for run_id, pub_id, metric_id in session.execute(
        select(PublicationRow.run_id, PublicationRow.id, PostMetricRow.id)
        .outerjoin(PostMetricRow, PostMetricRow.publication_id == PublicationRow.id)
        .where(PublicationRow.run_id.in_(run_ids))
        .order_by(PublicationRow.id, PostMetricRow.id)
    ):
        if pub_id not in publications[run_id]:
            publications[run_id].append(pub_id)
        if metric_id is not None:
            metrics[run_id].append(metric_id)
    result = result_of(session, experiment.id)
    return ExperimentLineageView(
        experiment_id=experiment.id,
        base_version_id=experiment.base_version_id,
        entries=[
            LineageEntryView(
                assignment_id=a.id,
                arm=labels[a.arm_id],
                run_id=a.run_id,
                candidate_ids=candidates[a.run_id],
                publication_ids=publications[a.run_id],
                post_metric_ids=metrics[a.run_id],
            )
            for a in taken
        ],
        adopted_version_id=None if result is None else adopted_version_id(session, result.id),
    )


def has_assignments(session: Session, experiment_id: str) -> bool:
    return bool(
        session.scalar(
            select(exists().where(ExperimentAssignmentRow.experiment_id == experiment_id))
        )
    )
