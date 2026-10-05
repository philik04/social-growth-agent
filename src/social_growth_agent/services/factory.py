"""Wires settings into dependencies. The only place that reads AppSettings for the graph."""

from social_growth_agent.config import AppSettings
from social_growth_agent.graph import AgentSettings, Dependencies
from social_growth_agent.providers import SocialResearchProvider
from social_growth_agent.providers.factory import build_llm_provider, build_research_provider


def build_dependencies(
    settings: AppSettings, research_provider: SocialResearchProvider | None = None
) -> Dependencies:
    """Raises ``ConfigurationError`` if the configured provider cannot be built."""
    return Dependencies(
        llm=build_llm_provider(settings),
        research_provider=research_provider or build_research_provider(settings),
        agent_settings=AgentSettings.from_app_settings(settings),
    )
