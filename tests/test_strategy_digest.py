"""Phase 8 (D8): the Strategy Agent reads a compact digest and is validated against it."""

import json

import pytest

from social_growth_agent.agents.evidence_digest import digest_summary
from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.agents.strategy import PROMPT_VERSION, ProposalContext, StrategyAgent
from social_growth_agent.analytics.evaluation import evaluate
from social_growth_agent.errors import ProposalValidationError
from social_growth_agent.models import ContentStrategy, HookType
from social_growth_agent.models.strategy import Dimension, Metric, ProposalOutcome
from social_growth_agent.policies.strategy_changes import validate_proposal
from social_growth_agent.providers import LLMSettings
from tests.test_evaluation_stats import CUTOFF, config
from tests.test_strategy_proposals import dataset, draft, hooks_change, ref


@pytest.fixture
def base() -> ContentStrategy:
    return ContentStrategy(
        id="strat_a",
        account_id="acct_a",
        pillars=["agents"],
        tone="plain",
        target_audience="engineers",
        preferred_hooks=[HookType.HOW_TO],
    )


@pytest.fixture
def result():
    return evaluate(dataset(), config(), CUTOFF)


def _agent() -> StrategyAgent:
    return StrategyAgent(build_fake_llm(), LLMSettings(provider="fake", model="fake"))


def test_the_prompt_carries_the_compact_digest_only(result, base):
    digest, omitted = digest_summary(result.summary)
    request = _agent().request(ProposalContext("eval_x", digest, {"min_group_n": 3}, base, omitted))
    assert PROMPT_VERSION == "strategy-v2"
    assert set(request.payload) == {
        "evaluation_id",
        "evidence",
        "thresholds",
        "current_strategy",
        "allowed_fields",
        "hook_types",
        "max_post_length",
    }
    current = request.payload["current_strategy"]
    assert isinstance(current, dict)
    assert set(current) == {"id", "version", "preferred_hooks", "length_guidance"}
    body = request.prompt.split("\n", 1)[1]
    assert body == json.dumps(request.payload, separators=(",", ":"))  # compact JSON
    full = json.dumps(result.summary.model_dump(mode="json"), indent=1)
    assert len(body) * 3 < len(full)
    assert "pillars" not in request.prompt and "tone" not in request.prompt


def test_numbers_must_appear_in_the_digest_not_just_the_full_summary(result, base):
    change = hooks_change(result, expected_metric=Metric.ENGAGEMENT_RATE)
    full_ok = validate_proposal(draft(change), summary=result.summary, config=config(), base=base)
    assert full_ok.changes  # "40 likes against 20" is in the full summary
    digest, _ = digest_summary(result.summary)
    with pytest.raises(ProposalValidationError) as info:
        validate_proposal(draft(change), summary=digest, config=config(), base=base)
    assert any("numbers not found" in r and "40" in r for r in info.value.reasons)


def test_references_omitted_from_the_digest_are_unknown(result, base):
    digest, omitted = digest_summary(result.summary, max_chars=1_500)
    assert "posting-time (hour_block_utc) groups and comparisons" in omitted
    hour_ref = next(
        c.comparison_id
        for c in result.summary.comparisons
        if c.dimension is Dimension.HOUR_BLOCK_UTC
    )
    d = draft(
        posting_time_note="post in the morning as a hypothesis",
        posting_time_evidence_refs=[hour_ref],
    )
    with pytest.raises(ProposalValidationError) as info:
        validate_proposal(d, summary=digest, config=config(), base=base)
    assert any("unknown evidence reference" in r for r in info.value.reasons)


def test_the_fake_agent_proposes_from_the_digest_and_validates_against_it(result, base):
    digest, omitted = digest_summary(result.summary)
    context = ProposalContext("e", digest, {}, base, omitted)
    output = _agent().run(context).output
    proposal = validate_proposal(output, summary=digest, config=config(), base=base)
    assert proposal.changes[0].proposed_value == ["number"]
    assert proposal.changes[0].evidence_refs == [ref(result, "hook_type")]


def test_no_change_remains_a_valid_answer(result, base):
    digest, _ = digest_summary(result.summary)
    no_change = draft(summary="The evidence does not support a change.")
    assert no_change.outcome is ProposalOutcome.NO_CHANGE
    assert validate_proposal(no_change, summary=digest, config=config(), base=base).changes == []
