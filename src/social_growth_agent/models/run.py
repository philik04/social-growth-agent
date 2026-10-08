"""Run-level records: status, errors, node execution events and LLM calls."""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from social_growth_agent.models.base import DomainModel, new_id, utc_now


class RunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"
    # Service-level states (Phase 4). The graph never sets these; the run service does.
    QUEUED = "queued"
    STALLED = "stalled"


class RunError(DomainModel):
    id: str = Field(default_factory=lambda: new_id("err"))
    node: str
    message: str
    generation_attempt: int = Field(ge=0)
    error_type: str = "RunError"
    occurred_at: datetime = Field(default_factory=utc_now)


class NodeEvent(DomainModel):
    """One node execution. Becomes an OpenTelemetry span in a later phase."""

    id: str = Field(default_factory=lambda: new_id("evt"))
    node: str
    generation_attempt: int = Field(ge=0)
    outcome: str
    duration_ms: float = Field(ge=0.0)
    started_at: datetime


class TokenUsage(DomainModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMCallOutcome(StrEnum):
    SUCCESS = "success"
    PROVIDER_ERROR = "provider_error"
    INVALID_OUTPUT = "invalid_output"


class ProviderErrorCategory(StrEnum):
    """Why an external provider call failed. Recorded on runs; drives no routing by itself."""

    AUTH = "auth"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK = "network"
    SERVER_ERROR = "server_error"
    BAD_REQUEST = "bad_request"
    MALFORMED_RESPONSE = "malformed_response"
    UNEXPECTED_STATUS = "unexpected_status"


class LLMCall(DomainModel):
    """Metadata for one structured LLM call (no prompt or output text is stored)."""

    id: str = Field(default_factory=lambda: new_id("llm"))
    agent: str
    task: str
    provider: str
    model: str
    generation_attempt: int = Field(ge=0)
    outcome: LLMCallOutcome
    latency_ms: float = Field(ge=0.0)
    usage: TokenUsage | None = None
    error_type: str | None = None
    started_at: datetime


class GraphRun(DomainModel):
    """Summary record of a run, the row a future ``graph_runs`` table would hold."""

    id: str
    account_id: str
    strategy_id: str
    strategy_version: int
    status: RunStatus
    generation_attempts: int
    started_at: datetime
