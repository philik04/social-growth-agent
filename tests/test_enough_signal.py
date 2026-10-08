"""Phase 4 enough-signal loop: a deterministic post count after retrieval decides whether
to proceed, broaden the query (bounded by the request budget) or fail. Offline."""

import time

import pytest

from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.graph import Dependencies, RunConfig
from social_growth_agent.models import (
    FetchOutcome,
    ProviderErrorCategory,
    ResearchQuery,
    RunStatus,
    SignalDecision,
)
from social_growth_agent.policies import assess_signal, broadened_queries
from social_growth_agent.providers.mocks import MockResearchProvider
from social_growth_agent.services import WorkflowService
from tests.conftest import FAST_RETRY
from tests.x_fakes import RESET_EPOCH, FakeX, error, full_metrics, ok, search_body, x_post

QUERY = ResearchQuery(text='"AI agents" lang:en', recency_hours=24)


def posts_body(*ids: int):
    posts = [x_post(f"19{i:04d}", f"agents post {i}", f"u{i}", metrics=full_metrics()) for i in ids]
    users = [{"id": f"u{i}", "username": f"user_{i}", "name": "U"} for i in ids]
    return search_body(posts, users)


def run(fake, account, strategy, *, max_queries, min_posts=5, attempts=3):
    deps = Dependencies(
        llm=build_fake_llm(), research_provider=fake.provider(max_queries_per_run=max_queries)
    )
    config = RunConfig(
        research_query=QUERY, min_signal_posts=min_posts, max_research_attempts=attempts
    )
    return WorkflowService(deps, retry_policy=FAST_RETRY).start_run(account, strategy, config)


@pytest.fixture
def no_sleep(monkeypatch):
    def forbidden(_seconds):
        raise AssertionError("retrieval must never sleep")

    monkeypatch.setattr(time, "sleep", forbidden)


# --- policy ---------------------------------------------------------------------------


def test_broadened_queries_are_deterministic_and_keep_the_original_first(strategy):
    steps = broadened_queries(QUERY, strategy)
    assert steps[0] == QUERY
    assert [s.text for s in steps] == [
        '"AI agents" lang:en',
        '"AI agents" lang:en',
        "AI agents lang:en",
        '(AI agents lang:en) OR "agent engineering" OR "llm evaluation"',
    ]
    assert steps[1].recency_hours is None
    assert broadened_queries(QUERY, strategy) == steps
    assert QUERY.text == '"AI agents" lang:en'  # never mutated


@pytest.mark.parametrize(
    ("posts", "can_fetch", "can_broaden", "expected"),
    [
        (5, False, False, SignalDecision.PROCEED),
        (2, True, True, SignalDecision.BROADEN),
        (2, False, True, SignalDecision.PROCEED),
        (2, True, False, SignalDecision.PROCEED),
        (0, True, True, SignalDecision.BROADEN),
        (0, False, True, None),
    ],
)
def test_assess_signal(posts, can_fetch, can_broaden, expected):
    decision = assess_signal(
        posts=posts, min_posts=5, can_fetch_again=can_fetch, can_broaden=can_broaden
    )
    assert decision is expected


# --- graph ----------------------------------------------------------------------------


def test_thin_sample_is_broadened_within_budget(account, strategy, no_sleep):
    fake = FakeX(ok(posts_body(1, 2)), ok(posts_body(2, 3, 4, 5)))
    result = run(fake, account, strategy, max_queries=3)
    state = result.state

    assert result.status is RunStatus.AWAITING_REVIEW
    assert len(fake.requests) == 2
    assert "start_time" in fake.requests[0].url.params  # recency window on the original
    assert "start_time" not in fake.requests[1].url.params  # dropped by broadening
    assert [p.source_id for p in state.source_posts] == [
        f"x_19{i:04d}" for i in (1, 2, 3, 4, 5)
    ]  # merged, duplicate post 2 kept once
    assert [f.outcome for f in state.research_fetches] == [FetchOutcome.SUCCESS] * 2
    assert state.research_attempts == 2
    assert state.research_source is not None
    assert state.research_source.query == QUERY.text
    assert state.research_source.broadened_queries == [QUERY.text]


def test_broadening_never_exceeds_the_request_budget(account, strategy, no_sleep):
    fake = FakeX(ok(posts_body(1)))
    result = run(fake, account, strategy, max_queries=2, attempts=5)

    assert len(fake.requests) == 2
    assert sum(f.requests_made for f in result.state.research_fetches) == 2
    assert result.status is RunStatus.AWAITING_REVIEW  # thin but non-empty: proceeds
    assert result.state.research_brief is not None
    assert any("Small sample" in x for x in result.state.research_brief.limitations)


def test_single_query_budget_keeps_phase3_behaviour(account, strategy):
    fake = FakeX(ok(posts_body(1, 2)))
    result = run(fake, account, strategy, max_queries=1)

    assert len(fake.requests) == 1
    assert result.state.research_source is not None
    assert result.state.research_source.broadened_queries == []


def test_no_posts_after_budget_fails_with_every_fetch_recorded(account, strategy):
    fake = FakeX(ok(search_body([])))
    result = run(fake, account, strategy, max_queries=2)

    assert result.status is RunStatus.FAILED
    assert len(fake.requests) == 2
    assert [f.outcome for f in result.state.research_fetches] == [FetchOutcome.EMPTY] * 2
    assert result.state.errors[-1].error_type == "InsufficientSignalError"


def test_research_attempt_cap_bounds_providers_without_a_request_budget(account, strategy):
    provider = MockResearchProvider(posts=[])
    deps = Dependencies(llm=build_fake_llm(), research_provider=provider)
    config = RunConfig(max_research_attempts=3)
    result = WorkflowService(deps, retry_policy=FAST_RETRY).start_run(account, strategy, config)

    assert result.status is RunStatus.FAILED
    assert len(provider.calls) == 3


def test_rate_limit_is_not_retried_or_broadened_and_never_sleeps(account, strategy, no_sleep):
    fake = FakeX(error(429, **{"x-rate-limit-reset": str(RESET_EPOCH)}), ok(posts_body(1)))
    result = run(fake, account, strategy, max_queries=3)

    assert result.status is RunStatus.FAILED
    assert len(fake.requests) == 1
    [fetch] = result.state.research_fetches
    assert fetch.error_category is ProviderErrorCategory.RATE_LIMITED
    assert fetch.rate_limit_reset_at is not None


def test_transient_failure_is_retried_in_the_loop_within_budget(account, strategy, no_sleep):
    fake = FakeX(error(503), ok(posts_body(1, 2, 3, 4, 5)))
    result = run(fake, account, strategy, max_queries=2)

    assert result.status is RunStatus.AWAITING_REVIEW
    assert len(fake.requests) == 2
    assert [f.outcome for f in result.state.research_fetches] == [
        FetchOutcome.ERROR,
        FetchOutcome.SUCCESS,
    ]


def test_transient_failure_without_budget_fails_after_one_request(account, strategy):
    fake = FakeX(error(503), ok(posts_body(1)))
    result = run(fake, account, strategy, max_queries=1)

    assert result.status is RunStatus.FAILED
    assert len(fake.requests) == 1
    assert [f.outcome for f in result.state.research_fetches] == [FetchOutcome.ERROR]
