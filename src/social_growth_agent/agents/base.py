"""Shared plumbing for agents: timed LLM calls, call records, and output validation."""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

from pydantic import BaseModel, ValidationError

from social_growth_agent.errors import (
    AgentOutputError,
    InvalidModelOutputError,
    ProviderError,
)
from social_growth_agent.models import LLMCall, LLMCallOutcome, TokenUsage, utc_now
from social_growth_agent.providers import LLMProvider, LLMRequest


@cache
def load_prompt(name: str) -> str:
    """Load a versioned system prompt from ``agents/prompts/<name>.md``."""
    return files("social_growth_agent.agents.prompts").joinpath(f"{name}.md").read_text("utf-8")


@dataclass(frozen=True)
class Structured[T: BaseModel]:
    output: T
    call: LLMCall


def call_llm[T: BaseModel](llm: LLMProvider, request: LLMRequest, schema: type[T]) -> Structured[T]:
    """Call the provider and record an ``LLMCall`` for success or failure.

    On failure the record is attached to the raised error as ``llm_call`` so the
    graph can store it alongside the ``RunError``.
    """
    started_at = utc_now()
    start = time.perf_counter()

    def record(
        outcome: LLMCallOutcome,
        model: str,
        usage: TokenUsage | None = None,
        error: Exception | None = None,
    ) -> LLMCall:
        return LLMCall(
            agent=request.agent,
            task=request.task,
            provider=request.settings.provider,
            model=model,
            generation_attempt=request.generation_attempt,
            outcome=outcome,
            latency_ms=(time.perf_counter() - start) * 1000,
            usage=usage,
            error_type=None if error is None else type(error).__name__,
            started_at=started_at,
        )

    model = request.settings.model
    try:
        response = llm.generate_structured(request, schema)
    except ProviderError as exc:
        exc.llm_call = record(LLMCallOutcome.PROVIDER_ERROR, model, error=exc)
        raise
    except AgentOutputError as exc:
        exc.llm_call = record(LLMCallOutcome.INVALID_OUTPUT, model, error=exc)
        raise
    call = record(LLMCallOutcome.SUCCESS, response.model, usage=response.usage)
    return Structured(output=response.output, call=call)


@contextmanager
def validating_output(call: LLMCall) -> Iterator[None]:
    """Convert model output into domain objects; contract violations become
    ``AgentOutputError`` carrying the (now invalid) call record."""
    try:
        yield
    except (AgentOutputError, ValidationError) as exc:
        error = (
            exc
            if isinstance(exc, AgentOutputError)
            else InvalidModelOutputError(f"model output failed domain validation: {exc}")
        )
        error.llm_call = call.model_copy(
            update={"outcome": LLMCallOutcome.INVALID_OUTPUT, "error_type": type(error).__name__}
        )
        if error is exc:
            raise
        raise error from exc
