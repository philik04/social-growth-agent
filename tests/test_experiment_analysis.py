"""Phase 8, pure: experiment analysis. Funnel, masking, bootstrap intervals, flags,
verdicts, determinism and calibration."""

import os
import random
import subprocess
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from social_growth_agent.analytics.experiment import (
    MASKED_FIELDS,
    NOT_MASKED_ELSEWHERE,
    _percentiles,
    analysis_digest,
    analyze,
    input_digest,
)
from social_growth_agent.models.experiment import (
    AnalysisStage,
    EvidenceClass,
    HookAdherence,
    ResultFlag,
    Verdict,
)
from social_growth_agent.services.experiment_seed import SeedOutcome, rate_outcome
from tests.experiment_helpers import (
    T0,
    build_input,
    hook_design,
    length_design,
    three_arm_design,
)

POSITIVE = {"number": 0.04, "question": 0.07}
NULL = {"number": 0.05, "question": 0.05}


# --- verdicts -------------------------------------------------------------------------


def test_a_clear_effect_at_the_planned_sample_favours_the_variant():
    result = analyze(build_input(hook_design(), POSITIVE))
    assert result.verdict is Verdict.FAVOURS_VARIANT
    assert result.evidence_class is EvidenceClass.CONFIRMATORY
    [effect] = result.effects or []
    assert effect.verdict is Verdict.FAVOURS_VARIANT
    assert Decimal(effect.ci_low or "0") > 0
    assert effect.n_variant >= 30 and effect.n_control >= 30
    assert effect.confidence_level == "0.95"
    assert result.flags == [ResultFlag.SYNTHETIC]


def test_the_reverse_effect_favours_the_control():
    result = analyze(build_input(hook_design(), {"number": 0.07, "question": 0.04}))
    assert result.verdict is Verdict.FAVOURS_CONTROL


def test_no_effect_is_inconclusive():
    result = analyze(build_input(hook_design(), NULL, seed=3))
    assert result.verdict is Verdict.INCONCLUSIVE
    [effect] = result.effects or []
    assert Decimal(effect.ci_low or "1") <= 0 <= Decimal(effect.ci_high or "-1")


def test_below_the_planned_sample_is_insufficient_data_whatever_the_numbers_say():
    result = analyze(build_input(hook_design(), {"number": 0.02, "question": 0.09}, runs=40))
    assert ResultFlag.BELOW_PLANNED_N in result.flags
    assert result.verdict is Verdict.INSUFFICIENT_DATA
    assert all(e.verdict is Verdict.INSUFFICIENT_DATA for e in result.effects or [])


def test_reaching_the_maximum_duration_never_lowers_the_bar():
    result = analyze(
        build_input(
            hook_design(), {"number": 0.02, "question": 0.09}, runs=40, max_duration_reached=True
        )
    )
    assert {ResultFlag.MAX_DURATION_REACHED, ResultFlag.BELOW_PLANNED_N} <= set(result.flags)
    assert result.verdict is Verdict.INSUFFICIENT_DATA


def test_stopping_early_is_flagged_and_does_not_shorten_the_plan():
    result = analyze(build_input(hook_design(), POSITIVE, runs=40, stopped_early=True))
    assert ResultFlag.STOPPED_EARLY in result.flags
    assert result.verdict is Verdict.INSUFFICIENT_DATA


def test_differential_attrition_makes_the_result_inconclusive() -> None:
    base = rate_outcome(POSITIVE)

    def outcome(label: str, index: int, rng: random.Random) -> SeedOutcome:
        if label == "question" and index % 2:
            return SeedOutcome(approved=False, published=False)
        return base(label, index, rng)

    result = analyze(build_input(hook_design(), outcome, runs=140))
    assert ResultFlag.DIFFERENTIAL_ATTRITION in result.flags
    assert result.verdict is Verdict.INCONCLUSIVE
    funnel = {f.arm: f for f in result.funnel}
    assert funnel["question"].approved == funnel["question"].published == 35
    assert funnel["number"].published == 70


def test_a_gap_below_the_attrition_threshold_is_not_flagged_but_still_disclosed() -> None:
    base = rate_outcome(POSITIVE)

    def outcome(label: str, index: int, rng: random.Random) -> SeedOutcome:
        if label == "question" and index % 10 == 0:
            return SeedOutcome(approved=False, published=False)
        return base(label, index, rng)

    result = analyze(build_input(hook_design(), outcome, runs=80))
    assert ResultFlag.DIFFERENTIAL_ATTRITION not in result.flags
    assert any("does not mean there is no selection bias" in s for s in result.statements)


def test_missing_impressions_make_the_result_insufficient() -> None:
    base = rate_outcome(POSITIVE)

    def outcome(label: str, index: int, rng: random.Random) -> SeedOutcome:
        result = base(label, index, rng)
        if index % 10 < 3 and result.metrics is not None:
            return SeedOutcome(metrics={**result.metrics, "impressions": None})
        return result

    result = analyze(build_input(hook_design(), outcome, runs=90))
    assert ResultFlag.IMPRESSIONS_MISSING in result.flags
    assert result.verdict is Verdict.INSUFFICIENT_DATA
    funnel = result.funnel[0]
    assert funnel.excluded_by_reason["primary_metric_not_computable"] > 0
    assert funnel.snapshot_eligible > funnel.analysable


def test_zero_impressions_are_never_a_zero_rate() -> None:
    base = rate_outcome(POSITIVE)

    def outcome(label: str, index: int, rng: random.Random) -> SeedOutcome:
        result = base(label, index, rng)
        if index == 0 and result.metrics is not None:
            return SeedOutcome(metrics={**result.metrics, "impressions": 0})
        return result

    result = analyze(build_input(hook_design(), outcome))
    assert all(
        f.excluded_by_reason.get("primary_metric_not_computable") == 1 for f in result.funnel
    )


def test_unblinding_makes_every_verdict_exploratory():
    result = analyze(build_input(hook_design(), POSITIVE, peeked=True))
    assert result.verdict is Verdict.EXPLORATORY_FAVOURS_VARIANT
    assert result.evidence_class is EvidenceClass.EXPLORATORY
    assert ResultFlag.UNBLINDED_EARLY in result.flags
    assert any("cannot be adopted" in s for s in result.statements)
    reverse = analyze(build_input(hook_design(), {"number": 0.07, "question": 0.04}, peeked=True))
    assert reverse.verdict is Verdict.EXPLORATORY_FAVOURS_CONTROL


def test_three_arms_use_bonferroni_intervals():
    design = three_arm_design()
    result = analyze(
        build_input(design, {"number": 0.04, "question": 0.07, "how_to": 0.04}, runs=96)
    )
    assert {e.confidence_level for e in result.effects or []} == {"0.975"}
    verdicts = {e.variant: e.verdict for e in result.effects or []}
    assert verdicts["question"] is Verdict.FAVOURS_VARIANT
    assert verdicts["how_to"] is Verdict.INCONCLUSIVE
    assert result.verdict is Verdict.FAVOURS_VARIANT


def test_length_experiments_have_no_adherence_table():
    result = analyze(build_input(length_design(), {"short": 0.05, "long": 0.05}))
    assert result.adherence is None
    assert not any("DECLARED hook" in s for s in result.statements)


# --- funnel, one post per run, cutoff -------------------------------------------------------


def test_only_the_first_published_post_of_a_run_is_analysed():
    result = analyze(build_input(hook_design(), POSITIVE, extra_post_every=4))
    extra = sum(f.extra_publications for f in result.funnel)
    assert extra == 16
    listed = [o for o in result.observations if o.extra_publication_ids]
    assert len(listed) == 16
    assert all(not o.publication_id.endswith("_extra") for o in listed if o.publication_id)
    assert sum(f.published for f in result.funnel) == 64


def test_the_funnel_counts_every_randomized_run():
    def outcome(label: str, index: int, rng: random.Random) -> SeedOutcome:
        stage = index % 4
        if stage == 0:
            return SeedOutcome(generated=False, approved=False, published=False)
        if stage == 1:
            return SeedOutcome(approved=False, published=False)
        if stage == 2:
            return SeedOutcome(published=False)
        return rate_outcome(NULL)(label, index, rng)

    result = analyze(build_input(hook_design(), outcome, runs=32))
    for f in result.funnel:
        assert (f.assigned, f.generated, f.reviewed, f.approved, f.published) == (16, 12, 12, 8, 4)
    stages = {o.stage_at_cutoff for o in result.observations}
    assert stages == {"assigned", "reviewed", "approved", "published"}


def test_the_cutoff_excludes_later_posts_and_snapshots():
    data = build_input(hook_design(), POSITIVE)
    cutoff = T0 + timedelta(hours=12 * 32 + 3)  # half of the runs published, last one unsnapped
    result = analyze(build_input(hook_design(), POSITIVE, cutoff=cutoff))
    assert sum(f.published for f in result.funnel) == 33
    reasons = {r for f in result.funnel for r in f.excluded_by_reason}
    assert "not_yet_due" in reasons
    assert input_digest(data) != input_digest(build_input(hook_design(), POSITIVE, cutoff=cutoff))


def test_real_snapshots_never_enter_a_synthetic_experiment():
    result = analyze(build_input(hook_design(), POSITIVE, provider="x"))
    assert all(f.analysable == 0 for f in result.funnel)
    assert result.verdict is Verdict.INSUFFICIENT_DATA


def test_balance_and_adherence_are_reported():
    def text(label: str, index: int) -> str:
        if label == "question":
            return (
                "What makes evals drift? " + "x" * 80 if index % 3 else "Evals drift. " + "x" * 80
            )
        return "3 ways evals drift " + "x" * 80

    result = analyze(build_input(hook_design(), POSITIVE, content=text))
    adherence = {a.arm: a for a in result.adherence or []}
    assert adherence["question"].heuristic_inconsistent == 11
    assert adherence["question"].heuristic_consistent == 21
    assert adherence["number"].heuristic_consistent == 32
    assert all(o.adherence is not None for o in result.observations)
    assert all(b.hour_blocks_utc for b in result.balance)
    assert all(b.median_hours_assignment_to_publication == "2.000000" for b in result.balance)
    assert {o.adherence for o in result.observations} >= {HookAdherence.HEURISTIC_CONSISTENT}


# --- masking ------------------------------------------------------------------------------


def test_a_masked_interim_withholds_outcome_summaries():
    result = analyze(build_input(hook_design(), POSITIVE, stage=AnalysisStage.MASKED_INTERIM))
    assert result.masked_fields == list(MASKED_FIELDS)
    assert result.outcomes is None and result.effects is None and result.verdict is None
    assert all(o.observation is None for o in result.observations)
    assert result.funnel and result.balance  # not outcomes: always shown
    assert result.not_masked_elsewhere == list(NOT_MASKED_ELSEWHERE)
    assert any("not blind" in line for line in result.not_masked_elsewhere)


def test_an_unblinded_interim_shows_estimates_without_a_verdict():
    result = analyze(
        build_input(hook_design(), POSITIVE, stage=AnalysisStage.UNBLINDED_INTERIM, peeked=True)
    )
    assert result.masked_fields == []
    assert result.effects and result.effects[0].verdict is None
    assert result.verdict is None
    assert result.evidence_class is EvidenceClass.EXPLORATORY


# --- statements -----------------------------------------------------------------------------


def test_the_limitations_are_stated():
    result = analyze(build_input(hook_design(), POSITIVE))
    text = " ".join(result.statements)
    for phrase in (
        "post-randomization selection bias",
        "published posts only",
        "DECLARED hook label",
        "not verification",
        "insufficient_data",
        "approximate",
        "one experiment in twenty",
        "SYNTHETIC",
    ):
        assert phrase in text


# --- determinism ----------------------------------------------------------------------------


def test_the_same_input_gives_the_same_result_and_digest():
    a = build_input(hook_design(), POSITIVE)
    b = build_input(hook_design(), POSITIVE)
    first, second = analyze(a), analyze(b)
    assert first == second
    assert analysis_digest(input_digest(a), first) == analysis_digest(input_digest(b), second)


_SCRIPT = """
from tests.experiment_helpers import build_input, hook_design
from social_growth_agent.analytics.experiment import analysis_digest, analyze, input_digest
data = build_input(hook_design(), {"number": 0.04, "question": 0.07})
print(analysis_digest(input_digest(data), analyze(data)))
"""


def test_the_digest_is_identical_across_processes():
    data = build_input(hook_design(), POSITIVE)
    here = analysis_digest(input_digest(data), analyze(data))
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONHASHSEED": "12345",
        "PYTHONPATH": f"{root}{os.pathsep}{root / 'src'}",
    }
    out = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        env=env,
        cwd=root,
        check=True,
    )
    assert out.stdout.strip() == here


def test_percentile_indices():
    values = [float(i) for i in range(1000)]
    assert _percentiles(values, 0.05) == ("25.000000", "974.000000")


# --- calibration ------------------------------------------------------------------------------


@pytest.mark.parametrize(("rates", "low", "high"), [(NULL, 0.0, 0.12)])
def test_false_positive_rate_under_no_effect_is_near_nominal(rates, low, high):
    """120 simulated null experiments (fixed seeds): the share with a confirmatory
    verdict either way should be near 5%. The interval is approximate, not exact."""
    verdicts = [analyze(build_input(hook_design(), rates, seed=s)).verdict for s in range(120)]
    positives = sum(v in {Verdict.FAVOURS_VARIANT, Verdict.FAVOURS_CONTROL} for v in verdicts)
    assert low <= positives / len(verdicts) <= high
    assert Verdict.INCONCLUSIVE in verdicts
