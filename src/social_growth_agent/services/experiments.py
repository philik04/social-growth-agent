"""Experiments (Phase 8): pre-registered A/B tests of hook type or post length.

Every state change is an explicit, authenticated request; reads never write, call a
model or call the platform:

- **Create** a draft with its full pre-registration (hypothesis, arms, primary metric,
  snapshot age, sample target, maximum duration, stopping rules). The design is frozen
  from then on (a database trigger refuses changes); a different design is a new draft.
  The account's active strategy version is pinned as the base.
- **Approve**, then **start**. At most one approved or running experiment per account.
- **Create a run** in the experiment: under a row lock on the experiment, the next arm is
  drawn (permuted blocks, ``secrets.SystemRandom``), its treatment is written into the
  run's content policy as hard rules, and the run and its assignment are inserted in one
  transaction. Generation starts only after that commit. Review and publishing stay the
  normal, human, explicit steps.
- **Stop** (operational, cost, content safety or other; never "results look good")
  ends new assignments. It does not shorten the analysis plan. There is no way to extend
  an experiment: the maximum duration is part of the design.
- **Unblind** shows outcome summaries before the conclusion. It is recorded, and every
  later verdict of the experiment is exploratory and never adoptable.
- **Conclude** stores an immutable result once every arm has its planned sample, the
  maximum duration has passed, or assignment was stopped. Below the planned sample the
  verdict is ``insufficient_data`` whatever the numbers say.
- **Adopt** a confirmatory ``favours_variant`` on real data as a new, inactive strategy
  version. Activation stays the Phase 7 compare-and-set request.
"""

import logging
import random
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from social_growth_agent.accounting import PriceList, UsageCounts, estimate_cost, merge_usage
from social_growth_agent.analytics.evaluation import sha256_json
from social_growth_agent.analytics.experiment import (
    ANALYSIS_VERSION,
    AnalysisInput,
    analysis_digest,
    analyze,
    input_digest,
)
from social_growth_agent.errors import (
    AdoptionRefusedError,
    ExperimentStateError,
    InvalidExperimentError,
)
from social_growth_agent.graph import RunConfig
from social_growth_agent.models import (
    Account,
    ContentIntent,
    ContentStrategy,
    RunStatus,
    new_id,
    utc_now,
)
from social_growth_agent.models.experiment import (
    AnalysisStage,
    ExperimentAnalysis,
    ExperimentDesign,
    ExperimentEventKind,
    ExperimentFactor,
    ExperimentStatus,
    StopReason,
    Treatment,
    Verdict,
)
from social_growth_agent.models.strategy import ReviewAuthMethod, StrategyVersionOrigin
from social_growth_agent.persistence import Database
from social_growth_agent.persistence import experiments as xdb
from social_growth_agent.persistence import repository as repo
from social_growth_agent.persistence import strategies as sdb
from social_growth_agent.persistence.tables import (
    ExperimentArmRow,
    ExperimentAssignmentRow,
    ExperimentResultRow,
    ExperimentRow,
)
from social_growth_agent.persistence.views import (
    ArmUsageView,
    AssignmentView,
    ExperimentLineageView,
    ExperimentRunCreated,
    ExperimentUsageView,
    ExperimentView,
    ProjectedCostView,
    StrategyVersionView,
)
from social_growth_agent.policies import ContentPolicy
from social_growth_agent.policies.treatment import apply_treatment
from social_growth_agent.services.evaluations import EvaluationDefaults
from social_growth_agent.services.runs import RunService

logger = logging.getLogger(__name__)

_FINISHED = (RunStatus.APPROVED.value, RunStatus.REJECTED.value, RunStatus.FAILED.value)
_ASSIGNABLE = (ExperimentStatus.DRAFT, ExperimentStatus.APPROVED, ExperimentStatus.RUNNING)
_PRECISION = Decimal("0.000001")
_Change = Callable[[Session, ExperimentRow, datetime], None]


class CreateExperimentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)
    design: ExperimentDesign
    reviewer: str = Field(min_length=1, max_length=128)


class ExperimentActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=1000)


class StopExperimentBody(BaseModel):
    """``reason`` is one of the pre-defined, outcome-independent reasons."""

    model_config = ConfigDict(extra="forbid")

    reviewer: str = Field(min_length=1, max_length=128)
    reason: StopReason
    note: str | None = Field(default=None, max_length=1000)


class UnblindBody(BaseModel):
    """A justification is required: unblinding makes every later conclusion exploratory."""

    model_config = ConfigDict(extra="forbid")

    reviewer: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)


class ExperimentRunBody(BaseModel):
    """The run's account must be the experiment's. ``config`` may set the usual run
    limits; the arm's treatment is applied on top of its content policy."""

    model_config = ConfigDict(extra="forbid")

    account: Account
    config: RunConfig = Field(default_factory=RunConfig)
    reviewer: str = Field(min_length=1, max_length=128)


class AdoptBody(BaseModel):
    """``variant`` names the arm to adopt; its own effect must favour it."""

    model_config = ConfigDict(extra="forbid")

    variant: str = Field(min_length=1, max_length=32)
    reviewer: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=1000)


@dataclass(frozen=True)
class _Actor:
    name: str
    auth: ReviewAuthMethod


class ExperimentService:
    def __init__(
        self,
        db: Database,
        *,
        runs: RunService,
        defaults: EvaluationDefaults,
        prices: PriceList,
        rng: random.Random | None = None,
    ) -> None:
        self._db = db
        self._runs = runs
        self._defaults = defaults
        self._prices = prices
        self._rng = rng or secrets.SystemRandom()

    # --- design and lifecycle -----------------------------------------------------------

    def create(
        self,
        body: CreateExperimentBody,
        *,
        auth: ReviewAuthMethod,
        now: datetime | None = None,
    ) -> ExperimentView:
        now = now or utc_now()
        design = body.design
        age = next(
            (a for a in self._defaults.ages if a.label == design.snapshot_age.strip().lower()),
            None,
        )
        if age is None:
            known = ", ".join(a.label for a in self._defaults.ages)
            raise InvalidExperimentError(
                f"snapshot_age must be one of the configured ages ({known})"
            )
        design = design.model_copy(update={"snapshot_age": age.label})
        for arm in design.arms:
            try:
                apply_treatment(ContentPolicy(), arm.treatment)
            except ValueError as exc:
                raise InvalidExperimentError(f"arm {arm.label}: {exc}") from None
        with self._db.transaction() as session:
            activation = sdb.latest_activation(session, body.account_id)
            if activation is None:
                raise ExperimentStateError(
                    f"account {body.account_id} has no active strategy: register and "
                    "activate a version first"
                )
            base = sdb.get_version(session, activation.strategy_version_id)
            design_json = design.model_dump(mode="json")
            row = ExperimentRow(
                id=new_id("exp"),
                account_id=body.account_id,
                strategy_id=base.strategy_id,
                base_version_id=base.id,
                factor=design.factor.value,
                synthetic=design.synthetic,
                design=design_json,
                design_sha256=sha256_json(design_json),
                snapshot_age_seconds=age.seconds,
                status=ExperimentStatus.DRAFT.value,
                peeked=False,
                created_by=body.reviewer,
                auth_method=auth.value,
                created_at=now,
            )
            session.add(row)
            session.flush()
            for position, arm in enumerate(design.arms):
                session.add(
                    ExperimentArmRow(
                        id=new_id("arm"),
                        experiment_id=row.id,
                        label=arm.label,
                        position=position,
                        is_control=arm.is_control,
                        treatment=arm.treatment.model_dump(mode="json"),
                    )
                )
            xdb.add_event(
                session,
                row.id,
                ExperimentEventKind.CREATED,
                actor=body.reviewer,
                auth_method=auth.value,
                at=now,
                detail={"design_sha256": row.design_sha256, "base_version_id": base.id},
            )
            session.flush()
            view = xdb.experiment_view(session, row)
        logger.info("experiment created", extra={"experiment_id": view.id})
        return view

    def approve(
        self, experiment_id: str, body: ExperimentActionBody, *, auth: ReviewAuthMethod
    ) -> ExperimentView:
        def change(session: Session, row: ExperimentRow, now: datetime) -> None:
            self._require(row, ExperimentStatus.DRAFT)
            self._require_base_active(session, row)
            row.status = ExperimentStatus.APPROVED.value
            row.approved_at = now

        return self._transition(
            experiment_id,
            _Actor(body.reviewer, auth),
            ExperimentEventKind.APPROVED,
            change,
            note=body.note,
        )

    def start(
        self, experiment_id: str, body: ExperimentActionBody, *, auth: ReviewAuthMethod
    ) -> ExperimentView:
        def change(session: Session, row: ExperimentRow, now: datetime) -> None:
            self._require(row, ExperimentStatus.APPROVED)
            self._require_base_active(session, row)
            row.status = ExperimentStatus.RUNNING.value
            row.started_at = now

        return self._transition(
            experiment_id,
            _Actor(body.reviewer, auth),
            ExperimentEventKind.STARTED,
            change,
            note=body.note,
        )

    def stop(
        self, experiment_id: str, body: StopExperimentBody, *, auth: ReviewAuthMethod
    ) -> ExperimentView:
        def change(_: Session, row: ExperimentRow, now: datetime) -> None:
            self._require(row, ExperimentStatus.RUNNING)
            row.status = ExperimentStatus.STOPPED.value
            row.stopped_at = now
            row.stop_reason = body.reason.value

        return self._transition(
            experiment_id,
            _Actor(body.reviewer, auth),
            ExperimentEventKind.STOPPED,
            change,
            note=body.note,
            detail={"reason": body.reason.value},
        )

    def cancel(
        self, experiment_id: str, body: ExperimentActionBody, *, auth: ReviewAuthMethod
    ) -> ExperimentView:
        """Abandon without a conclusion. Runs already assigned stay recorded."""

        def change(_: Session, row: ExperimentRow, now: datetime) -> None:
            self._require(
                row,
                ExperimentStatus.DRAFT,
                ExperimentStatus.APPROVED,
                ExperimentStatus.RUNNING,
                ExperimentStatus.STOPPED,
            )
            row.status = ExperimentStatus.CANCELLED.value
            row.cancelled_at = now

        return self._transition(
            experiment_id,
            _Actor(body.reviewer, auth),
            ExperimentEventKind.CANCELLED,
            change,
            note=body.note,
        )

    def unblind(
        self, experiment_id: str, body: UnblindBody, *, auth: ReviewAuthMethod
    ) -> ExperimentView:
        """Irreversible: the experiment can then only reach exploratory verdicts."""

        def change(_: Session, row: ExperimentRow, now: datetime) -> None:
            self._require(row, ExperimentStatus.RUNNING, ExperimentStatus.STOPPED)
            if row.peeked:
                raise ExperimentStateError(f"experiment {row.id} is already unblinded")
            row.peeked = True
            row.unblinded_at = now

        return self._transition(
            experiment_id,
            _Actor(body.reviewer, auth),
            ExperimentEventKind.UNBLINDED,
            change,
            note=body.reason,
        )

    # --- assignment ---------------------------------------------------------------------

    def create_run(
        self,
        experiment_id: str,
        body: ExperimentRunBody,
        *,
        auth: ReviewAuthMethod,
        now: datetime | None = None,
    ) -> ExperimentRunCreated:
        """Randomize, then insert the run and its assignment in one transaction. The run
        starts (and may call models and the research provider) only after the commit."""
        now = now or utc_now()
        if body.config.content_policy.content_intent is ContentIntent.GROK_QUESTION:
            # Phase 9: Grok questions are manual-only and occasional; they are not
            # randomized into experiments.
            raise InvalidExperimentError("Grok questions are excluded from experiments")
        try:
            with self._db.transaction() as session:
                row = xdb.get(session, experiment_id, lock=True)
                self._require(row, ExperimentStatus.RUNNING)
                design = ExperimentDesign.model_validate(row.design)
                if body.account.id != row.account_id:
                    raise InvalidExperimentError("account.id must be the experiment's account")
                end = xdb.planned_end(row, design)
                if end is not None and now >= end:
                    raise ExperimentStateError(
                        f"experiment {row.id} has reached its maximum duration; conclude it"
                    )
                self._require_base_active(session, row)
                arm_rows = xdb.arms(session, row.id)
                taken = xdb.assignments(session, row.id)
                if len(taken) >= design.runs_cap_per_arm * len(arm_rows):
                    raise ExperimentStateError(
                        f"experiment {row.id} has reached its assignment cap "
                        f"({design.runs_cap_per_arm} runs per arm)"
                    )
                block_no, position, arm = xdb.draw_slot(taken, arm_rows, self._rng)
                treatment = Treatment.model_validate(arm.treatment)
                config = body.config.model_copy(
                    update={
                        "content_policy": apply_treatment(body.config.content_policy, treatment)
                    }
                )
                strategy = ContentStrategy.model_validate(
                    sdb.get_version(session, row.base_version_id).content
                )
                run_id = self._runs.insert_run(session, body.account, strategy, config)
                session.flush()  # the run row first: the assignment references it
                assignment = ExperimentAssignmentRow(
                    id=new_id("asg"),
                    experiment_id=row.id,
                    arm_id=arm.id,
                    run_id=run_id,
                    block_no=block_no,
                    position=position,
                    assigned_by=body.reviewer,
                    auth_method=auth.value,
                    assigned_at=now,
                )
                session.add(assignment)
                session.flush()
                assignment_view = xdb.assignment_view(assignment, arm.label)
        except ValueError as exc:
            raise InvalidExperimentError(str(exc)) from None
        except IntegrityError:
            raise ExperimentStateError(
                "the randomization slot was taken concurrently; try again"
            ) from None
        self._runs.start_inserted(run_id)
        logger.info(
            "experiment run assigned",
            extra={"experiment_id": experiment_id, "run_id": run_id, "arm": arm.label},
        )
        return ExperimentRunCreated(assignment=assignment_view, run=self._runs.get_summary(run_id))

    # --- analysis and conclusion --------------------------------------------------------

    def analysis(self, experiment_id: str, *, now: datetime | None = None) -> ExperimentAnalysis:
        """Read-only. A concluded experiment returns its stored result; an open one is
        analysed at ``now`` with outcome summaries masked unless it was unblinded."""
        now = now or utc_now()
        with self._db.transaction() as session:
            row = xdb.get(session, experiment_id)
            result = xdb.result_of(session, row.id)
            if result is not None:
                return ExperimentAnalysis.model_validate(result.analysis)
            stage = AnalysisStage.UNBLINDED_INTERIM if row.peeked else AnalysisStage.MASKED_INTERIM
            return analyze(self._input(session, row, now, stage))

    def conclude(
        self,
        experiment_id: str,
        body: ExperimentActionBody,
        *,
        auth: ReviewAuthMethod,
        now: datetime | None = None,
    ) -> ExperimentView:
        """Idempotent: a concluded experiment returns its stored result unchanged."""
        now = now or utc_now()
        try:
            with self._db.transaction() as session:
                row = xdb.get(session, experiment_id, lock=True)
                if row.status == ExperimentStatus.CONCLUDED.value:
                    return xdb.experiment_view(session, row)
                self._require(row, ExperimentStatus.RUNNING, ExperimentStatus.STOPPED)
                data = self._input(session, row, now, AnalysisStage.FINAL)
                analysis = analyze(data)
                design = data.design
                complete = all(f.analysable >= design.planned_n_per_arm for f in analysis.funnel)
                stopped = row.status == ExperimentStatus.STOPPED.value
                if not (complete or data.max_duration_reached or stopped):
                    raise ExperimentStateError(
                        "not yet: every arm needs its planned sample "
                        f"({design.planned_n_per_arm} analysable posts) or the maximum "
                        "duration must pass. Stop the experiment first to end it early "
                        "(the verdict is then insufficient_data below the planned sample)."
                    )
                assert analysis.verdict is not None and analysis.evidence_class is not None
                in_sha = input_digest(data)
                result = ExperimentResultRow(
                    id=new_id("xres"),
                    experiment_id=row.id,
                    verdict=analysis.verdict.value,
                    evidence_class=analysis.evidence_class.value,
                    peeked=row.peeked,
                    cutoff=now,
                    analysis_version=ANALYSIS_VERSION,
                    input_digest=in_sha,
                    digest=analysis_digest(in_sha, analysis),
                    analysis=analysis.model_dump(mode="json"),
                    flags=[f.value for f in analysis.flags],
                    concluded_by=body.reviewer,
                    auth_method=auth.value,
                    created_at=now,
                )
                session.add(result)
                row.status = ExperimentStatus.CONCLUDED.value
                row.concluded_at = now
                xdb.add_event(
                    session,
                    row.id,
                    ExperimentEventKind.CONCLUDED,
                    actor=body.reviewer,
                    auth_method=auth.value,
                    at=now,
                    reason=body.note,
                    detail={"verdict": analysis.verdict.value, "result_id": result.id},
                )
                session.flush()
                view = xdb.experiment_view(session, row)
        except IntegrityError:
            raise ExperimentStateError(
                f"experiment {experiment_id} was concluded concurrently"
            ) from None
        logger.info(
            "experiment concluded",
            extra={"experiment_id": experiment_id, "verdict": analysis.verdict.value},
        )
        return view

    def adopt(
        self,
        experiment_id: str,
        body: AdoptBody,
        *,
        auth: ReviewAuthMethod,
        now: datetime | None = None,
    ) -> StrategyVersionView:
        """A new, inactive strategy version from the winning arm. Never activates it."""
        now = now or utc_now()
        try:
            with self._db.transaction() as session:
                row = xdb.get(session, experiment_id, lock=True)
                result = xdb.result_of(session, row.id)
                if result is None:
                    raise AdoptionRefusedError(f"experiment {row.id} has no concluded result")
                blockers = xdb.adoption_blockers(session, row, result)
                if blockers:
                    raise AdoptionRefusedError("not adoptable: " + "; ".join(blockers))
                analysis = ExperimentAnalysis.model_validate(result.analysis)
                effect = next(
                    (e for e in analysis.effects or [] if e.variant == body.variant), None
                )
                if effect is None or effect.verdict is not Verdict.FAVOURS_VARIANT:
                    raise AdoptionRefusedError(
                        f"arm {body.variant} is not a variant whose own comparison favours it"
                    )
                arm = next(a for a in xdb.arms(session, row.id) if a.label == body.variant)
                base_row = sdb.get_version(session, row.base_version_id)
                base = ContentStrategy.model_validate(base_row.content)
                treatment = Treatment.model_validate(arm.treatment)
                if treatment.factor is ExperimentFactor.HOOK_TYPE:
                    assert treatment.hook_type is not None
                    hooks = [treatment.hook_type] + [
                        h for h in base.preferred_hooks if h != treatment.hook_type
                    ]
                    update: dict[str, object] = {"preferred_hooks": hooks}
                else:
                    update = {"length_guidance": treatment.length}
                strategy = base.model_copy(
                    update={**update, "version": sdb.next_version_number(session, base.id)}
                )
                strategy = ContentStrategy.model_validate(strategy.model_dump(mode="json"))
                version = sdb.insert_version(
                    session,
                    version_id=new_id("sv"),
                    strategy=strategy,
                    origin=StrategyVersionOrigin.EXPERIMENT,
                    parent_version_id=base_row.id,
                    created_by=body.reviewer,
                    auth_method=auth.value,
                    note=body.note or f"adopted from experiment {row.id} ({body.variant})",
                    source_experiment_result_id=result.id,
                )
                xdb.add_event(
                    session,
                    row.id,
                    ExperimentEventKind.ADOPTED,
                    actor=body.reviewer,
                    auth_method=auth.value,
                    at=now,
                    reason=body.note,
                    detail={"version_id": version.id, "variant": body.variant},
                )
                session.flush()
                return sdb.version_view(version, sdb.active_version_id(session, row.account_id))
        except IntegrityError:
            raise AdoptionRefusedError(
                f"experiment {experiment_id} was adopted concurrently"
            ) from None

    # --- reads ------------------------------------------------------------------------

    def get(self, experiment_id: str) -> ExperimentView:
        with self._db.transaction() as session:
            return xdb.experiment_view(session, xdb.get(session, experiment_id))

    def list_experiments(
        self, *, account_id: str | None, status: ExperimentStatus | None, limit: int
    ) -> list[ExperimentView]:
        with self._db.transaction() as session:
            return [
                xdb.experiment_view(session, r)
                for r in xdb.list_rows(session, account_id=account_id, status=status, limit=limit)
            ]

    def assignments(self, experiment_id: str) -> list[AssignmentView]:
        with self._db.transaction() as session:
            xdb.get(session, experiment_id)
            return xdb.assignment_views(session, experiment_id)

    def lineage(self, experiment_id: str) -> ExperimentLineageView:
        with self._db.transaction() as session:
            return xdb.lineage(session, xdb.get(session, experiment_id))

    def usage(self, experiment_id: str) -> ExperimentUsageView:
        """Usage of every assigned run (all of them, including failed and unpublished),
        per arm and in total, plus a projection for the runs the cap still allows."""
        with self._db.transaction() as session:
            row = xdb.get(session, experiment_id)
            design = ExperimentDesign.model_validate(row.design)
            arm_rows = xdb.arms(session, row.id)
            taken = xdb.assignments(session, row.id)
            per_arm: list[ArmUsageView] = []
            arm_usage: list[UsageCounts] = []
            finished: list[Decimal | None] = []
            missing_current: set[str] = set()
            for arm in arm_rows:
                counts: list[UsageCounts] = []
                total: Decimal | None = Decimal(0)
                missing: set[str] = set()
                for a in (t for t in taken if t.arm_id == arm.id):
                    run = repo.get_run_row(session, a.run_id)
                    own = repo.usage_counts(session, a.run_id)
                    counts.append(own)
                    stored = repo.run_price_list(session, run) or self._prices
                    at_run = estimate_cost(own, stored, basis="run_pricing")
                    missing.update(at_run.missing_prices)
                    total = None if total is None or at_run.total is None else total + at_run.total
                    if run.status in _FINISHED:
                        current = estimate_cost(own, self._prices, basis="current_pricing")
                        missing_current.update(current.missing_prices)
                        finished.append(current.total)
                merged = merge_usage(counts)
                arm_usage.append(merged)
                per_arm.append(
                    ArmUsageView(
                        arm=arm.label,
                        runs=len(counts),
                        usage=merged,
                        at_run_pricing_total=total,
                        run_pricing_missing=sorted(missing),
                    )
                )
            open_ = row.status in {s.value for s in _ASSIGNABLE}
            remaining = max(0, design.runs_cap_per_arm * len(arm_rows) - len(taken)) if open_ else 0
            known = [c for c in finished if c is not None]
            mean = (
                (sum(known, Decimal(0)) / len(known)).quantize(_PRECISION)
                if finished and len(known) == len(finished)
                else None
            )
            usage = merge_usage(arm_usage)
            return ExperimentUsageView(
                experiment_id=row.id,
                per_arm=per_arm,
                usage=usage,
                at_current_pricing=estimate_cost(usage, self._prices, basis="current_pricing"),
                projected=ProjectedCostView(
                    remaining_runs=remaining,
                    runs_with_usage=len(finished),
                    mean_cost_per_run=mean,
                    projected_remaining=None if mean is None else mean * remaining,
                    missing_prices=sorted(missing_current),
                ),
            )

    # --- internals ----------------------------------------------------------------------

    @staticmethod
    def _require(row: ExperimentRow, *allowed: ExperimentStatus) -> None:
        if row.status not in {s.value for s in allowed}:
            wanted = " or ".join(s.value for s in allowed)
            raise ExperimentStateError(f"experiment {row.id} is {row.status}, not {wanted}")

    @staticmethod
    def _require_base_active(session: Session, row: ExperimentRow) -> None:
        if sdb.active_version_id(session, row.account_id) != row.base_version_id:
            raise ExperimentStateError(
                "the account's active strategy is no longer the experiment's base version; "
                "create a new experiment"
            )

    def _transition(
        self,
        experiment_id: str,
        actor: _Actor,
        kind: ExperimentEventKind,
        change: _Change,
        *,
        note: str | None,
        detail: dict[str, object] | None = None,
    ) -> ExperimentView:
        now = utc_now()
        try:
            with self._db.transaction() as session:
                row = xdb.get(session, experiment_id, lock=True)
                change(session, row, now)
                xdb.add_event(
                    session,
                    row.id,
                    kind,
                    actor=actor.name,
                    auth_method=actor.auth.value,
                    at=now,
                    reason=note,
                    detail=dict(detail) if detail else None,
                )
                session.flush()
                view = xdb.experiment_view(session, row)
        except IntegrityError:
            raise ExperimentStateError(
                "another experiment is already approved or running for this account"
            ) from None
        logger.info("experiment " + kind.value, extra={"experiment_id": experiment_id})
        return view

    def _input(
        self, session: Session, row: ExperimentRow, cutoff: datetime, stage: AnalysisStage
    ) -> AnalysisInput:
        design = ExperimentDesign.model_validate(row.design)
        arm_inputs, facts = xdb.load_facts(session, row, cutoff)
        end = xdb.planned_end(row, design)
        return AnalysisInput(
            experiment_id=row.id,
            design=design,
            snapshot_age_seconds=row.snapshot_age_seconds,
            on_target_min_seconds=self._defaults.on_target_min_seconds,
            arms=arm_inputs,
            runs=facts,
            cutoff=cutoff,
            stage=stage,
            peeked=row.peeked,
            stopped_early=row.stop_reason is not None,
            max_duration_reached=end is not None and cutoff >= end,
        )
