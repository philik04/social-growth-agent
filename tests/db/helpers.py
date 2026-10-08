"""LLM wrappers for DB tests."""

from pydantic import BaseModel

from social_growth_agent.agents import ResearchReport
from social_growth_agent.models import TokenUsage
from social_growth_agent.providers import LLMProvider, LLMRequest, LLMResponse


class TokenReportingLLM:
    """Delegates to another provider and reports fixed token usage per call."""

    def __init__(self, inner: LLMProvider, usage: TokenUsage) -> None:
        self._inner = inner
        self._usage = usage

    def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> LLMResponse[T]:
        response = self._inner.generate_structured(request, schema)
        return LLMResponse(output=response.output, model=response.model, usage=self._usage)


class SimulatedCrash(BaseException):
    """Stands in for the process dying (BaseException: nothing in the app catches it)."""


class CrashOnce:
    """Delegates, but 'kills the process' the first time ``crash_on`` is requested."""

    def __init__(self, inner: LLMProvider, crash_on: type[BaseModel] = ResearchReport) -> None:
        self._inner = inner
        self._crash_on = crash_on
        self.crashed = False

    def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> LLMResponse[T]:
        if schema is self._crash_on and not self.crashed:
            self.crashed = True
            raise SimulatedCrash(f"process died during {request.task}")
        return self._inner.generate_structured(request, schema)
