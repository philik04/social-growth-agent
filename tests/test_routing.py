"""Routing is a pure function of state; these tests pin the decision table."""

import pytest

from social_growth_agent.graph import GraphState, RunConfig
from social_growth_agent.graph.routing import (
    route_after_critique,
    route_after_generate,
    route_after_research,
    route_after_review,
)
from social_growth_agent.models import (
    Account,
    ContentStrategy,
    CritiqueVerdict,
    ReviewAction,
    ReviewDecision,
    ReviewState,
    RunStatus,
)
from social_growth_agent.policies import CriticGate
from tests.conftest import make_critique


def state_with(
    account: Account,
    strategy: ContentStrategy,
    *,
    attempts: int,
    verdicts: list[CritiqueVerdict],
    status: RunStatus = RunStatus.RUNNING,
    max_attempts: int = 3,
    gate: CriticGate | None = None,
    scores: list[float] | None = None,
) -> GraphState:
    critiques = [
        make_critique(f"c{i}", attempts, v, score=(scores or [0.8] * len(verdicts))[i])
        for i, v in enumerate(verdicts)
    ]
    return GraphState(
        account=account,
        strategy=strategy,
        config=RunConfig(max_generation_attempts=max_attempts, critic_gate=gate or CriticGate()),
        generation_attempts=attempts,
        critiques=critiques,
        status=status,
    )


P, R, X = CritiqueVerdict.PASS, CritiqueVerdict.REVISE, CritiqueVerdict.REJECT


@pytest.mark.parametrize(
    ("attempts", "verdicts", "expected"),
    [
        (1, [P], "request_review"),
        (1, [R, X, P], "request_review"),
        (1, [R, R], "generate"),
        (2, [X], "generate"),
        (3, [R, X], "failed"),
        (3, [P], "request_review"),
    ],
)
def test_route_after_critique(account, strategy, attempts, verdicts, expected):
    state = state_with(account, strategy, attempts=attempts, verdicts=verdicts)
    assert route_after_critique(state) == expected


def test_route_after_critique_only_considers_latest_attempt(account, strategy):
    state = state_with(account, strategy, attempts=2, verdicts=[R])
    old_pass = make_critique("old", 1, P)
    state = state.model_copy(update={"critiques": [old_pass, *state.critiques]})
    assert route_after_critique(state) == "generate"


def test_failed_status_short_circuits_every_router(account, strategy):
    state = state_with(account, strategy, attempts=1, verdicts=[P], status=RunStatus.FAILED)
    assert route_after_research(state) == "failed"
    assert route_after_generate(state) == "failed"
    assert route_after_critique(state) == "failed"


def test_routing_is_deterministic(account, strategy):
    state = state_with(account, strategy, attempts=1, verdicts=[R, P])
    assert {route_after_critique(state) for _ in range(20)} == {"request_review"}


def test_gate_requiring_two_passes_retries_on_one(account, strategy):
    gate = CriticGate(min_passing_candidates=2)
    state = state_with(account, strategy, attempts=1, verdicts=[P, R, R], gate=gate)
    assert route_after_critique(state) == "generate"
    state = state_with(account, strategy, attempts=1, verdicts=[P, P, R], gate=gate)
    assert route_after_critique(state) == "request_review"


def test_gate_best_score_threshold(account, strategy):
    gate = CriticGate(min_best_score=0.9)
    low = state_with(account, strategy, attempts=1, verdicts=[P], gate=gate, scores=[0.7])
    high = state_with(account, strategy, attempts=1, verdicts=[P], gate=gate, scores=[0.95])
    assert route_after_critique(low) == "generate"
    assert route_after_critique(high) == "request_review"


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (ReviewAction.EDIT, "critique_edit"),
        (ReviewAction.APPROVE, "__end__"),
        (ReviewAction.REJECT, "__end__"),
        (ReviewAction.REGENERATE, "__end__"),
    ],
)
def test_route_after_review_sends_edits_to_critique(account, strategy, action, expected):
    needs_id = action in (ReviewAction.APPROVE, ReviewAction.EDIT)
    decision = ReviewDecision(
        action=action,
        candidate_id="c0" if needs_id else None,
        edited_content="changed" if action is ReviewAction.EDIT else None,
        reviewer="p",
    )
    state = state_with(account, strategy, attempts=1, verdicts=[P])
    state = state.model_copy(update={"review": ReviewState(decision=decision)})
    assert route_after_review(state) == expected
