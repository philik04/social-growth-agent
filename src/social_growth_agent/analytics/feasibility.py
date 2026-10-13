"""Planning arithmetic for experiments (Phase 8): sample size, duration, runs needed.

Planning only: nothing here is used to analyse an experiment. The power figures use the
normal approximation for a difference in means of per-post rates, with an ASSUMED
coefficient of variation (spread of engagement rates between posts). Until enough real
posts exist to estimate it, the default of 0.6 is a labelled assumption, not a fact
about the account.
"""

import math
from decimal import ROUND_HALF_EVEN, Decimal

from social_growth_agent.models.experiment import (
    ExperimentDesign,
    Feasibility,
    FeasibilityRow,
    PowerRow,
    RunsNeededRow,
)

ASSUMED_CV = Decimal("0.6")
Z_POWER_80 = 0.841621
Z_TWO_SIDED = {1: 1.959964, 2: 2.241403}
"""Two-sided z for alpha 0.05, Bonferroni-adjusted for two variant comparisons."""
POSTS_PER_WEEK = (3, 5, 7, 10)
ELIGIBLE_SHARES = (Decimal("1.0"), Decimal("0.8"), Decimal("0.6"))
PUBLICATION_RATES = (Decimal("0.9"), Decimal("0.7"), Decimal("0.5"))
PLANNING_ELIGIBLE_SHARE = Decimal("0.8")
"""Share of published posts expected to have an on-target snapshot with impressions."""
RELATIVE_DIFFERENCES = (Decimal("0.2"), Decimal("0.3"), Decimal("0.4"), Decimal("0.5"))


def posts_per_arm(relative_difference: Decimal, cv: Decimal, variants: int) -> int:
    z = Z_TWO_SIDED[min(variants, 2)] + Z_POWER_80
    return math.ceil(2 * z * z * float(cv) ** 2 / float(relative_difference) ** 2)


def minimum_detectable(n_per_arm: int, cv: Decimal, variants: int) -> Decimal:
    z = Z_TWO_SIDED[min(variants, 2)] + Z_POWER_80
    return _q(Decimal(repr(math.sqrt(2 * z * z * float(cv) ** 2 / n_per_arm))))


def feasibility(design: ExperimentDesign, cv: Decimal = ASSUMED_CV) -> Feasibility:
    arms = len(design.arms)
    variants = arms - 1
    total = design.planned_n_per_arm * arms
    duration = [
        FeasibilityRow(
            posts_per_week=rate,
            eligible_share=str(share),
            weeks_needed=math.ceil(total / (rate * float(share))),
        )
        for rate in POSTS_PER_WEEK
        for share in ELIGIBLE_SHARES
    ]
    runs = [
        RunsNeededRow(
            publication_rate=str(rate),
            runs_needed=math.ceil(total / float(rate * PLANNING_ELIGIBLE_SHARE)),
        )
        for rate in PUBLICATION_RATES
    ]
    weeks = math.ceil(total / (design.planning_posts_per_week * float(PLANNING_ELIGIBLE_SHARE)))
    max_weeks = Decimal(design.max_duration_days) / 7
    warnings = []
    if weeks > max_weeks:
        warnings.append(
            f"At {design.planning_posts_per_week} published posts per week the planned sample "
            f"({design.planned_n_per_arm} per arm) needs about {weeks} weeks, longer than the "
            f"maximum duration of {design.max_duration_days} days: the experiment is likely "
            "to end as insufficient_data."
        )
    cap_runs = design.runs_cap_per_arm * arms
    needed_at_70 = math.ceil(total / float(Decimal("0.7") * PLANNING_ELIGIBLE_SHARE))
    if needed_at_70 > cap_runs:
        warnings.append(
            f"If only 70% of runs end in a published post, about {needed_at_70} runs are "
            f"needed, more than the assignment cap of {cap_runs} runs."
        )
    return Feasibility(
        planned_n_per_arm=design.planned_n_per_arm,
        arms=arms,
        assumed_coefficient_of_variation=str(cv),
        minimum_detectable_relative_difference=str(
            minimum_detectable(design.planned_n_per_arm, cv, variants)
        ),
        power_table=[
            PowerRow(relative_difference=str(d), posts_per_arm=posts_per_arm(d, cv, variants))
            for d in RELATIVE_DIFFERENCES
        ],
        duration_table=duration,
        runs_needed_table=runs,
        planning_posts_per_week=design.planning_posts_per_week,
        weeks_at_planning_assumption=weeks,
        max_duration_weeks=str(_q(max_weeks)),
        warnings=warnings,
        assumptions=[
            f"coefficient of variation {cv} between posts (assumed, not measured)",
            "80% power, two-sided alpha 0.05 (Bonferroni-adjusted with two variants)",
            f"{PLANNING_ELIGIBLE_SHARE} of published posts get an on-target snapshot with "
            "impressions",
            f"{design.planning_posts_per_week} published posts per week (planning assumption)",
        ],
    )


def _q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
