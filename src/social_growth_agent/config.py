"""Application settings, read from environment variables (and an optional .env file).

Secrets are ``SecretStr`` so they never appear in reprs, logs or tracebacks.
"""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
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

    database_url: SecretStr | None = Field(
        default=None,
        description="PostgreSQL URL for the run API. SecretStr: it may contain a password.",
    )
    pricing_file: Path = Field(
        default=Path("pricing.toml"), description="TOML price list used for cost estimates."
    )
    api_max_concurrent_runs: int = Field(
        default=2, ge=1, le=16, description="Worker threads executing runs in the API process."
    )

    # --- publishing (Phase 5) ---------------------------------------------------------
    # User-context credentials for POST /2/tweets (OAuth 1.0a). Deliberately separate
    # from X_BEARER_TOKEN, which is app-only and read-only.
    publisher_provider: Literal["mock", "x"] = Field(
        default="mock",
        description="'mock' records posts in memory; 'x' posts to X. Never a silent fallback.",
    )
    x_publish_api_key: SecretStr | None = Field(default=None, description="App consumer key.")
    x_publish_api_secret: SecretStr | None = Field(default=None, description="App secret.")
    x_publish_access_token: SecretStr | None = Field(
        default=None, description="Access token of the posting account (Read and write)."
    )
    x_publish_access_token_secret: SecretStr | None = None
    x_publish_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    publisher_poll_seconds: float = Field(default=5.0, gt=0, le=3600)
    publisher_lease_seconds: int = Field(
        default=60, ge=10, le=3600, description="How long a claimed publication stays leased."
    )
    publish_max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Attempts per publication; only failures proven to happen before the "
        "request was sent are retried automatically.",
    )
    publisher_embedded_worker: bool = Field(
        default=False,
        description="Run a publisher worker thread inside the API process (local "
        "development only). The standalone sga-publisher-worker is the canonical worker.",
    )
    publish_max_schedule_days: int = Field(default=30, ge=1, le=365)
    publish_past_tolerance_seconds: int = Field(
        default=300, ge=0, le=3600, description="A slightly past scheduled_for means 'now'."
    )

    @model_validator(mode="after")
    def _lease_outlives_the_call(self) -> Self:
        # A lease that expired while the HTTP call is still running would let the
        # sweeper mark the attempt unknown although its worker is alive.
        if self.publisher_lease_seconds < 2 * self.x_publish_timeout_seconds + 5:
            raise ValueError(
                "PUBLISHER_LEASE_SECONDS must be at least 2 x X_PUBLISH_TIMEOUT_SECONDS + 5"
            )
        return self
