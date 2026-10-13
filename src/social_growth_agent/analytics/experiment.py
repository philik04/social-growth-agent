"""Experiment analysis (Phase 8). Pure: no I/O, no model, no platform call.

Input: the pre-registered design, the arms, and every randomized run with the facts
recorded up to a cutoff. Output: the per-arm funnel, balance, hook adherence, outcomes,
effect estimates with bootstrap intervals, flags and (for a final result) a verdict.

Rules that keep it honest:

- **One post per randomized run.** The first published post (by publication time) is
  analysed; further posts of the same run are listed and ignored, so a run cannot weigh
  more than another.
- **Age matching.** Phase 7's ``classify`` decides eligibility at the experiment's one
  snapshot age; synthetic and real snapshots never mix.
- **Masking.** While an experiment is open, outcome summaries are withheld unless it was
  explicitly unblinded, and an unblinded experiment can only ever reach an exploratory
  verdict.
- **Fixed horizon.** A confirmatory verdict needs every arm at its pre-registered sample.
  Reaching the maximum duration without it is ``insufficient_data``, never a lower bar.
- **Reproducible.** Facts are filtered by the cutoff, the bootstrap seed is derived from
  the input digest, and numbers are fixed-precision strings, so the same rows and cutoff
  give the same result and digest.
"""

import math
import random
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from typing import Any, Literal

from social_growth_agent.analytics.evaluation import (
    EvaluationConfig,
    PostInput,
    classify,
    fmt,
    hour_block,
    metric_value,
    sha256_json,
)
from social_growth_agent.models.experiment import (
    AnalysisStage,
    ArmAdherence,
    ArmBalance,
    ArmFunnel,
    ArmOutcome,
    EffectEstimate,
    EvidenceClass,
    ExperimentAnalysis,
    ExperimentDesign,
    ExperimentFactor,
    ExperimentObservation,
    HookAdherence,
    ResultFlag,
    Treatment,
    Verdict,
)
from social_growth_agent.models.strategy import Metric, Observation
from social_growth_agent.policies.treatment import hook_adherence

ANALYSIS_VERSION: Literal["expa-v1"] = "expa-v1"
_PRECISION = Decimal("0.000001")
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

MASKED_FIELDS = (
    "outcomes: per-arm engagements, impressions and rates",
    "effects: differences between arms and their intervals",
    "verdict",
    "observations[].observation: per-post counts",
)
NOT_MASKED_ELSEWHERE = (
    "GET /publications/{id}/metrics and GET /posts show each post's counts",
    "GET /experiments/{id}/assignments and GET /runs/{id} show each run's arm",
    "Phase 7 evaluations (POST /evaluations) describe the same posts by hook and length",
    "so the experiment is not blind to anyone who looks up individual posts; masking only "
    "keeps the experiment's own per-arm summaries out of view",
)


@dataclass(frozen=True)
class ArmInput:
    arm_id: str
    label: str
    is_control: bool
    treatment: Treatment


@dataclass(frozen=True)
class RunFacts:
    """One randomized run as recorded up to the cutoff: ``generated``, ``reviewed`` and
    ``approved`` come from rows created at or before it, so the facts (unlike the run's
    current status) do not change once the cutoff has passed."""

    assignment_id: str
    arm_id: str
    run_id: str
    assigned_at: datetime
    generated: bool
    reviewed: bool
    approved: bool
    posts: tuple[PostInput, ...]
    """Published posts (published_at at or before the cutoff), any order."""


@dataclass(frozen=True)
class AnalysisInput:
    experiment_id: str
    design: ExperimentDesign
    snapshot_age_seconds: int
    on_target_min_seconds: int
    arms: tuple[ArmInput, ...]
    runs: tuple[RunFacts, ...]
    cutoff: datetime
    stage: AnalysisStage
    peeked: bool
    stopped_early: bool
    max_duration_reached: bool


def input_digest(data: AnalysisInput) -> str:
    """Everything the analysis depends on, canonicalized."""
    return sha256_json(
        {
            "analysis_version": ANALYSIS_VERSION,
            "experiment_id": data.experiment_id,
            "design": data.design.model_dump(mode="json"),
            "snapshot_age_seconds": data.snapshot_age_seconds,
            "on_target_min_seconds": data.on_target_min_seconds,
            "arms": [
                [a.arm_id, a.label, a.is_control, a.treatment.model_dump(mode="json")]
                for a in data.arms
            ],
            "runs": [_run_key(r) for r in sorted(data.runs, key=lambda r: r.assignment_id)],
            "cutoff": data.cutoff.astimezone(UTC).isoformat(),
            "stage": data.stage.value,
            "peeked": data.peeked,
            "stopped_early": data.stopped_early,
            "max_duration_reached": data.max_duration_reached,
        }
    )


def _run_key(r: RunFacts) -> list[Any]:
    posts = []
    for p in sorted(r.posts, key=lambda p: p.publication_id):
        s = p.snapshot
        snap = (
            None
            if s is None
            else [
                s.post_metric_id,
                s.provider,
                s.captured_at.astimezone(UTC).isoformat(),
                None if s.provider_created_at is None else s.provider_created_at.isoformat(),
                s.likes,
                s.replies,
                s.reposts,
                s.quotes,
                s.bookmarks,
                s.impressions,
            ]
        )
        posts.append(
            [p.publication_id, p.published_at.astimezone(UTC).isoformat(), p.content, snap]
        )
    return [
        r.assignment_id,
        r.arm_id,
        r.run_id,
        r.assigned_at.astimezone(UTC).isoformat(),
        r.generated,
        r.reviewed,
        r.approved,
        posts,
    ]


def analysis_digest(input_sha: str, analysis: ExperimentAnalysis) -> str:
    return sha256_json({"input": input_sha, "analysis": analysis.model_dump(mode="json")})


# --- analysis -----------------------------------------------------------------------------


def analyze(data: AnalysisInput) -> ExperimentAnalysis:
    design = data.design
    metric = design.primary_metric
    config = EvaluationConfig(
        snapshot_age=design.snapshot_age,
        snapshot_age_seconds=data.snapshot_age_seconds,
        on_target_min_seconds=data.on_target_min_seconds,
        synthetic=design.synthetic,
    )
    labels = {a.arm_id: a.label for a in data.arms}
    treatments = {a.arm_id: a.treatment for a in data.arms}
    masked = data.stage is AnalysisStage.MASKED_INTERIM
    observations: list[ExperimentObservation] = []
    analysed: dict[str, list[tuple[RunFacts, PostInput, Observation]]] = defaultdict(list)
    for run in sorted(data.runs, key=lambda r: (r.assigned_at, r.assignment_id)):
        posts = sorted(
            (p for p in run.posts if p.published_at <= data.cutoff),
            key=lambda p: (p.published_at, p.publication_id),
        )
        first = posts[0] if posts else None
        obs = None
        if first is not None:
            group = f"{first.strategy_id}@v{first.strategy_version}"
            obs = classify(first, config, data.cutoff, group)
            analysed[run.arm_id].append((run, first, obs))
        treatment = treatments[run.arm_id]
        adherence = (
            hook_adherence(first.content, treatment.hook_type)
            if first is not None and treatment.hook_type is not None
            else None
        )
        observations.append(
            ExperimentObservation(
                assignment_id=run.assignment_id,
                arm=labels[run.arm_id],
                run_id=run.run_id,
                stage_at_cutoff=_stage(run, first is not None),
                publication_id=first.publication_id if first else None,
                extra_publication_ids=[p.publication_id for p in posts[1:]],
                eligible=obs.eligible if obs else None,
                exclusion_reason=(
                    obs.exclusion_reason.value if obs and obs.exclusion_reason else None
                ),
                observation=None if masked else obs,
                adherence=adherence,
                analysable=obs is not None and _rate(obs, metric) is not None,
            )
        )

    funnel = [_funnel(a, data, analysed[a.arm_id], metric) for a in data.arms]
    balance = [_balance(a, analysed[a.arm_id]) for a in data.arms]
    adherence_rows = (
        [_adherence(a, observations) for a in data.arms]
        if design.factor is ExperimentFactor.HOOK_TYPE
        else None
    )
    flags = _flags(data, funnel)
    outcomes = None if masked else [_outcome(a, analysed[a.arm_id], metric) for a in data.arms]
    effects = None
    verdict = None
    evidence = EvidenceClass.EXPLORATORY if data.peeked else None
    if not masked:
        seed = int(input_digest(data)[:16], 16)
        effects = _effects(data, analysed, metric, random.Random(seed))
    if data.stage is AnalysisStage.FINAL:
        assert effects is not None
        verdict, effects = _verdict(data, funnel, flags, effects)
        evidence = EvidenceClass.EXPLORATORY if data.peeked else EvidenceClass.CONFIRMATORY
    return ExperimentAnalysis(
        experiment_id=data.experiment_id,
        synthetic=design.synthetic,
        stage=data.stage,
        cutoff=data.cutoff.astimezone(UTC).isoformat(),
        masked_fields=list(MASKED_FIELDS) if masked else [],
        not_masked_elsewhere=list(NOT_MASKED_ELSEWHERE),
        funnel=funnel,
        balance=balance,
        adherence=adherence_rows,
        outcomes=outcomes,
        effects=effects,
        flags=flags,
        verdict=verdict,
        evidence_class=evidence,
        statements=statements(design, data.peeked),
        observations=observations,
    )


def _stage(
    run: RunFacts, published: bool
) -> Literal["assigned", "generated", "reviewed", "approved", "published"]:
    if published:
        return "published"
    if run.approved:
        return "approved"
    if run.reviewed:
        return "reviewed"
    return "generated" if run.generated else "assigned"


def _rate(obs: Observation, metric: Metric) -> Fraction | None:
    return metric_value(obs, metric) if obs.eligible else None


def _funnel(
    arm: ArmInput,
    data: AnalysisInput,
    rows: Sequence[tuple[RunFacts, PostInput, Observation]],
    metric: Metric,
) -> ArmFunnel:
    runs = [r for r in data.runs if r.arm_id == arm.arm_id]
    reasons = Counter(
        o.exclusion_reason.value for _, _, o in rows if o.exclusion_reason is not None
    )
    eligible = [o for _, _, o in rows if o.eligible]
    analysable = [o for o in eligible if _rate(o, metric) is not None]
    unrated = len(eligible) - len(analysable)
    if unrated:
        reasons["primary_metric_not_computable"] = unrated
    extra = sum(
        max(0, len([p for p in r.posts if p.published_at <= data.cutoff]) - 1) for r in runs
    )
    return ArmFunnel(
        arm=arm.label,
        assigned=len(runs),
        generated=sum(r.generated for r in runs),
        reviewed=sum(r.reviewed for r in runs),
        approved=sum(r.approved for r in runs),
        published=len(rows),
        snapshot_eligible=len(eligible),
        analysable=len(analysable),
        extra_publications=extra,
        excluded_by_reason=dict(sorted(reasons.items())),
    )


def _balance(arm: ArmInput, rows: Sequence[tuple[RunFacts, PostInput, Observation]]) -> ArmBalance:
    hours: Counter[str] = Counter()
    days: Counter[str] = Counter()
    origins: Counter[str] = Counter()
    delays: list[Fraction] = []
    for run, post, _ in rows:
        published = post.published_at.astimezone(UTC)
        hours[hour_block(published.hour, 4)] += 1
        days[_WEEKDAYS[published.weekday()]] += 1
        origins[post.origin] += 1
        seconds = int((post.published_at - run.assigned_at).total_seconds())
        delays.append(Fraction(seconds, 3600))
    return ArmBalance(
        arm=arm.label,
        hour_blocks_utc=dict(sorted(hours.items())),
        weekdays_utc={d: days[d] for d in _WEEKDAYS if days[d]},
        origins=dict(sorted(origins.items())),
        median_hours_assignment_to_publication=fmt(_median(delays)) if delays else None,
    )


def _adherence(arm: ArmInput, observations: Sequence[ExperimentObservation]) -> ArmAdherence:
    values = Counter(o.adherence for o in observations if o.arm == arm.label and o.adherence)
    return ArmAdherence(
        arm=arm.label,
        heuristic_consistent=values[HookAdherence.HEURISTIC_CONSISTENT],
        heuristic_inconsistent=values[HookAdherence.HEURISTIC_INCONSISTENT],
        not_checkable=values[HookAdherence.NOT_CHECKABLE],
    )


def _parts(
    rows: Sequence[tuple[RunFacts, PostInput, Observation]], metric: Metric
) -> list[tuple[int, int]]:
    """(numerator, impressions) per analysable post, in a stable order."""
    out = []
    for _, _post, obs in sorted(rows, key=lambda r: r[1].publication_id):
        rate = _rate(obs, metric)
        if rate is not None and obs.impressions:
            out.append((int(rate * obs.impressions), obs.impressions))
    return out


def _outcome(
    arm: ArmInput, rows: Sequence[tuple[RunFacts, PostInput, Observation]], metric: Metric
) -> ArmOutcome:
    parts = _parts(rows, metric)
    nums = sum(n for n, _ in parts)
    imps = sum(i for _, i in parts)
    rates = [Fraction(n, i) for n, i in parts]
    return ArmOutcome(
        arm=arm.label,
        n=len(parts),
        engagements=nums,
        impressions=imps,
        pooled_rate=fmt(Fraction(nums, imps)) if imps else None,
        mean_rate=fmt(sum(rates, Fraction(0)) / len(rates)) if rates else None,
        median_rate=fmt(_median(rates)) if rates else None,
    )


def _effects(
    data: AnalysisInput,
    analysed: dict[str, list[tuple[RunFacts, PostInput, Observation]]],
    metric: Metric,
    rng: random.Random,
) -> list[EffectEstimate]:
    control = next(a for a in data.arms if a.is_control)
    variants = [a for a in data.arms if not a.is_control]
    alpha = Decimal("0.05") / len(variants)  # Bonferroni across variant-vs-control
    level = str(Decimal(1) - alpha)
    resamples = data.design.bootstrap_resamples
    c_parts = _parts(analysed[control.arm_id], metric)
    effects = []
    for v in variants:
        v_parts = _parts(analysed[v.arm_id], metric)
        point = _pooled(v_parts) is not None and _pooled(c_parts) is not None
        diff = rel = med = None
        ci: tuple[str | None, str | None] = (None, None)
        mci: tuple[str | None, str | None] = (None, None)
        if point:
            pv, pc = _pooled(v_parts), _pooled(c_parts)
            assert pv is not None and pc is not None
            diff = fmt(pv - pc)
            rel = fmt((pv - pc) / pc) if pc else None
            med = fmt(
                _median([Fraction(n, i) for n, i in v_parts])
                - _median([Fraction(n, i) for n, i in c_parts])
            )
        if len(v_parts) >= 2 and len(c_parts) >= 2:
            ci, mci = _bootstrap(v_parts, c_parts, resamples, float(alpha), rng)
        effects.append(
            EffectEstimate(
                variant=v.label,
                control=control.label,
                metric=metric,
                n_variant=len(v_parts),
                n_control=len(c_parts),
                pooled_difference=diff,
                relative_lift=rel,
                ci_low=ci[0],
                ci_high=ci[1],
                confidence_level=level,
                median_difference=med,
                median_ci_low=mci[0],
                median_ci_high=mci[1],
                resamples=resamples,
                verdict=None,
            )
        )
    return effects


def _pooled(parts: Sequence[tuple[int, int]]) -> Fraction | None:
    imps = sum(i for _, i in parts)
    return Fraction(sum(n for n, _ in parts), imps) if imps else None


def _bootstrap(
    v: Sequence[tuple[int, int]],
    c: Sequence[tuple[int, int]],
    resamples: int,
    alpha: float,
    rng: random.Random,
) -> tuple[tuple[str | None, str | None], tuple[str | None, str | None]]:
    """Percentile intervals for the pooled-rate difference and the median difference,
    resampling posts with replacement within each arm."""
    v_rates = [n / i for n, i in v]
    c_rates = [n / i for n, i in c]
    pooled: list[float] = []
    medians: list[float] = []
    v_idx, c_idx = range(len(v)), range(len(c))
    for _ in range(resamples):
        vs = rng.choices(v_idx, k=len(v))
        cs = rng.choices(c_idx, k=len(c))
        v_imp = sum(v[j][1] for j in vs)
        c_imp = sum(c[j][1] for j in cs)
        pooled.append(sum(v[j][0] for j in vs) / v_imp - sum(c[j][0] for j in cs) / c_imp)
        medians.append(
            statistics.median(v_rates[j] for j in vs) - statistics.median(c_rates[j] for j in cs)
        )
    return _percentiles(pooled, alpha), _percentiles(medians, alpha)


def _percentiles(values: list[float], alpha: float) -> tuple[str | None, str | None]:
    xs = sorted(values)
    n = len(xs)
    lo = xs[max(0, math.floor(alpha / 2 * n))]
    hi = xs[min(n - 1, math.ceil((1 - alpha / 2) * n) - 1)]
    return _float(lo), _float(hi)


def _float(x: float) -> str:
    return str(Decimal(repr(x)).quantize(_PRECISION, rounding=ROUND_HALF_EVEN))


def _median(values: Sequence[Fraction]) -> Fraction:
    xs = sorted(values)
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


# --- flags and verdict --------------------------------------------------------------------


def _flags(data: AnalysisInput, funnel: Sequence[ArmFunnel]) -> list[ResultFlag]:
    design = data.design
    flags: list[ResultFlag] = []
    if any(f.analysable < design.planned_n_per_arm for f in funnel):
        flags.append(ResultFlag.BELOW_PLANNED_N)
    if data.max_duration_reached:
        flags.append(ResultFlag.MAX_DURATION_REACHED)
    if data.stopped_early:
        flags.append(ResultFlag.STOPPED_EARLY)
    if data.peeked:
        flags.append(ResultFlag.UNBLINDED_EARLY)
    shares = [Fraction(f.published, f.assigned) for f in funnel if f.assigned]
    if len(shares) == len(funnel) and max(shares) - min(shares) > Fraction(
        design.attrition_threshold_pp, 100
    ):
        flags.append(ResultFlag.DIFFERENTIAL_ATTRITION)
    for f in funnel:
        unrated = f.excluded_by_reason.get("primary_metric_not_computable", 0)
        if f.snapshot_eligible and Fraction(unrated, f.snapshot_eligible) > Fraction(
            design.missing_impressions_threshold_pct, 100
        ):
            flags.append(ResultFlag.IMPRESSIONS_MISSING)
            break
    if design.synthetic:
        flags.append(ResultFlag.SYNTHETIC)
    return flags


def _verdict(
    data: AnalysisInput,
    funnel: Sequence[ArmFunnel],
    flags: Sequence[ResultFlag],
    effects: list[EffectEstimate],
) -> tuple[Verdict, list[EffectEstimate]]:
    if ResultFlag.BELOW_PLANNED_N in flags or ResultFlag.IMPRESSIONS_MISSING in flags:
        overall = Verdict.INSUFFICIENT_DATA
        return overall, [e.model_copy(update={"verdict": overall}) for e in effects]
    if ResultFlag.DIFFERENTIAL_ATTRITION in flags:
        overall = Verdict.INCONCLUSIVE
        return overall, [e.model_copy(update={"verdict": overall}) for e in effects]
    favour_v = Verdict.EXPLORATORY_FAVOURS_VARIANT if data.peeked else Verdict.FAVOURS_VARIANT
    favour_c = Verdict.EXPLORATORY_FAVOURS_CONTROL if data.peeked else Verdict.FAVOURS_CONTROL
    judged = []
    for e in effects:
        if e.ci_low is None or e.ci_high is None:
            v = Verdict.INSUFFICIENT_DATA
        elif Decimal(e.ci_low) > 0:
            v = favour_v
        elif Decimal(e.ci_high) < 0:
            v = favour_c
        else:
            v = Verdict.INCONCLUSIVE
        judged.append(e.model_copy(update={"verdict": v}))
    verdicts = [e.verdict for e in judged]
    if favour_v in verdicts:
        overall = favour_v
    elif all(v is favour_c for v in verdicts):
        overall = favour_c
    elif Verdict.INSUFFICIENT_DATA in verdicts:
        overall = Verdict.INSUFFICIENT_DATA
    else:
        overall = Verdict.INCONCLUSIVE
    return overall, judged


def statements(design: ExperimentDesign, peeked: bool) -> list[str]:
    out = [
        "The comparison uses published posts only, not every randomized run. Humans review, "
        "approve and choose what to publish after randomization, so the compared groups can "
        "differ in ways randomization does not protect against (post-randomization "
        "selection bias).",
        f"The differential-attrition flag only catches gaps above "
        f"{design.attrition_threshold_pp} percentage points in the share of runs published; "
        "a smaller gap does not mean there is no selection bias.",
        f"Fixed horizon: a confirmatory verdict needs {design.planned_n_per_arm} analysable "
        "posts in every arm. Reaching the maximum duration without them is "
        "insufficient_data.",
        "Intervals are percentile bootstrap intervals over posts. With samples of a few dozen "
        "posts they are approximate and can be too narrow for skewed rates.",
        "Posting time is chosen by a human after assignment; the balance table shows it, "
        "nothing corrects for it.",
        "An interval that excludes zero is evidence, not proof: about one experiment in "
        "twenty shows such a difference by chance alone.",
        "Secondary metrics and other dimensions are exploratory; only the pre-registered "
        f"primary metric ({design.primary_metric.value} at {design.snapshot_age}) decides.",
    ]
    if design.factor is ExperimentFactor.HOOK_TYPE:
        out.append(
            "Hook arms enforce the DECLARED hook label. That does not establish that the "
            "published text genuinely uses the hook; the adherence counts are a heuristic "
            "estimate of that uncertainty, not verification."
        )
    if peeked:
        out.append(
            "Outcomes were unblinded before the planned sample was reached. Every conclusion "
            "of this experiment is exploratory and cannot be adopted as a strategy change."
        )
    if design.synthetic:
        out.append(
            "SYNTHETIC: these numbers come from generated demo data. They demonstrate the "
            "mechanics and are not evidence about real posts."
        )
    return out
