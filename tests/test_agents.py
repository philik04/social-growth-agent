"""Agent contracts: provider output is validated and converted into domain objects."""

import pytest

from social_growth_agent.agents import (
    CandidateBatch,
    CandidateDraft,
    ContentAgent,
    CriticAgent,
    CriticReport,
    FindingDraft,
    GenerationContext,
    OpportunityDraft,
    ResearchAgent,
    ResearchReport,
)
from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.agents.research import SYNTHETIC_LIMITATION
from social_growth_agent.errors import AgentOutputError, InsufficientSignalError
from social_growth_agent.models import (
    ClaimType,
    Confidence,
    ContentCandidate,
    CritiqueVerdict,
    HookType,
    PostFormat,
    ResearchFinding,
)
from social_growth_agent.policies import ContentPolicy
from social_growth_agent.policies.research_policy import small_sample_limitation
from social_growth_agent.providers import LLMSettings
from social_growth_agent.providers.mocks import MockResearchProvider, ScriptedLLMProvider
from tests.conftest import always, make_critique, research_material, scripted_critic


def candidate(
    content: str, cid: str = "cand_1", fmt: PostFormat = PostFormat.SINGLE
) -> ContentCandidate:
    return ContentCandidate(
        id=cid,
        run_id="run_1",
        generation_attempt=1,
        strategy_id="strat_test",
        strategy_version=1,
        research_finding_ids=["find_1"],
        topic="agent engineering",
        hook_type=HookType.NUMBER,
        format=fmt,
        target_audience="engineers",
        content=content,
    )


# --- research -----------------------------------------------------------------------

REPORT = ResearchReport(
    topic="agent reliability",
    summary="Posts about measured latency wins do well.",
    findings=[
        FindingDraft(
            theme="latency wins",
            summary="Posts with concrete numbers showed higher engagement in the sample.",
            claim_type=ClaimType.OBSERVATION,
            evidence_source_ids=["src_001", "src_003"],
            signal_strength=0.8,
        )
    ],
    content_opportunities=[
        OpportunityDraft(angle="Share a benchmark", rationale="Numbers work", finding_indexes=[0])
    ],
    confidence=Confidence.MEDIUM,
    limitations=["Only five posts."],
)


def test_research_agent_converts_provider_output_into_domain_state(account, strategy):
    llm = ScriptedLLMProvider({ResearchReport: lambda _r: REPORT})
    posts, source = research_material(strategy)
    result = ResearchAgent(llm).run(account, strategy, posts, source)

    [finding] = result.findings
    assert isinstance(finding, ResearchFinding)
    assert finding.id.startswith("find_")  # ids assigned by the application, not the model
    assert finding.evidence_source_ids == ["src_001", "src_003"]
    assert finding.claim_type is ClaimType.OBSERVATION
    brief = result.brief
    assert brief.opportunities[0].finding_ids == [finding.id]
    assert brief.confidence is Confidence.MEDIUM
    assert brief.source.provider == "mock_fixtures"
    assert brief.source.synthetic is True
    assert set(brief.source.source_ids) == {"src_001", "src_002", "src_003", "src_005"}
    assert brief.limitations == [
        "Only five posts.",
        small_sample_limitation(4),
        SYNTHETIC_LIMITATION,
    ]
    request = llm.calls[0]
    assert "supplied by the application" in request.prompt
    assert "SYNTHETIC" in request.prompt
    assert "must not claim" in request.system


def test_research_agent_rejects_findings_citing_unsupplied_posts(account, strategy):
    bad = REPORT.model_copy(
        update={
            "findings": [REPORT.findings[0].model_copy(update={"evidence_source_ids": ["made_up"]})]
        }
    )
    agent = ResearchAgent(ScriptedLLMProvider({ResearchReport: lambda _r: bad}))
    with pytest.raises(AgentOutputError) as exc_info:
        agent.run(account, strategy, *research_material(strategy))
    assert exc_info.value.llm_call is not None
    assert exc_info.value.llm_call.outcome == "invalid_output"


def test_research_agent_rejects_opportunity_with_unknown_finding(account, strategy):
    bad = REPORT.model_copy(
        update={
            "content_opportunities": [
                OpportunityDraft(angle="a", rationale="r", finding_indexes=[3])
            ]
        }
    )
    agent = ResearchAgent(ScriptedLLMProvider({ResearchReport: lambda _r: bad}))
    with pytest.raises(AgentOutputError):
        agent.run(account, strategy, *research_material(strategy))


def test_research_agent_raises_when_nothing_supplied(account, strategy):
    _posts, source = research_material(strategy, MockResearchProvider(posts=[]))
    with pytest.raises(InsufficientSignalError):
        ResearchAgent(build_fake_llm()).run(account, strategy, [], source)


# --- content ------------------------------------------------------------------------


def generation_context(strategy, feedback=()) -> GenerationContext:
    finding = ResearchFinding(
        id="find_1",
        theme="latency",
        summary="s",
        evidence_source_ids=["src_001"],
        signal_strength=0.5,
    )
    return GenerationContext(
        run_id="run_1",
        strategy=strategy,
        findings=[finding],
        brief=None,
        policy=ContentPolicy(max_post_length=200),
        attempt=2 if feedback else 1,
        count=3,
        feedback=list(feedback),
    )


def draft(**overrides) -> CandidateDraft:
    base = {
        "content": "We cut p95 latency 40% by caching tool calls.",
        "topic": "latency",
        "hook_type": HookType.NUMBER,
        "format": PostFormat.SINGLE,
        "target_audience": "engineers",
        "research_finding_ids": ["find_1"],
        "rationale": "Concrete numbers perform well here.",
        "revises_candidate_id": None,
    }
    return CandidateDraft(**{**base, **overrides})


def test_content_agent_produces_candidates_with_structured_metadata(strategy):
    batch = CandidateBatch(
        candidates=[draft(), draft(hook_type=HookType.HOW_TO), draft(topic="evals")]
    )
    llm = ScriptedLLMProvider({CandidateBatch: lambda _r: batch})
    result = ContentAgent(llm).run(generation_context(strategy))

    assert len(result.candidates) == 3
    first = result.candidates[0]
    assert (first.run_id, first.generation_attempt, first.strategy_id) == ("run_1", 1, "strat_test")
    assert first.rationale == "Concrete numbers perform well here."
    assert [c.hook_type for c in result.candidates] == [
        HookType.NUMBER,
        HookType.HOW_TO,
        HookType.NUMBER,
    ]
    request = llm.calls[0]
    assert request.payload["policy"] == {"max_post_length": 200, "allowed_formats": ["single"]}
    assert request.generation_attempt == 1
    assert result.call.agent == "content"


def test_content_agent_links_revisions_to_previous_candidates(strategy):
    previous = candidate("old", cid="cand_old")
    feedback = [(previous, make_critique("cand_old", 1, CritiqueVerdict.REVISE))]
    batch = CandidateBatch(candidates=[draft(revises_candidate_id="cand_old")])
    llm = ScriptedLLMProvider({CandidateBatch: lambda _r: batch})

    result = ContentAgent(llm).run(generation_context(strategy, feedback))

    assert result.candidates[0].revises_candidate_id == "cand_old"
    assert llm.calls[0].payload["previous_critique"][0]["candidate_id"] == "cand_old"


@pytest.mark.parametrize(
    "bad",
    [
        CandidateBatch(candidates=[]),
        CandidateBatch(candidates=[draft()] * 4),
        CandidateBatch(candidates=[draft(research_finding_ids=["find_x"])]),
        CandidateBatch(candidates=[draft(research_finding_ids=[])]),
        CandidateBatch(candidates=[draft(revises_candidate_id="cand_never_existed")]),
    ],
)
def test_content_agent_rejects_contract_violations(strategy, bad):
    llm = ScriptedLLMProvider({CandidateBatch: lambda _r: bad})
    with pytest.raises(AgentOutputError):
        ContentAgent(llm).run(generation_context(strategy))


# --- critic -------------------------------------------------------------------------


def critic_with(script) -> CriticAgent:
    return CriticAgent(ScriptedLLMProvider({CriticReport: scripted_critic(script)}))


def test_critic_structured_result_is_applied(strategy):
    critiques = (
        critic_with(lambda _a, i: [CritiqueVerdict.PASS, CritiqueVerdict.REVISE][i])
        .run(strategy, [candidate("good", "a"), candidate("meh", "b")], ContentPolicy())
        .critiques
    )

    good, meh = critiques
    assert (good.candidate_id, good.verdict, good.passed) == ("a", CritiqueVerdict.PASS, True)
    assert good.assessment.score == 0.8
    assert (meh.verdict, meh.revision_required) == (CritiqueVerdict.REVISE, True)
    assert meh.issues[0].category == "weak_hook"
    assert meh.suggested_revision == "Lead with a concrete number."
    assert meh.policy_violations == []


def test_candidate_over_max_length_fails_even_when_model_passes_it(strategy):
    critic = critic_with(always(CritiqueVerdict.PASS))
    [critique] = critic.run(strategy, [candidate("x" * 281)], ContentPolicy()).critiques

    assert critique.recommended_verdict is CritiqueVerdict.PASS
    assert critique.verdict is CritiqueVerdict.REVISE
    assert not critique.passed
    assert [v.rule for v in critique.policy_violations] == ["max_post_length"]
    assert "281" in critique.policy_violations[0].detail
    assert critique.suggested_revision is not None


def test_max_length_comes_from_policy_not_a_constant(strategy):
    critic = critic_with(always(CritiqueVerdict.PASS))
    [critique] = critic.run(
        strategy, [candidate("x" * 281)], ContentPolicy(max_post_length=500)
    ).critiques
    assert critique.passed


def test_disallowed_format_is_a_hard_violation(strategy):
    critic = critic_with(always(CritiqueVerdict.PASS))
    [critique] = critic.run(
        strategy, [candidate("short", fmt=PostFormat.THREAD)], ContentPolicy()
    ).critiques
    assert critique.verdict is CritiqueVerdict.REVISE
    assert [v.rule for v in critique.policy_violations] == ["allowed_format"]


def test_model_reject_is_never_upgraded(strategy):
    critic = critic_with(always(CritiqueVerdict.REJECT))
    [critique] = critic.run(strategy, [candidate("x" * 300)], ContentPolicy()).critiques
    assert critique.verdict is CritiqueVerdict.REJECT


def test_critic_uses_its_configured_model_settings(strategy):
    llm = ScriptedLLMProvider({CriticReport: scripted_critic(always(CritiqueVerdict.PASS))})
    settings = LLMSettings(provider="openai", model="critic-model", temperature=0.0)
    result = CriticAgent(llm, settings).run(strategy, [candidate("ok")], ContentPolicy())
    assert llm.calls[0].settings.model == "critic-model"
    assert (result.call.provider, result.call.model) == ("openai", "critic-model")


def test_fake_critic_flags_unsupported_claims(strategy):
    critiques = (
        CriticAgent(build_fake_llm())
        .run(
            strategy,
            [
                candidate("This is guaranteed to work.", "a"),
                candidate("Here is what we measured.", "b"),
            ],
            ContentPolicy(),
        )
        .critiques
    )
    assert [c.verdict for c in critiques] == [CritiqueVerdict.REVISE, CritiqueVerdict.PASS]
