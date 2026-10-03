"""OpenAI adapter behaviour with a fake client. No network, no API key."""

from types import SimpleNamespace
from typing import cast

import httpx2
import openai
import pytest
from openai import OpenAI
from pydantic import BaseModel, SecretStr, ValidationError

from social_growth_agent.errors import (
    ConfigurationError,
    EmptyModelOutputError,
    InvalidModelOutputError,
    ModelRefusalError,
    ProviderError,
    ProviderTimeoutError,
    TransientProviderError,
)
from social_growth_agent.providers import LLMRequest, LLMSettings
from social_growth_agent.providers.openai_provider import OpenAIProvider

REQ = httpx2.Request("POST", "https://api.openai.com/v1/responses")


class Answer(BaseModel):
    text: str


class FakeResponses:
    def __init__(self, outcome):
        self.outcome = outcome
        self.kwargs: dict = {}

    def parse(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def response(parsed=None, *, status="completed", refusal=None, usage=(12, 5)):
    content = [SimpleNamespace(type="refusal", refusal=refusal)] if refusal else []
    return SimpleNamespace(
        status=status,
        incomplete_details=SimpleNamespace(reason="max_output_tokens")
        if status == "incomplete"
        else None,
        output=[SimpleNamespace(type="message", content=content)],
        output_parsed=parsed,
        usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]) if usage else None,
        model="gpt-test-2026",
    )


def provider(outcome):
    responses = FakeResponses(outcome)
    client = cast(OpenAI, SimpleNamespace(responses=responses))  # duck-typed fake client
    return OpenAIProvider(client), responses


def request(temperature: float | None = 0.3) -> LLMRequest:
    return LLMRequest(
        agent="critic",
        task="critic.review",
        system="sys",
        prompt="prompt",
        settings=LLMSettings(provider="openai", model="gpt-x", temperature=temperature),
    )


def status_error(cls, code: int):
    return cls("boom", response=httpx2.Response(code, request=REQ), body=None)


def validation_error() -> ValidationError:
    try:
        Answer.model_validate({})
    except ValidationError as exc:
        return exc
    raise AssertionError


def test_success_returns_parsed_output_and_usage():
    llm, responses = provider(response(Answer(text="hi")))
    result = llm.generate_structured(request(), Answer)

    assert result.output == Answer(text="hi")
    assert result.model == "gpt-test-2026"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 5)
    assert responses.kwargs["model"] == "gpt-x"
    assert responses.kwargs["text_format"] is Answer
    assert responses.kwargs["instructions"] == "sys"
    assert responses.kwargs["temperature"] == 0.3


def test_temperature_is_omitted_when_disabled():
    llm, responses = provider(response(Answer(text="hi")))
    llm.generate_structured(request(temperature=None), Answer)
    assert responses.kwargs["temperature"] is openai.omit


@pytest.mark.parametrize(
    ("raised", "expected", "transient"),
    [
        (openai.APITimeoutError(request=REQ), ProviderTimeoutError, True),
        (openai.APIConnectionError(request=REQ), TransientProviderError, True),
        (status_error(openai.RateLimitError, 429), TransientProviderError, True),
        (status_error(openai.InternalServerError, 500), TransientProviderError, True),
        (status_error(openai.AuthenticationError, 401), ProviderError, False),
        (status_error(openai.BadRequestError, 400), ProviderError, False),
        (RuntimeError("socket exploded"), ProviderError, False),
    ],
)
def test_sdk_errors_map_to_domain_errors(raised, expected, transient):
    llm, _ = provider(raised)
    with pytest.raises(expected) as exc_info:
        llm.generate_structured(request(), Answer)
    assert isinstance(exc_info.value, TransientProviderError) is transient


def test_schema_validation_failure_is_invalid_output():
    llm, _ = provider(validation_error())
    with pytest.raises(InvalidModelOutputError):
        llm.generate_structured(request(), Answer)


def test_empty_result_is_explicit_error():
    llm, _ = provider(response(None))
    with pytest.raises(EmptyModelOutputError):
        llm.generate_structured(request(), Answer)


def test_truncated_output_is_invalid_output():
    llm, _ = provider(response(None, status="incomplete"))
    with pytest.raises(InvalidModelOutputError, match="max_output_tokens"):
        llm.generate_structured(request(), Answer)


def test_refusal_is_explicit_error():
    llm, _ = provider(response(None, refusal="I can't help with that."))
    with pytest.raises(ModelRefusalError):
        llm.generate_structured(request(), Answer)


@pytest.mark.parametrize("key", [None, SecretStr(""), SecretStr("   ")])
def test_missing_api_key_is_configuration_error(key):
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        OpenAIProvider.from_api_key(key, timeout_seconds=5)


def test_client_is_built_without_sdk_retries():
    llm = OpenAIProvider.from_api_key(SecretStr("sk-test"), timeout_seconds=7)
    client = llm._client
    assert client.max_retries == 0
    assert client.timeout == 7
