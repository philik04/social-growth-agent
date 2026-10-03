"""Provider and model are configuration; graph logic does not change with them."""

import pytest

from social_growth_agent.agents import CandidateBatch, CriticReport, ResearchReport
from social_growth_agent.agents.fakes import fake_content, fake_critic, fake_research
from social_growth_agent.config import AppSettings
from social_growth_agent.errors import ConfigurationError
from social_growth_agent.graph import AgentSettings, Dependencies
from social_growth_agent.models import RunStatus
from social_growth_agent.providers.mocks import MockResearchProvider, ScriptedLLMProvider
from social_growth_agent.providers.openai_provider import OpenAIProvider
from social_growth_agent.services import WorkflowService
from social_growth_agent.services.factory import build_dependencies
from tests.conftest import FAST_RETRY


def test_settings_come_from_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-custom")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "12.5")
    settings = AppSettings(_env_file=None)

    assert settings.llm_provider == "openai"
    assert settings.openai_model == "gpt-custom"
    assert settings.llm_timeout_seconds == 12.5
    assert "sk-from-env" not in repr(settings)  # secrets never appear in reprs


def test_agent_settings_follow_app_settings():
    agents = AgentSettings.from_app_settings(
        AppSettings(_env_file=None, llm_provider="openai", openai_model="gpt-custom")
    )
    assert {agents.for_agent(a).model for a in ("research", "content", "critic")} == {"gpt-custom"}
    assert agents.content.provider == "openai"
    assert (agents.critic.temperature, agents.content.temperature) == (0.0, 0.7)

    no_temp = AgentSettings.from_app_settings(
        AppSettings(_env_file=None, llm_temperature_enabled=False)
    )
    assert no_temp.critic.temperature is None


def test_openai_without_key_fails_at_startup_not_mid_run(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ConfigurationError):
        build_dependencies(AppSettings(_env_file=None, llm_provider="openai"))


def test_openai_provider_is_built_from_settings():
    deps = build_dependencies(
        AppSettings(_env_file=None, llm_provider="openai", openai_api_key="sk-test")
    )
    assert isinstance(deps.llm, OpenAIProvider)


def test_fake_provider_is_only_used_when_explicitly_configured(account, strategy):
    deps = build_dependencies(AppSettings(_env_file=None, llm_provider="fake"))
    assert isinstance(deps.llm, ScriptedLLMProvider)
    result = WorkflowService(deps, retry_policy=FAST_RETRY).start_run(account, strategy)
    assert result.status is RunStatus.AWAITING_REVIEW


def test_each_agent_can_use_its_own_provider_and_model(account, strategy):
    main = ScriptedLLMProvider({ResearchReport: fake_research, CandidateBatch: fake_content})
    critic_llm = ScriptedLLMProvider({CriticReport: fake_critic})
    agents = AgentSettings.from_app_settings(AppSettings(_env_file=None, llm_provider="fake"))
    agents = AgentSettings(
        research=agents.research,
        content=agents.content,
        critic=agents.critic.model_copy(update={"model": "cheap-critic"}),
    )
    deps = Dependencies(
        llm=main,
        research_provider=MockResearchProvider(),
        agent_settings=agents,
        agent_llms={"critic": critic_llm},
    )

    result = WorkflowService(deps, retry_policy=FAST_RETRY).start_run(account, strategy)

    assert result.status is RunStatus.AWAITING_REVIEW
    assert {r.task for r in main.calls} == {"research.synthesize", "content.generate"}
    assert {r.task for r in critic_llm.calls} == {"critic.review"}
    assert {r.settings.model for r in critic_llm.calls} == {"cheap-critic"}
    critic_calls = [c for c in result.state.llm_calls if c.agent == "critic"]
    assert {c.model for c in critic_calls} == {"cheap-critic"}
