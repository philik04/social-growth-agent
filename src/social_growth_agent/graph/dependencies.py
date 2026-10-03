"""Everything the graph needs from the outside world, injected at build time.

Each agent gets its own ``LLMSettings`` and may get its own ``LLMProvider``
(``agent_llms``), so e.g. a cheaper critic model or a different vendor is a
configuration change that never touches graph logic.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from social_growth_agent.config import AppSettings
from social_growth_agent.providers import LLMProvider, LLMSettings, SocialResearchProvider

type AgentName = Literal["research", "content", "critic"]

_DEFAULT_TEMPERATURES: dict[AgentName, float] = {"research": 0.2, "content": 0.7, "critic": 0.0}


def _settings(agent: AgentName) -> LLMSettings:
    return LLMSettings(temperature=_DEFAULT_TEMPERATURES[agent])


@dataclass(frozen=True)
class AgentSettings:
    research: LLMSettings = field(default_factory=lambda: _settings("research"))
    content: LLMSettings = field(default_factory=lambda: _settings("content"))
    critic: LLMSettings = field(default_factory=lambda: _settings("critic"))

    @classmethod
    def from_app_settings(cls, app: AppSettings) -> "AgentSettings":
        """One provider/model for all agents (Phase 2 default); temperatures stay per agent."""
        model = app.openai_model if app.llm_provider == "openai" else "fake-deterministic"

        def build(agent: AgentName) -> LLMSettings:
            temperature = _DEFAULT_TEMPERATURES[agent] if app.llm_temperature_enabled else None
            return LLMSettings(provider=app.llm_provider, model=model, temperature=temperature)

        return cls(research=build("research"), content=build("content"), critic=build("critic"))

    def for_agent(self, agent: AgentName) -> LLMSettings:
        settings: dict[AgentName, LLMSettings] = {
            "research": self.research,
            "content": self.content,
            "critic": self.critic,
        }
        return settings[agent]


@dataclass(frozen=True)
class Dependencies:
    llm: LLMProvider
    research_provider: SocialResearchProvider
    agent_settings: AgentSettings = field(default_factory=AgentSettings)
    agent_llms: Mapping[AgentName, LLMProvider] = field(default_factory=dict)

    def llm_for(self, agent: AgentName) -> LLMProvider:
        return self.agent_llms.get(agent, self.llm)
