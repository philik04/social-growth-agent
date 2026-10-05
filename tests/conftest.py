from collections.abc import Callable

import httpx
import pytest
from langgraph.types import RetryPolicy

from social_growth_agent.agents import (
    CandidateBatch,
    CandidateEvaluation,
    CriticReport,
    IssueDraft,
    ResearchReport,
)
from social_growth_agent.agents.fakes import fake_content, fake_research
from social_growth_agent.errors import TransientProviderError
from social_growth_agent.graph import Dependencies
from social_growth_agent.models import (
    Account,
    ContentStrategy,
    Critique,
    CritiqueAssessment,
    CritiqueVerdict,
    IssueCategory,
    ResearchSource,
    RiskLevel,
    SourcePost,
    ToneMatch,
)
from social_growth_agent.policies import default_research_query
from social_growth_agent.providers import LLMRequest, SocialResearchProvider
from social_growth_agent.providers.mocks import (
    MockResearchProvider,
    ScriptedLLMProvider,
    payload_list,
)
from social_growth_agent.providers.mocks.llm import Handler
from social_growth_agent.services import WorkflowService

# Same retry semantics as production, without the waiting.
FAST_RETRY = RetryPolicy(
    max_attempts=3, initial_interval=0.0, jitter=False, retry_on=TransientProviderError
)

_CREDENTIAL_ENV = ("OPENAI_API_KEY", "X_BEARER_TOKEN", "LLM_PROVIDER", "RESEARCH_PROVIDER")


@pytest.fixture(autouse=True)
def offline(request, monkeypatch):
    """Normal tests never need credentials and never reach the network.

    Credentials from the environment are removed and real httpx transports are
    disabled; X tests use ``httpx.MockTransport``. ``live`` tests are exempt.
    """
    if request.node.get_closest_marker("live"):
        return
    for name in _CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)

    def blocked(self, request):
        raise RuntimeError(f"real network access in tests is disabled: {request.url.host}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)


type VerdictScript = Callable[[int, int], CritiqueVerdict]
"""(generation_attempt, candidate_index) -> verdict the *model* recommends"""


@pytest.fixture
def account() -> Account:
    return Account(id="acct_test", handle="@tester", niche="AI engineering")


@pytest.fixture
def strategy(account: Account) -> ContentStrategy:
    return ContentStrategy(
        id="strat_test",
        account_id=account.id,
        pillars=["agent engineering", "llm evaluation"],
        tone="practical",
        target_audience="engineers",
    )


def evaluation(
    candidate_id: str, verdict: CritiqueVerdict, score: float = 0.8
) -> CandidateEvaluation:
    passed = verdict is CritiqueVerdict.PASS
    return CandidateEvaluation(
        candidate_id=candidate_id,
        recommendation=verdict,
        score=score if passed else 0.3,
        tone_match=ToneMatch.STRONG,
        factual_risk=RiskLevel.LOW,
        originality_risk=RiskLevel.LOW,
        issues=[] if passed else [IssueDraft(category=IssueCategory.WEAK_HOOK, detail="weak hook")],
        suggested_revision=None if passed else "Lead with a concrete number.",
    )


def scripted_critic(script: VerdictScript) -> Callable[[LLMRequest], CriticReport]:
    """Critic whose recommendations are fully determined by attempt and candidate position."""

    def handler(request: LLMRequest) -> CriticReport:
        return CriticReport(
            evaluations=[
                evaluation(str(c["id"]), script(int(c["generation_attempt"]), i))
                for i, c in enumerate(payload_list(request, "candidates"))
            ]
        )

    return handler


def make_llm(
    script: VerdictScript,
    *,
    content: Handler = fake_content,
    fail_first: int = 0,
    **kwargs: object,
) -> ScriptedLLMProvider:
    return ScriptedLLMProvider(
        {
            ResearchReport: fake_research,
            CandidateBatch: content,
            CriticReport: scripted_critic(script),
        },
        fail_first=fail_first,
        **kwargs,  # type: ignore[arg-type]
    )


def make_service(
    llm: ScriptedLLMProvider, research: MockResearchProvider | None = None
) -> WorkflowService:
    deps = Dependencies(llm=llm, research_provider=research or MockResearchProvider())
    return WorkflowService(deps, retry_policy=FAST_RETRY)


def always(verdict: CritiqueVerdict) -> VerdictScript:
    return lambda _attempt, _index: verdict


def make_critique(
    candidate_id: str, attempt: int, verdict: CritiqueVerdict, score: float = 0.8
) -> Critique:
    return Critique(
        candidate_id=candidate_id,
        generation_attempt=attempt,
        verdict=verdict,
        recommended_verdict=verdict,
        assessment=CritiqueAssessment(
            score=score,
            factual_risk=RiskLevel.LOW,
            originality_risk=RiskLevel.LOW,
            tone_match=ToneMatch.STRONG,
        ),
    )


def research_material(
    strategy: ContentStrategy, provider: SocialResearchProvider | None = None
) -> tuple[list[SourcePost], ResearchSource]:
    """What the retrieve node hands the Research Agent, built from a provider."""
    provider = provider or MockResearchProvider()
    result = provider.search(default_research_query(strategy))
    source = ResearchSource(
        provider=provider.source_name,
        source_ids=[p.source_id for p in result.posts],
        synthetic=provider.synthetic,
        query=result.fetch.query,
        effective_query=result.fetch.effective_query,
        retrieved_at=result.fetch.started_at,
    )
    return result.posts, source
