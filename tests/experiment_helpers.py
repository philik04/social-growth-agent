"""Pure builders for Phase 8 analysis tests: randomized runs with chosen outcomes."""

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from social_growth_agent.analytics.evaluation import PostInput, SnapshotInput
from social_growth_agent.analytics.experiment import AnalysisInput, ArmInput, RunFacts
from social_growth_agent.models import HookType, LengthGuidance
from social_growth_agent.models.experiment import (
    AnalysisStage,
    ArmSpec,
    ExperimentDesign,
    ExperimentFactor,
    Treatment,
)
from social_growth_agent.persistence.experiments import draw_slot
from social_growth_agent.services.experiment_seed import SeedOutcome, rate_outcome

T0 = datetime(2026, 6, 1, tzinfo=UTC)
DAY = 86_400


def hook_design(**overrides: Any) -> ExperimentDesign:
    body: dict[str, Any] = {
        "factor": ExperimentFactor.HOOK_TYPE,
        "hypothesis": "question beats number",
        "arms": [
            ArmSpec(
                label="number", is_control=True, treatment=Treatment(hook_type=HookType.NUMBER)
            ),
            ArmSpec(label="question", treatment=Treatment(hook_type=HookType.QUESTION)),
        ],
        "planned_n_per_arm": 30,
        "bootstrap_resamples": 1000,
        "synthetic": True,
    }
    return ExperimentDesign.model_validate(body | overrides)


def three_arm_design(**overrides: Any) -> ExperimentDesign:
    return hook_design(
        arms=[
            ArmSpec(
                label="number", is_control=True, treatment=Treatment(hook_type=HookType.NUMBER)
            ),
            ArmSpec(label="question", treatment=Treatment(hook_type=HookType.QUESTION)),
            ArmSpec(label="how_to", treatment=Treatment(hook_type=HookType.HOW_TO)),
        ],
        **overrides,
    )


def length_design(**overrides: Any) -> ExperimentDesign:
    body: dict[str, Any] = {
        "factor": ExperimentFactor.LENGTH,
        "hypothesis": "longer posts do better",
        "arms": [
            ArmSpec(
                label="short",
                is_control=True,
                treatment=Treatment(length=LengthGuidance(min_chars=60, max_chars=120)),
            ),
            ArmSpec(
                label="long",
                treatment=Treatment(length=LengthGuidance(min_chars=180, max_chars=260)),
            ),
        ],
        "planned_n_per_arm": 30,
        "bootstrap_resamples": 1000,
        "synthetic": True,
    }
    return ExperimentDesign.model_validate(body | overrides)


@dataclass
class _Arm:
    id: str
    label: str


@dataclass
class _Taken:
    arm_id: str
    block_no: int


Outcome = Callable[[str, int, random.Random], SeedOutcome]


def build_input(
    design: ExperimentDesign,
    outcome: Outcome | dict[str, float],
    *,
    runs: int = 64,
    seed: int = 1,
    stage: AnalysisStage = AnalysisStage.FINAL,
    peeked: bool = False,
    stopped_early: bool = False,
    max_duration_reached: bool = False,
    cutoff: datetime | None = None,
    provider: str = "mock_analytics",
    extra_post_every: int = 0,
    content: Callable[[str, int], str] | None = None,
) -> AnalysisInput:
    """``runs`` randomized runs, one every 12 hours from ``T0``; each published post gets
    an on-target 24h snapshot unless the outcome says otherwise."""
    rng = random.Random(seed)
    make = rate_outcome(outcome) if isinstance(outcome, dict) else outcome
    arm_rows = [_Arm(a.label, a.label) for a in design.arms]
    arms = tuple(
        ArmInput(arm_id=a.label, label=a.label, is_control=a.is_control, treatment=a.treatment)
        for a in design.arms
    )
    taken: list[_Taken] = []
    per_arm: dict[str, int] = {}
    facts = []
    for i in range(runs):
        block_no, _, arm = draw_slot(taken, arm_rows, rng)
        taken.append(_Taken(arm.id, block_no))
        index = per_arm.get(arm.label, 0)
        per_arm[arm.label] = index + 1
        result = make(arm.label, index, rng)
        assigned = T0 + timedelta(hours=12 * i)
        posts: list[PostInput] = []
        if result.generated and result.approved and result.published:
            text = content(arm.label, index) if content else f"post {i} " + "x" * 90
            posts.append(_post(f"pub_{i:04d}", f"run_{i:04d}", assigned, result, provider, text))
            if extra_post_every and i % extra_post_every == 0:
                posts.append(
                    _post(
                        f"pub_{i:04d}_extra",
                        f"run_{i:04d}",
                        assigned + timedelta(hours=1),
                        result,
                        provider,
                        text,
                    )
                )
        facts.append(
            RunFacts(
                assignment_id=f"asg_{i:04d}",
                arm_id=arm.id,
                run_id=f"run_{i:04d}",
                assigned_at=assigned,
                generated=result.generated,
                reviewed=result.generated,
                approved=result.generated and result.approved,
                posts=tuple(posts),
            )
        )
    return AnalysisInput(
        experiment_id="exp_test",
        design=design,
        snapshot_age_seconds=DAY,
        on_target_min_seconds=120,
        arms=arms,
        runs=tuple(facts),
        cutoff=cutoff or T0 + timedelta(hours=12 * runs + 72),
        stage=stage,
        peeked=peeked,
        stopped_early=stopped_early,
        max_duration_reached=max_duration_reached,
    )


def _post(
    pub_id: str,
    run_id: str,
    assigned: datetime,
    result: SeedOutcome,
    provider: str,
    text: str,
) -> PostInput:
    published = assigned + timedelta(hours=2)
    created = published - timedelta(seconds=2)
    m = result.metrics
    snapshot = (
        None
        if m is None
        else SnapshotInput(
            post_metric_id=f"pm_{pub_id}",
            provider=provider,
            captured_at=created + timedelta(seconds=DAY + 30),
            provider_created_at=created,
            likes=m.get("likes"),
            replies=m.get("replies"),
            reposts=m.get("reposts"),
            quotes=m.get("quotes"),
            bookmarks=m.get("bookmarks"),
            impressions=m.get("impressions"),
        )
    )
    return PostInput(
        publication_id=pub_id,
        run_id=run_id,
        candidate_id=f"cand_{run_id}",
        content=text,
        published_at=published,
        hook_type="number",
        origin="generated",
        format="single",
        strategy_id="strat_x",
        strategy_version=1,
        strategy_digest="d" * 64,
        snapshot=snapshot,
    )
