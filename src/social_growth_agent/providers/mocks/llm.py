"""A scripted, deterministic LLM provider for tests and demos.

Each output schema is mapped to a handler that receives the ``LLMRequest`` and
returns an instance of that schema. Tests script exact behaviour (for example
"fail the critique on attempt 1, pass it on attempt 2"); the demo uses the
rule-based handlers in ``agents.fakes``. ``fail_first`` and ``failure`` simulate
provider errors for retry and failure-handling tests.
"""

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from pydantic import BaseModel

from social_growth_agent.errors import (
    AgentOutputError,
    InvalidModelOutputError,
    ProviderError,
    TransientProviderError,
)
from social_growth_agent.providers.llm import LLMRequest, LLMResponse

type Handler = Callable[[LLMRequest], BaseModel]


class ScriptedLLMProvider:
    def __init__(
        self,
        handlers: Mapping[type[BaseModel], Handler],
        *,
        fail_first: int = 0,
        failure: type[ProviderError] = TransientProviderError,
    ) -> None:
        self._handlers = dict(handlers)
        self._failures_left = fail_first
        self._failure = failure
        self.calls: list[LLMRequest] = []

    def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> LLMResponse[T]:
        self.calls.append(request)
        if self._failures_left > 0:
            self._failures_left -= 1
            raise self._failure("scripted provider failure")
        handler = self._handlers.get(schema)
        if handler is None:
            raise AgentOutputError(f"no scripted handler for schema {schema.__name__}")
        output = handler(request)
        if not isinstance(output, schema):
            raise InvalidModelOutputError(
                f"handler for {schema.__name__} returned {type(output).__name__}"
            )
        return LLMResponse(output=output, model=request.settings.model, usage=None)


def sequence(outputs: Iterable[BaseModel]) -> Handler:
    """Handler returning the given outputs in order, one per call."""
    queue = list(outputs)

    def handler(_request: LLMRequest) -> BaseModel:
        if not queue:
            raise AgentOutputError("scripted LLM output sequence exhausted")
        return queue.pop(0)

    return handler


def payload_list(request: LLMRequest, key: str) -> list[dict[str, Any]]:
    """Read a list of JSON objects from a request payload (helper for handlers)."""
    value = request.payload.get(key, [])
    if not isinstance(value, list):
        raise AgentOutputError(f"payload '{key}' is not a list")
    return [item for item in value if isinstance(item, dict)]
