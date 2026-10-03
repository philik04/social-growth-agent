"""LLM provider abstraction.

Agents depend only on ``LLMProvider.generate_structured``. Provider, model,
temperature and structured-output strategy travel in ``LLMSettings``, so switching
model or provider is configuration plus (at most) one new adapter class.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from social_growth_agent.models import TokenUsage


class StructuredOutputStrategy(StrEnum):
    NATIVE = "native"  # provider-side JSON schema enforcement
    TOOL_CALL = "tool_call"  # schema expressed as a forced tool call
    JSON_PROMPT = "json_prompt"  # schema in the prompt, parsed and validated locally


class LLMSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = "fake"
    model: str = "fake-deterministic"
    temperature: float | None = Field(
        default=0.0, ge=0.0, le=2.0, description="None = do not send (reasoning models)."
    )
    max_output_tokens: int = Field(default=2048, ge=1)
    structured_output: StructuredOutputStrategy = StructuredOutputStrategy.NATIVE


class LLMRequest(BaseModel):
    """A single structured-generation request.

    ``payload`` holds the structured inputs that the prompt was rendered from.
    Real adapters ignore it (the prompt is authoritative); fakes and evaluations
    read it so they can behave deterministically without parsing prompt text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: str
    task: str = Field(description="Stable task name, e.g. 'critic.review'. Used for tracing.")
    system: str
    prompt: str
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    generation_attempt: int = Field(default=0, ge=0)
    settings: LLMSettings = Field(default_factory=LLMSettings)


@dataclass(frozen=True)
class LLMResponse[T: BaseModel]:
    output: T
    model: str
    usage: TokenUsage | None = None


class LLMProvider(Protocol):
    def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> LLMResponse[T]:
        """Return a validated instance of ``schema`` plus call metadata.

        Raises ``TransientProviderError`` for retryable failures, ``ProviderError``
        for non-retryable ones, and ``InvalidModelOutputError`` (or a subclass) when
        the output is missing or cannot be validated against ``schema``.
        """
        ...
