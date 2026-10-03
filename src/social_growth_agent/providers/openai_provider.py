"""OpenAI adapter for ``LLMProvider`` using the Responses API with structured outputs.

This is the only module that imports the OpenAI SDK. It translates SDK exceptions
into domain errors so the graph can apply one retry and failure policy to every
provider. SDK-level retries are disabled: the graph's ``RetryPolicy`` owns retries,
which keeps the retry budget in one visible place.
"""

import openai
from openai import OpenAI
from pydantic import BaseModel, SecretStr, ValidationError

from social_growth_agent.errors import (
    ConfigurationError,
    EmptyModelOutputError,
    InvalidModelOutputError,
    ModelRefusalError,
    ProviderError,
    ProviderTimeoutError,
    SocialGrowthError,
    TransientProviderError,
)
from social_growth_agent.models import TokenUsage
from social_growth_agent.providers.llm import LLMRequest, LLMResponse

_TRANSIENT = (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError)
_BAD_OUTPUT = (
    openai.LengthFinishReasonError,
    openai.ContentFilterFinishReasonError,
    ValidationError,
)


class OpenAIProvider:
    def __init__(self, client: OpenAI) -> None:
        self._client = client

    @classmethod
    def from_api_key(cls, api_key: SecretStr | None, *, timeout_seconds: float) -> "OpenAIProvider":
        if api_key is None or not api_key.get_secret_value().strip():
            raise ConfigurationError("OPENAI_API_KEY is not set")
        client = OpenAI(api_key=api_key.get_secret_value(), timeout=timeout_seconds, max_retries=0)
        return cls(client)

    def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> LLMResponse[T]:
        settings = request.settings
        try:
            response = self._client.responses.parse(
                model=settings.model,
                instructions=request.system,
                input=request.prompt,
                text_format=schema,
                max_output_tokens=settings.max_output_tokens,
                temperature=openai.omit if settings.temperature is None else settings.temperature,
            )
        except SocialGrowthError:
            raise
        except Exception as exc:
            raise _translate(exc) from exc

        if response.status == "incomplete":
            reason = response.incomplete_details.reason if response.incomplete_details else None
            raise InvalidModelOutputError(f"model output incomplete ({reason})")
        refusal = _refusal_text(response.output)
        if refusal is not None:
            raise ModelRefusalError(f"model refused: {refusal[:200]}")
        parsed = response.output_parsed
        if parsed is None:
            raise EmptyModelOutputError(f"model returned no {schema.__name__}")

        usage = None
        if response.usage is not None:
            usage = TokenUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        return LLMResponse(output=parsed, model=response.model, usage=usage)


def _translate(exc: Exception) -> SocialGrowthError:
    """Map SDK and parsing exceptions onto the domain error hierarchy."""
    if isinstance(exc, openai.APITimeoutError):  # subclass of APIConnectionError: check first
        return ProviderTimeoutError(f"OpenAI request timed out: {exc}")
    if isinstance(exc, _TRANSIENT):
        return TransientProviderError(f"OpenAI transient error: {type(exc).__name__}: {exc}")
    if isinstance(exc, _BAD_OUTPUT):
        return InvalidModelOutputError(f"OpenAI output invalid: {type(exc).__name__}: {exc}")
    if isinstance(exc, openai.APIStatusError):
        return ProviderError(f"OpenAI API error {exc.status_code}: {exc.message}")
    if isinstance(exc, openai.OpenAIError):
        return ProviderError(f"OpenAI error: {type(exc).__name__}: {exc}")
    return ProviderError(f"unexpected provider failure: {type(exc).__name__}: {exc}")


def _refusal_text(output: object) -> str | None:
    for item in output if isinstance(output, list) else []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                return str(getattr(part, "refusal", ""))
    return None
