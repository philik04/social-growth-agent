"""Phase 8 types: pre-registered A/B experiments on hook type or post length.

Design, assignment, analysis and decisions are separate types:

- ``ExperimentDesign`` is the pre-registration: hypothesis, arms, primary metric,
  snapshot age, sample target, duration and stopping rules. It is fixed when the draft is
  created (drafts are not editable; a changed design is a new draft) and stored with its
  digest, so nothing can be "adjusted" after outcomes are seen.
- ``Treatment`` is what an arm imposes on a run: a hard, code-enforced constraint
  (declared hook type, or a length range), not a soft preference.
- ``ExperimentAnalysis`` is computed in code from stored rows. While an experiment is
  open its outcome summaries are masked; ``masked_fields`` says exactly which.
- Verdicts are deterministic. An early unblinding makes every later verdict
  ``exploratory_*``, which is never eligible for adoption.

Every number is a fixed-precision string, as in Phase 7.
"""

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from social_growth_agent.models.account import LengthGuidance
from social_growth_agent.models.base import DomainModel
from social_growth_agent.models.content import HookType
from social_growth_agent.models.strategy import RATE_METRICS, Metric, Observation

DESIGN_VERSION: Literal["exp-v1"] = "exp-v1"
MIN_PLANNED_N_PER_ARM = 30
"""Floor for the pre-registered sample per arm (posts with a computable primary metric)."""


class ExperimentFactor(StrEnum):
    HOOK_TYPE = "hook_type"
    LENGTH = "length"


class ExperimentStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    RUNNING = "running"
    STOPPED = "stopped"
    """No new assignments. Stopping never shortens the analysis plan: a conclusion still
    needs the planned sample or the maximum duration."""
    CONCLUDED = "concluded"
    CANCELLED = "cancelled"


OPEN_EXPERIMENT_STATUSES = (ExperimentStatus.APPROVED, ExperimentStatus.RUNNING)
"""At most one experiment per account in these states (partial unique index)."""


class StopReason(StrEnum):
    """Why assignment was stopped. Outcome-driven reasons do not exist on purpose."""

    OPERATIONAL = "operational"
    COST = "cost"
    CONTENT_SAFETY = "content_safety"
    OTHER = "other"


class Verdict(StrEnum):
    INSUFFICIENT_DATA = "insufficient_data"
    INCONCLUSIVE = "inconclusive"
    FAVOURS_VARIANT = "favours_variant"
    FAVOURS_CONTROL = "favours_control"
    EXPLORATORY_FAVOURS_VARIANT = "exploratory_favours_variant"
    """The interval excludes 0, but outcomes were unblinded before the planned sample:
    hypothesis-generating only, never eligible for adoption."""
    EXPLORATORY_FAVOURS_CONTROL = "exploratory_favours_control"


class EvidenceClass(StrEnum):
    CONFIRMATORY = "confirmatory"
    EXPLORATORY = "exploratory"


class ResultFlag(StrEnum):
    BELOW_PLANNED_N = "below_planned_n"
    MAX_DURATION_REACHED = "max_duration_reached"
    STOPPED_EARLY = "stopped_early"
    UNBLINDED_EARLY = "unblinded_early"
    DIFFERENTIAL_ATTRITION = "differential_attrition"
    IMPRESSIONS_MISSING = "impressions_missing"
    SYNTHETIC = "synthetic"


class HookAdherence(StrEnum):
    """A heuristic reading of published text against the declared hook. Never proof."""

    HEURISTIC_CONSISTENT = "heuristic_consistent"
    HEURISTIC_INCONSISTENT = "heuristic_inconsistent"
    NOT_CHECKABLE = "not_checkable"


class ExperimentEventKind(StrEnum):
    CREATED = "created"
    APPROVED = "approved"
    STARTED = "started"
    STOPPED = "stopped"
    CANCELLED = "cancelled"
    UNBLINDED = "unblinded"
    CONCLUDED = "concluded"
    ADOPTED = "adopted"


class AnalysisStage(StrEnum):
    MASKED_INTERIM = "masked_interim"
    UNBLINDED_INTERIM = "unblinded_interim"
    FINAL = "final"


# --- design -------------------------------------------------------------------------------


class Treatment(DomainModel):
    """Exactly one of ``hook_type`` or ``length``. Enforced per run as hard rules."""

    hook_type: HookType | None = None
    length: LengthGuidance | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.hook_type is None) == (self.length is None):
            raise ValueError("a treatment sets exactly one of hook_type or length")
        return self

    @property
    def factor(self) -> ExperimentFactor:
        return ExperimentFactor.HOOK_TYPE if self.hook_type else ExperimentFactor.LENGTH

    def describe(self) -> str:
        if self.hook_type is not None:
            return f"declared hook = {self.hook_type.value}"
        assert self.length is not None
        return f"length {self.length.min_chars}-{self.length.max_chars} characters"


class ArmSpec(DomainModel):
    label: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    is_control: bool = False
    treatment: Treatment


class ExperimentDesign(DomainModel):
    """The pre-registration. Every field is fixed before the first assignment."""

    design_version: Literal["exp-v1"] = DESIGN_VERSION
    factor: ExperimentFactor
    hypothesis: str = Field(min_length=1, max_length=1000)
    arms: list[ArmSpec] = Field(min_length=2, max_length=3)
    primary_metric: Metric = Metric.ENGAGEMENT_RATE
    snapshot_age: str = Field(default="24h", min_length=1, max_length=16)
    planned_n_per_arm: int = Field(default=50, ge=MIN_PLANNED_N_PER_ARM, le=10_000)
    """Posts per arm with a computable primary metric at the snapshot age."""
    max_runs_per_arm: int | None = Field(default=None, ge=1, le=100_000)
    """Assignment cap (spend bound). Default: twice the planned sample."""
    max_duration_days: int = Field(default=120, ge=7, le=365)
    confidence_level: Literal["0.95"] = "0.95"
    bootstrap_resamples: int = Field(default=10_000, ge=1_000, le=100_000)
    attrition_threshold_pp: int = Field(default=20, ge=5, le=50)
    missing_impressions_threshold_pct: int = Field(default=20, ge=5, le=50)
    synthetic: bool = False
    planning_posts_per_week: int = Field(default=5, ge=1, le=100)
    """Planning assumption for the feasibility table only; never used in the analysis."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.primary_metric not in RATE_METRICS:
            raise ValueError("primary_metric must be a rate metric")
        controls = [a for a in self.arms if a.is_control]
        if len(controls) != 1:
            raise ValueError("exactly one arm must be the control")
        labels = [a.label for a in self.arms]
        if len(set(labels)) != len(labels):
            raise ValueError("arm labels must be distinct")
        if any(a.treatment.factor is not self.factor for a in self.arms):
            raise ValueError(f"every arm must set the experiment factor ({self.factor.value})")
        treatments = [a.treatment.model_dump_json() for a in self.arms]
        if len(set(treatments)) != len(treatments):
            raise ValueError("arm treatments must differ")
        if self.max_runs_per_arm is not None and self.max_runs_per_arm < self.planned_n_per_arm:
            raise ValueError("max_runs_per_arm must be at least planned_n_per_arm")
        return self

    @property
    def runs_cap_per_arm(self) -> int:
        return self.max_runs_per_arm or 2 * self.planned_n_per_arm

    @property
    def control(self) -> ArmSpec:
        return next(a for a in self.arms if a.is_control)


# --- analysis -----------------------------------------------------------------------------


class ArmFunnel(DomainModel):
    """Every randomized run, counted at each stage (persisted in the concluded result)."""

    arm: str
    assigned: int
    generated: int
    """At least one candidate was generated."""
    reviewed: int
    """A human recorded at least one review decision."""
    approved: int
    """A human approved a candidate."""
    published: int
    """At least one post of the run is published (the first one is the analysed one)."""
    snapshot_eligible: int
    """The first published post has an age-matched, on-target snapshot."""
    analysable: int
    """... and the primary metric is computable (impressions reported and > 0)."""
    extra_publications: int
    """Further published posts of the same runs: listed, never analysed."""
    excluded_by_reason: dict[str, int]


class ArmBalance(DomainModel):
    """Covariates of analysed posts. Shown, never adjusted for."""

    arm: str
    hour_blocks_utc: dict[str, int]
    weekdays_utc: dict[str, int]
    origins: dict[str, int]
    median_hours_assignment_to_publication: str | None


class ArmAdherence(DomainModel):
    arm: str
    heuristic_consistent: int
    heuristic_inconsistent: int
    not_checkable: int


class ArmOutcome(DomainModel):
    arm: str
    n: int
    engagements: int
    impressions: int
    pooled_rate: str | None
    mean_rate: str | None
    median_rate: str | None


class EffectEstimate(DomainModel):
    variant: str
    control: str
    metric: Metric
    n_variant: int
    n_control: int
    pooled_difference: str | None
    """variant - control, pooled rates (sum of numerators / sum of impressions)."""
    relative_lift: str | None
    ci_low: str | None
    ci_high: str | None
    confidence_level: str
    """Bonferroni-adjusted when there are two variants."""
    median_difference: str | None
    median_ci_low: str | None
    median_ci_high: str | None
    resamples: int
    verdict: Verdict | None
    """Only in a final result; interim (unblinded) estimates carry no verdict."""


class ExperimentObservation(DomainModel):
    """One randomized run's analysed post (the first published one), or why there is none."""

    assignment_id: str
    arm: str
    run_id: str
    stage_at_cutoff: Literal["assigned", "generated", "reviewed", "approved", "published"]
    """The furthest funnel stage the run had reached at the analysis cutoff."""
    publication_id: str | None
    extra_publication_ids: list[str]
    eligible: bool | None
    exclusion_reason: str | None
    observation: Observation | None
    """Per-post counts; withheld (None) while outcomes are masked."""
    adherence: HookAdherence | None
    analysable: bool


class FeasibilityRow(DomainModel):
    posts_per_week: int
    eligible_share: str
    weeks_needed: int


class RunsNeededRow(DomainModel):
    publication_rate: str
    runs_needed: int


class PowerRow(DomainModel):
    relative_difference: str
    posts_per_arm: int


class Feasibility(DomainModel):
    """Planning arithmetic only. The coefficient of variation is an assumption."""

    planned_n_per_arm: int
    arms: int
    assumed_coefficient_of_variation: str
    minimum_detectable_relative_difference: str
    power_table: list[PowerRow]
    duration_table: list[FeasibilityRow]
    runs_needed_table: list[RunsNeededRow]
    planning_posts_per_week: int
    weeks_at_planning_assumption: int
    max_duration_weeks: str
    warnings: list[str]
    assumptions: list[str]


class ExperimentAnalysis(DomainModel):
    experiment_id: str
    synthetic: bool
    stage: AnalysisStage
    cutoff: str
    masked_fields: list[str]
    """Outcome summaries withheld in this response (empty once unblinded or final)."""
    not_masked_elsewhere: list[str]
    funnel: list[ArmFunnel]
    balance: list[ArmBalance]
    adherence: list[ArmAdherence] | None
    """Hook experiments only."""
    outcomes: list[ArmOutcome] | None
    effects: list[EffectEstimate] | None
    flags: list[ResultFlag]
    verdict: Verdict | None
    evidence_class: EvidenceClass | None
    statements: list[str]
    observations: list[ExperimentObservation]
