"""Application settings, read from environment variables (and an optional .env file).

Secrets are ``SecretStr`` so they never appear in reprs, logs or tracebacks.
"""

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_provider: Literal["openai", "fake"] = Field(
        default="openai",
        description="'fake' uses deterministic rule-based agents; it is never a silent fallback.",
    )
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4.1-mini"
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_temperature_enabled: bool = Field(
        default=True, description="Disable for models that reject a temperature parameter."
    )

    research_provider: Literal["mock", "x"] = Field(
        default="mock",
        description="'mock' uses synthetic fixtures; 'x' calls the X API. Never a silent fallback.",
    )
    x_bearer_token: SecretStr | None = None
    x_max_results_per_query: int = Field(
        default=10, ge=10, le=100, description="Posts per request; 10 is the API minimum."
    )
    x_max_queries_per_run: int = Field(
        default=1, ge=1, le=5, description="X requests per run, including any retries."
    )
    x_timeout_seconds: float = Field(default=10.0, gt=0)
