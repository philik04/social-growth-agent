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
    publish_backoff_base_seconds: int = Field(
        default=30,
        ge=1,
        le=3600,
        description="Wait before retrying a publish that provably never reached the "
        "platform (doubles per attempt, capped). Persisted as retry_not_before.",
    )
    publish_backoff_max_seconds: int = Field(default=600, ge=1, le=86_400)
    publish_max_schedule_days: int = Field(default=30, ge=1, le=365)
    publish_past_tolerance_seconds: int = Field(
        default=300, ge=0, le=3600, description="A slightly past scheduled_for means 'now'."
    )

    # --- analytics (Phase 6) ----------------------------------------------------------
    # Public metrics only, read with the app-only X_BEARER_TOKEN (never the publish keys).
    analytics_provider: Literal["mock", "x"] = Field(
        default="mock",
        description="'mock' returns synthetic metrics; 'x' reads GET /2/tweets with the "
        "app-only bearer token. Never a silent fallback.",
    )
    analytics_snapshot_ages: str = Field(
        default="1h,24h,72h",
        description="Comma-separated ages after publication, e.g. '1h,24h,72h' (s/m/h/d).",
    )
    analytics_enqueue_on_publish: bool = Field(
        default=True,
        description="Create the snapshot jobs when a publication is recorded as published. "
        "Older publications are only scheduled by an explicit backfill.",
    )
    analytics_poll_seconds: float = Field(default=30.0, gt=0, le=3600)
    analytics_lease_seconds: int = Field(default=60, ge=10, le=3600)
    analytics_max_attempts: int = Field(default=5, ge=1, le=20)
    analytics_batch_size: int = Field(
        default=50, ge=1, le=100, description="Due jobs per cycle; X allows 100 ids per call."
    )
    analytics_backoff_base_seconds: int = Field(default=60, ge=1, le=86_400)
    analytics_backoff_max_seconds: int = Field(default=3600, ge=1, le=7 * 86_400)

    # --- strategy evaluation (Phase 7) ---------------------------------------------------
    app_environment: Literal["development", "production"] = Field(
        default="production",
        description="'development' is required before unauthenticated strategy review can "
        "be enabled. Nothing else changes with it.",
    )
    strategy_review_token: SecretStr | None = Field(
        default=None,
        description="Bearer token for approving, rejecting, activating and registering "
        "strategy versions. Proves possession of the token, not who the reviewer is.",
    )
    strategy_review_allow_unauthenticated: bool = Field(
        default=False,
        description="Local development only: allow strategy review without a token when "
        "STRATEGY_REVIEW_TOKEN is unset. Requires APP_ENVIRONMENT=development; decisions "
        "are recorded with auth_method 'unauthenticated_local'.",
    )
    strategy_proposer: Literal["disabled", "fake", "openai"] = Field(
        default="disabled",
        description="Who drafts strategy proposals. 'disabled' (default) never calls a model; "
        "'openai' uses OPENAI_API_KEY and STRATEGY_LLM_MODEL. Never a silent fallback.",
    )
    strategy_llm_model: str | None = Field(
        default=None, description="Model for strategy proposals; defaults to OPENAI_MODEL."
    )
    eval_min_group_n: int = Field(default=5, ge=3, le=1000)
    eval_min_compare_n: int = Field(default=10, ge=5, le=1000)
    eval_min_total_n: int = Field(default=20, ge=10, le=10_000)
    eval_hour_block_hours: Literal[1, 2, 3, 4, 6, 8, 12, 24] = 4
    eval_length_bucket_edges: str = Field(
        default="100,200", description="Comma-separated upper edges of the length buckets."
    )

    @model_validator(mode="after")
    def _strategy_review_is_safe(self) -> Self:
        if self.strategy_review_allow_unauthenticated and self.app_environment != "development":
            raise ValueError(
                "STRATEGY_REVIEW_ALLOW_UNAUTHENTICATED requires APP_ENVIRONMENT=development"
            )
        token = self.strategy_review_token
        if token is not None and len(token.get_secret_value().strip()) < 16:
            raise ValueError("STRATEGY_REVIEW_TOKEN must be at least 16 characters")
        if self.eval_min_compare_n < self.eval_min_group_n:
            raise ValueError("EVAL_MIN_COMPARE_N must be at least EVAL_MIN_GROUP_N")
        self.length_bucket_edges()  # raises ValueError if invalid
        return self

    def length_bucket_edges(self) -> tuple[int, ...]:
        try:
            edges = tuple(int(p) for p in self.eval_length_bucket_edges.split(",") if p.strip())
        except ValueError:
            raise ValueError("EVAL_LENGTH_BUCKET_EDGES must be comma-separated integers") from None
        if any(e < 1 for e in edges) or list(edges) != sorted(set(edges)) or len(edges) > 10:
            raise ValueError("EVAL_LENGTH_BUCKET_EDGES must be 1-10 increasing positive integers")
        return edges

    @model_validator(mode="after")
    def _analytics_settings_are_consistent(self) -> Self:
        from social_growth_agent.policies.analytics import parse_snapshot_ages

        parse_snapshot_ages(self.analytics_snapshot_ages)  # raises ValueError if invalid
        if self.analytics_backoff_max_seconds < self.analytics_backoff_base_seconds:
            raise ValueError(
                "ANALYTICS_BACKOFF_MAX_SECONDS must be at least ANALYTICS_BACKOFF_BASE_SECONDS"
            )
        if self.publish_backoff_max_seconds < self.publish_backoff_base_seconds:
            raise ValueError(
                "PUBLISH_BACKOFF_MAX_SECONDS must be at least PUBLISH_BACKOFF_BASE_SECONDS"
            )
        if self.analytics_lease_seconds < 2 * self.x_timeout_seconds + 5:
            raise ValueError("ANALYTICS_LEASE_SECONDS must be at least 2 x X_TIMEOUT_SECONDS + 5")
        return self

    @model_validator(mode="after")
    def _lease_outlives_the_call(self) -> Self:
        # A lease that expired while the HTTP call is still running would let the
        # sweeper mark the attempt unknown although its worker is alive.
        if self.publisher_lease_seconds < 2 * self.x_publish_timeout_seconds + 5:
            raise ValueError(
                "PUBLISHER_LEASE_SECONDS must be at least 2 x X_PUBLISH_TIMEOUT_SECONDS + 5"
            )
        return self
