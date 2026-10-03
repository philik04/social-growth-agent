"""Builds the configured LLM provider. There is deliberately no automatic fallback:
if 'openai' is configured and cannot be built, startup fails."""

from social_growth_agent.config import AppSettings
from social_growth_agent.providers.llm import LLMProvider


def build_llm_provider(settings: AppSettings) -> LLMProvider:
    if settings.llm_provider == "fake":
        from social_growth_agent.agents.fakes import build_fake_llm

        return build_fake_llm()
    from social_growth_agent.providers.openai_provider import OpenAIProvider

    return OpenAIProvider.from_api_key(
        settings.openai_api_key, timeout_seconds=settings.llm_timeout_seconds
    )
