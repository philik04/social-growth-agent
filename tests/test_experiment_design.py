"""Phase 8, pure: designs, treatments as hard rules, hook adherence, randomization,
feasibility and the compact Strategy Agent digest."""

import random
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import JsonValue, ValidationError

from social_growth_agent.agents.content import ContentAgent
from social_growth_agent.agents.evidence_digest import (
    MAX_DIGEST_CHARS,
    digest_summary,
    render,
    size_report,
)
from social_growth_agent.agents.fakes import fake_content
from social_growth_agent.analytics.evaluation import evaluate
from social_growth_agent.analytics.feasibility import feasibility, minimum_detectable, posts_per_arm
from social_growth_agent.models import (
    ContentCandidate,
    CritiqueVerdict,
    HookType,
    LengthGuidance,
    RunStatus,
)
from social_growth_agent.models.experiment import (
    ArmSpec,
    ExperimentDesign,
    ExperimentFactor,
    HookAdherence,
    Treatment,
)
from social_growth_agent.models.strategy import Dimension, EvaluationSummary, Metric
from social_growth_agent.persistence.experiments import draw_slot
from social_growth_agent.policies import ContentPolicy, find_violations
from social_growth_agent.policies.publishing import PublishFacts, publish_refusals
from social_growth_agent.policies.treatment import apply_treatment, hook_adherence
from social_growth_agent.providers import LLMRequest, LLMSettings
from tests.experiment_helpers import hook_design, length_design, three_arm_design
from tests.test_agents import candidate
from tests.test_evaluation_stats import CUTOFF, T0, config, post

# --- design -----------------------------------------------------------------------------


def test_a_design_needs_exactly_one_control_and_distinct_arms():
    number = Treatment(hook_type=HookType.NUMBER)
    question = Treatment(hook_type=HookType.QUESTION)
    base = {"factor": "hook_type", "hypothesis": "h"}
    with pytest.raises(ValidationError, match="exactly one arm must be the control"):
        ExperimentDesign.model_validate(
            base
            | {
                "arms": [
                    ArmSpec(label="a", treatment=number),
                    ArmSpec(label="b", treatment=question),
                ]
            }
        )
    with pytest.raises(ValidationError, match="labels must be distinct"):
        ExperimentDesign.model_validate(
            base
            | {
                "arms": [
                    ArmSpec(label="a", is_control=True, treatment=number),
                    ArmSpec(label="a", treatment=question),
                ]
            }
        )
    with pytest.raises(ValidationError, match="treatments must differ"):
        ExperimentDesign.model_validate(
            base
            | {
                "arms": [
                    ArmSpec(label="a", is_control=True, treatment=number),
                    ArmSpec(label="b", treatment=number),
                ]
            }
        )


def test_a_design_rejects_mixed_factors_small_samples_and_count_metrics():
    with pytest.raises(ValidationError, match="experiment factor"):
        hook_design(
            arms=[
                ArmSpec(label="a", is_control=True, treatment=Treatment(hook_type=HookType.NUMBER)),
                ArmSpec(
                    label="b",
                    treatment=Treatment(length=LengthGuidance(min_chars=10, max_chars=20)),
                ),
            ]
        )
    with pytest.raises(ValidationError):
        hook_design(planned_n_per_arm=29)  # below the pre-registration floor
    with pytest.raises(ValidationError, match="rate metric"):
        hook_design(primary_metric=Metric.LIKES)
    with pytest.raises(ValidationError, match="max_runs_per_arm"):
        hook_design(max_runs_per_arm=10)
    with pytest.raises(ValidationError):
        hook_design(max_duration_days=3)
    with pytest.raises(ValidationError):
        ArmSpec(label="Bad Label", treatment=Treatment(hook_type=HookType.NUMBER))


def test_a_treatment_sets_exactly_one_factor():
    with pytest.raises(ValidationError, match="exactly one"):
        Treatment()
    with pytest.raises(ValidationError, match="exactly one"):
        Treatment(hook_type=HookType.NUMBER, length=LengthGuidance(min_chars=1, max_chars=2))
    assert Treatment(hook_type=HookType.STORY).factor is ExperimentFactor.HOOK_TYPE
    assert Treatment(length=LengthGuidance(min_chars=5, max_chars=9)).describe() == (
        "length 5-9 characters"
    )


def test_runs_cap_defaults_to_twice_the_planned_sample():
    assert hook_design().runs_cap_per_arm == 60
    assert hook_design(max_runs_per_arm=45).runs_cap_per_arm == 45


# --- treatments as hard rules ----------------------------------------------------------


def test_a_hook_treatment_is_a_hard_rule_on_the_declared_label():
    policy = apply_treatment(ContentPolicy(), Treatment(hook_type=HookType.QUESTION))
    assert policy.required_hook_type is HookType.QUESTION
    declared_number = candidate("Is this a question? It reads like one.")  # declares number
    rules = [v.rule for v in find_violations(declared_number, policy)]
    assert rules == ["required_hook_type"]
    declared_question = declared_number.model_copy(update={"hook_type": HookType.QUESTION})
    assert find_violations(declared_question, policy) == []


def test_a_length_treatment_narrows_the_range_and_never_widens_it():
    policy = apply_treatment(
        ContentPolicy(), Treatment(length=LengthGuidance(min_chars=50, max_chars=100))
    )
    assert (policy.min_post_length, policy.max_post_length) == (50, 100)
    assert [v.rule for v in find_violations(candidate("x" * 49), policy)] == ["min_post_length"]
    assert [v.rule for v in find_violations(candidate("x" * 101), policy)] == ["max_post_length"]
    assert find_violations(candidate("x" * 75), policy) == []
    with pytest.raises(ValueError, match="exceeds the content policy maximum"):
        apply_treatment(
            ContentPolicy(), Treatment(length=LengthGuidance(min_chars=1, max_chars=281))
        )


def test_the_new_rules_are_no_ops_without_a_treatment_and_old_prompts_are_unchanged():
    policy = ContentPolicy()
    assert policy.prompt_payload() == {"max_post_length": 280, "allowed_formats": ["single"]}
    assert find_violations(candidate("x"), policy) == []
    treated = apply_treatment(policy, Treatment(hook_type=HookType.HOW_TO))
    assert treated.prompt_payload()["required_hook_type"] == "how_to"


def test_publishing_rechecks_the_treatment():
    policy = apply_treatment(ContentPolicy(), Treatment(hook_type=HookType.QUESTION))
    cand: ContentCandidate = candidate("not a question")
    facts = PublishFacts(
        run_status=RunStatus.APPROVED,
        candidate=cand,
        approved_candidate_id=cand.id,
        approval_count=1,
        review_candidate_ids=(cand.id,),
        latest_critique_verdict=CritiqueVerdict.PASS,
        content_policy=policy,
    )
    assert any("required_hook_type" in r for r in publish_refusals(facts))


def _content_request(policy: ContentPolicy) -> LLMRequest:
    payload: dict[str, JsonValue] = {
        "findings": [{"id": "find_1", "theme": "evals"}],
        "attempt": 2,
        "count": 3,
        "strategy": {"target_audience": "engineers"},
        "policy": policy.prompt_payload(),
    }
    return LLMRequest(
        agent=ContentAgent.name,
        task="content.generate",
        system="s",
        prompt="p",
        payload=payload,
        settings=LLMSettings(provider="fake", model="fake"),
    )


def test_the_fake_content_agent_is_unchanged_without_a_treatment():
    batch = fake_content(_content_request(ContentPolicy()))
    assert next(c.content for c in batch.candidates) == (
        "[2.1] What we learned about evals. Here's what worked."
    )
    assert len({c.hook_type for c in batch.candidates}) == 3


@pytest.mark.parametrize("hook", list(HookType))
def test_the_fake_content_agent_follows_a_hook_treatment(hook):
    policy = apply_treatment(ContentPolicy(), Treatment(hook_type=hook))
    batch = fake_content(_content_request(policy))
    assert {c.hook_type for c in batch.candidates} == {hook}


def test_the_fake_content_agent_follows_a_length_treatment():
    policy = apply_treatment(
        ContentPolicy(), Treatment(length=LengthGuidance(min_chars=180, max_chars=200))
    )
    for c in fake_content(_content_request(policy)).candidates:
        assert 180 <= len(c.content) <= 200


# --- hook adherence (a heuristic, never proof) -----------------------------------------


@pytest.mark.parametrize(
    ("hook", "text", "expected"),
    [
        (HookType.QUESTION, "Why do evals drift? Here is what we saw.", "heuristic_consistent"),
        (HookType.QUESTION, "Evals drift. We saw it twice.", "heuristic_inconsistent"),
        (HookType.QUESTION, "x" * 150 + " really?", "not_checkable"),
        (HookType.NUMBER, "3 reasons evals drift", "heuristic_consistent"),
        (HookType.NUMBER, "Reasons evals drift", "heuristic_inconsistent"),
        (HookType.HOW_TO, "How to keep evals honest", "heuristic_consistent"),
        (HookType.HOW_TO, "1. write the test\n2. run it", "heuristic_consistent"),
        (HookType.HOW_TO, "Evals matter", "not_checkable"),
        (HookType.STORY, "Last week we shipped", "not_checkable"),
        (HookType.CONTRARIAN, "Unpopular opinion: evals", "not_checkable"),
    ],
)
def test_hook_adherence_is_a_modest_heuristic(hook, text, expected):
    assert hook_adherence(text, hook) is HookAdherence(expected)


# --- randomization ------------------------------------------------------------------------


@dataclass
class _Arm:
    id: str


@dataclass
class _Taken:
    arm_id: str
    block_no: int


def _assign(arms: list[str], n: int, seed: int) -> list[tuple[int, int, str]]:
    rng = random.Random(seed)
    rows = [_Arm(a) for a in arms]
    taken: list[_Taken] = []
    out = []
    for _ in range(n):
        block, position, arm = draw_slot(taken, rows, rng)
        taken.append(_Taken(arm.id, block))
        out.append((block, position, arm.id))
    return out


@pytest.mark.parametrize("arms", [["a", "b"], ["a", "b", "c"]])
def test_permuted_blocks_balance_every_block(arms):
    drawn = _assign(arms, 12 * len(arms), seed=3)
    size = 2 * len(arms)
    slots = [(b, p) for b, p, _ in drawn]
    assert len(set(slots)) == len(slots)
    assert slots == [divmod(i, size) for i in range(len(drawn))]
    for block in range(12 // 2):
        counts = Counter(a for b, _, a in drawn if b == block)
        assert counts == dict.fromkeys(arms, 2)


def test_assignment_order_within_blocks_is_random_but_balanced_overall():
    firsts = Counter(_assign(["a", "b"], 1, seed=s)[0][2] for s in range(400))
    assert 160 < firsts["a"] < 240  # roughly half of first slots go to each arm
    sequences = {tuple(a for _, _, a in _assign(["a", "b"], 4, seed=s)) for s in range(200)}
    assert len(sequences) == 6  # all C(4,2) orderings of a balanced block occur


# --- feasibility -----------------------------------------------------------------------


def test_feasibility_tables_and_assumptions():
    report = feasibility(hook_design(planned_n_per_arm=50))
    assert report.assumed_coefficient_of_variation == "0.6"
    assert any("assumed, not measured" in a for a in report.assumptions)
    row = next(
        r for r in report.duration_table if r.posts_per_week == 5 and r.eligible_share == "0.8"
    )
    assert row.weeks_needed == 25  # 100 posts / (5 * 0.8)
    assert report.weeks_at_planning_assumption == 25
    assert [r.publication_rate for r in report.runs_needed_table] == ["0.9", "0.7", "0.5"]
    assert report.runs_needed_table[2].runs_needed == 250
    assert report.warnings and "insufficient_data" in report.warnings[0]


def test_feasibility_warns_when_the_cap_is_too_low():
    report = feasibility(
        hook_design(planned_n_per_arm=30, max_runs_per_arm=30, max_duration_days=365)
    )
    assert any("assignment cap" in w for w in report.warnings)
    assert not any("maximum duration" in w for w in report.warnings)


def test_power_arithmetic():
    assert posts_per_arm(Decimal("0.3"), Decimal("0.6"), 1) == 63
    assert posts_per_arm(Decimal("0.3"), Decimal("0.6"), 2) > 63  # Bonferroni
    assert minimum_detectable(50, Decimal("0.6"), 1) == Decimal("0.34")
    assert feasibility(three_arm_design()).arms == 3


def test_length_designs_validate_against_the_policy_maximum():
    design = length_design()
    for arm in design.arms:
        apply_treatment(ContentPolicy(), arm.treatment)


# --- Strategy Agent digest (D8) ---------------------------------------------------------


@pytest.fixture
def evaluation_summary() -> EvaluationSummary:
    """A busy account: 150 posts over five hooks, many lengths and hours, two strategy
    versions and two origins, so every dimension has groups and comparisons."""
    hooks = [h.value for h in HookType]
    posts = [
        post(
            i,
            hook=hooks[i % 5],
            length=40 + (i * 37) % 240,
            published=T0 + timedelta(hours=(i * 5) % 220),
            metrics={
                "likes": 5 + (i * 7) % 40,
                "replies": i % 6,
                "impressions": 900 + (i * 13) % 400,
            },
            strategy_version=1 + i % 2,
            origin="generated" if i % 4 else "human_edit",
        )
        for i in range(150)
    ]
    return evaluate(posts, config(), CUTOFF).summary


def test_digest_keeps_only_the_proposable_dimensions_and_rates(evaluation_summary):
    reduced, omitted = digest_summary(evaluation_summary)
    assert {g.dimension for g in reduced.groups} <= {
        Dimension.HOOK_TYPE,
        Dimension.LENGTH_BUCKET,
        Dimension.HOUR_BLOCK_UTC,
    }
    assert all(
        m.metric in {Metric.ENGAGEMENT_RATE, Metric.REPLY_RATE, Metric.REPOST_RATE}
        for g in reduced.groups
        for m in g.metrics
    )
    assert all(c.confounders == [] for c in reduced.comparisons)
    assert {c.comparison_id for c in reduced.comparisons} <= {
        c.comparison_id for c in evaluation_summary.comparisons
    }
    assert omitted == []
    sizes = size_report(evaluation_summary)
    assert sizes["digest_chars"] <= MAX_DIGEST_CHARS
    assert sizes["digest_chars"] * 5 < sizes["full_summary_chars"]


def test_digest_is_reduced_deterministically_to_its_budget(evaluation_summary):
    full, _ = digest_summary(evaluation_summary)
    reduced, omitted = digest_summary(evaluation_summary, max_chars=4_000)
    assert len(render(reduced, omitted)) <= 4_000
    assert omitted == [
        "descriptive-only groups",
        "posting-time (hour_block_utc) groups and comparisons",
        "comparisons with the smallest samples",
    ]
    assert not any(g.dimension is Dimension.HOUR_BLOCK_UTC for g in reduced.groups)
    assert 0 < len(reduced.comparisons) < len(full.comparisons)
    assert digest_summary(evaluation_summary, max_chars=4_000) == (reduced, omitted)
