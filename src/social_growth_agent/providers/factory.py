"""Builds the configured providers. There is deliberately no automatic fallback:
if 'openai' or 'x' is configured and cannot be built, startup fails."""

from social_growth_agent.config import AppSettings
from social_growth_agent.providers.llm import LLMProvider
from social_growth_agent.providers.social import SocialPublisher, SocialResearchProvider


def build_llm_provider(settings: AppSettings) -> LLMProvider:
    if settings.llm_provider == "fake":
        from social_growth_agent.agents.fakes import build_fake_llm

        return build_fake_llm()
    from social_growth_agent.providers.openai_provider import OpenAIProvider

    return OpenAIProvider.from_api_key(
        settings.openai_api_key, timeout_seconds=settings.llm_timeout_seconds
    )


def build_research_provider(settings: AppSettings) -> SocialResearchProvider:
    if settings.research_provider == "mock":
        from social_growth_agent.providers.mocks import MockResearchProvider

        return MockResearchProvider()
    from social_growth_agent.providers.x import XResearchProvider

    return XResearchProvider.from_token(
        settings.x_bearer_token,
        timeout_seconds=settings.x_timeout_seconds,
        max_results_per_query=settings.x_max_results_per_query,
        max_queries_per_run=settings.x_max_queries_per_run,
    )


def build_publisher(settings: AppSettings) -> SocialPublisher:
    """'x' needs all four X_PUBLISH_* user-context credentials; X_BEARER_TOKEN is never
    used for publishing."""
    if settings.publisher_provider == "mock":
        from social_growth_agent.providers.mocks import MockPublisher

        return MockPublisher()
    from social_growth_agent.providers.x import XPublisher

    return XPublisher.from_settings(settings)
