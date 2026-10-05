"""X research through the whole graph: provenance, failures, budget, credential isolation.

Offline: the X API is httpx.MockTransport, the LLM is the deterministic fake.
"""

import json

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from social_growth_agent.agents import ResearchAgent, ResearchReport
from social_growth_agent.agents.base import load_prompt
from social_growth_agent.agents.fakes import build_fake_llm, fake_research
from social_growth_agent.errors import AgentOutputError
from social_growth_agent.graph import DEFAULT_RETRY_POLICY, Dependencies, RunConfig
from social_growth_agent.graph.checkpointing import checkpoint_serializer
from social_growth_agent.models import (
    ClaimType,
    FetchOutcome,
    ProviderErrorCategory,
    ResearchQuery,
    RunStatus,
)
from social_growth_agent.policies import SYNTHETIC_LIMITATION, deterministic_limitations
from social_growth_agent.policies.research_policy import (
    missing_impressions_limitation,
    small_sample_limitation,
)
from social_growth_agent.providers.mocks import ScriptedLLMProvider
from social_growth_agent.services import WorkflowService
from tests.conftest import FAST_RETRY, research_material
from tests.x_fakes import (
    RESET_EPOCH,
    TOKEN,
    FakeX,
    error,
    full_metrics,
    ok,
    sample_body,
    search_body,
    x_post,
)

CONFIG = RunConfig(research_query=ResearchQuery(text="AI agents lang:en"))


def run_x(fake, account, strategy, *, llm=None, retry=FAST_RETRY, checkpointer=None, **limits):
    deps = Dependencies(llm=llm or build_fake_llm(), research_provider=fake.provider(**limits))
    service = WorkflowService(deps, retry_policy=retry, checkpointer=checkpointer)
    return service.start_run(account, strategy, CONFIG)


def node_path(result):
    return [e.node for e in result.state.events]


# --- 8. source ids stay stable through the workflow ----------------------------------


def test_source_ids_are_stable_from_x_response_to_candidates(account, strategy):
    result = run_x(FakeX(ok(sample_body(3))), account, strategy)
    state = result.state

    assert result.status is RunStatus.AWAITING_REVIEW
    supplied = [p.source_id for p in state.source_posts]
    assert supplied == ["x_180000", "x_180001", "x_180002"]
    assert state.research_source is not None
    assert state.research_source.source_ids == supplied
    assert state.research_brief is not None
    assert state.research_brief.source.source_ids == supplied
    assert state.research_brief.source.query == "AI agents lang:en"
    assert state.research_brief.source.effective_query == "AI agents lang:en -is:retweet"
    assert state.research_brief.source.synthetic is False
    finding_ids = {f.id for f in state.research}
    for finding in state.research:
        assert set(finding.evidence_source_ids) <= set(supplied)
    for candidate in state.candidates:
        assert set(candidate.research_finding_ids) <= finding_ids
    [fetch] = state.research_fetches
    assert (fetch.provider, fetch.requests_made, fetch.posts_fetched) == ("x", 1, 3)
    assert node_path(result)[:3] == ["retrieve", "research", "generate"]


# --- 9 / 10. provenance validation ----------------------------------------------------


def test_findings_reference_only_supplied_ids(account, strategy):
    posts, source = research_material(strategy, FakeX(ok(sample_body(4))).provider())
    result = ResearchAgent(build_fake_llm()).run(account, strategy, posts, source)

    cited = {i for f in result.findings for i in f.evidence_source_ids}
    assert cited
    assert cited <= {p.source_id for p in posts}


def fabricating(request):
    report = fake_research(request)
    bad = report.findings[0].model_copy(update={"evidence_source_ids": ["x_180000", "x_999"]})
    return report.model_copy(update={"findings": [bad, *report.findings[1:]]})


def test_fabricated_evidence_ids_are_rejected_by_the_agent(account, strategy):
    posts, source = research_material(strategy, FakeX(ok(sample_body(2))).provider())
    agent = ResearchAgent(ScriptedLLMProvider({ResearchReport: fabricating}))
    with pytest.raises(AgentOutputError, match="x_999"):
        agent.run(account, strategy, posts, source)


def test_fabricated_evidence_ids_fail_the_run_and_keep_the_fetch_record(account, strategy):
    llm = build_fake_llm()
    llm._handlers[ResearchReport] = fabricating
    result = run_x(FakeX(ok(sample_body(2))), account, strategy, llm=llm)

    assert result.status is RunStatus.FAILED
    [err] = result.state.errors
    assert (err.node, err.error_type) == ("research", "AgentOutputError")
    assert "x_999" in err.message
    assert result.state.research == []
    assert result.state.research_fetches[0].outcome is FetchOutcome.SUCCESS


# --- 6. rate limits in the graph: recorded, never slept on, never retried ------------


def test_rate_limit_fails_the_run_without_retry_or_sleep(account, strategy):
    fake = FakeX(error(429, **{"x-rate-limit-reset": str(RESET_EPOCH)}))
    # Production retry policy and a budget of 3: a retry would be possible, but
    # rate limits are not transient, so exactly one request is made.
    result = run_x(fake, account, strategy, retry=DEFAULT_RETRY_POLICY, max_queries_per_run=3)

    assert len(fake.requests) == 1
    assert result.status is RunStatus.FAILED
    assert node_path(result) == ["retrieve", "failed"]
    [err] = result.state.errors
    assert (err.node, err.error_type) == ("retrieve", "RateLimitedError")
    [fetch] = result.state.research_fetches
    assert fetch.error_category is ProviderErrorCategory.RATE_LIMITED
    assert fetch.rate_limit_reset_at is not None
    assert fetch.rate_limit_reset_at.isoformat() in err.message


# --- 7. empty results -----------------------------------------------------------------


def test_empty_x_results_fail_the_run_with_a_recorded_fetch(account, strategy):
    result = run_x(FakeX(ok({"meta": {"result_count": 0}})), account, strategy)

    assert result.status is RunStatus.FAILED
    [err] = result.state.errors
    assert (err.node, err.error_type) == ("retrieve", "InsufficientSignalError")
    [fetch] = result.state.research_fetches
    assert (fetch.outcome, fetch.posts_fetched) == (FetchOutcome.EMPTY, 0)
    assert result.state.llm_calls == []  # no LLM spend on nothing


# --- 14. the request budget bounds retries --------------------------------------------


@pytest.mark.parametrize(("budget", "expected_requests"), [(1, 1), (2, 2), (5, 3)])
def test_retrieval_retries_never_exceed_the_request_budget(
    account, strategy, budget, expected_requests
):
    fake = FakeX(error(503))
    result = run_x(fake, account, strategy, max_queries_per_run=budget)

    # FAST_RETRY allows 3 attempts; the budget caps it, and never raises it.
    assert len(fake.requests) == expected_requests
    assert result.status is RunStatus.FAILED
    assert result.state.errors[0].error_type == "TransientProviderError"


def test_research_llm_retry_does_not_refetch_from_x(account, strategy):
    fake = FakeX(ok(sample_body(2)))
    # The first LLM call (research) fails transiently, then succeeds on retry.
    llm = ScriptedLLMProvider(build_fake_llm()._handlers, fail_first=1)
    result = run_x(fake, account, strategy, llm=llm)

    assert result.status is RunStatus.AWAITING_REVIEW
    assert [c.agent for c in result.state.llm_calls][:1] == ["research"]
    assert len(llm.calls) >= 2
    assert len(fake.requests) == 1


def test_auth_failure_is_not_retried_and_never_falls_back_to_mock_data(account, strategy):
    fake = FakeX(error(401))
    result = run_x(fake, account, strategy, max_queries_per_run=3)

    assert len(fake.requests) == 1
    assert result.status is RunStatus.FAILED
    assert result.state.source_posts == []
    assert result.state.research_brief is None
    assert result.state.errors[0].error_type == "ProviderError"


# --- limitations (deterministic) ------------------------------------------------------


def test_live_sample_gets_small_sample_and_missing_impressions_limitations(account, strategy):
    body = search_body(
        [
            x_post("1", "AI agents in prod", "u1", metrics=full_metrics(impressions=None)),
            x_post("2", "AI agents evals", "u2", metrics=full_metrics()),
        ],
        users=[{"id": "u1", "username": "a"}, {"id": "u2", "username": "b"}],
    )
    result = run_x(FakeX(ok(body)), account, strategy)
    limitations = result.state.research_brief.limitations

    assert small_sample_limitation(2) in limitations
    assert missing_impressions_limitation(1, 2) in limitations
    assert SYNTHETIC_LIMITATION not in limitations


def test_deterministic_limitations_cover_size_impressions_and_synthetic():
    posts, _ = research_material_for_policy()
    assert deterministic_limitations(posts, synthetic=True) == [
        small_sample_limitation(len(posts)),
        SYNTHETIC_LIMITATION,
    ]
    many = [posts[0].model_copy(update={"source_id": f"s{i}"}) for i in range(20)]
    assert deterministic_limitations(many, synthetic=False) == []


def research_material_for_policy():
    from social_growth_agent.models import ContentStrategy

    strategy = ContentStrategy(
        account_id="a", pillars=["agent engineering"], tone="t", target_audience="a"
    )
    return research_material(strategy)


def test_research_prompt_separates_observation_hypothesis_and_causal_claims():
    prompt = load_prompt("research")
    for phrase in ("observation", "hypothesis", "causal", "in the supplied sample", "impressions"):
        assert phrase in prompt
    assert set(ClaimType) == {ClaimType.OBSERVATION, ClaimType.HYPOTHESIS}  # no causal option


# --- credentials never leave the provider ---------------------------------------------


def test_credentials_never_enter_state_checkpoints_or_prompts(account, strategy, caplog):
    caplog.set_level("DEBUG", logger="social_growth_agent")
    saver = InMemorySaver(serde=checkpoint_serializer())
    llm = build_fake_llm()
    result = run_x(FakeX(ok(sample_body(2))), account, strategy, llm=llm, checkpointer=saver)

    assert result.status is RunStatus.AWAITING_REVIEW
    assert TOKEN not in result.state.model_dump_json()
    assert TOKEN not in repr((saver.storage, saver.writes, saver.blobs))
    for request in llm.calls:
        assert TOKEN not in request.system + request.prompt
        assert TOKEN not in json.dumps(request.payload, default=str)
    for record in caplog.records:
        assert TOKEN not in str(record.__dict__)


def test_failed_run_errors_do_not_expose_credentials(account, strategy):
    for reply in (error(401), error(429), httpx.ReadTimeout("t"), httpx.ConnectError("c")):
        result = run_x(FakeX(reply), account, strategy)
        assert result.status is RunStatus.FAILED
        assert TOKEN not in result.state.model_dump_json()
