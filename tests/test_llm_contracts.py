"""LLM output schemas must be accepted by OpenAI strict structured outputs."""

import json

import pytest
from openai.lib._pydantic import to_strict_json_schema

from social_growth_agent.agents import LLM_OUTPUT_SCHEMAS
from social_growth_agent.agents.base import load_prompt


@pytest.mark.parametrize("schema", LLM_OUTPUT_SCHEMAS, ids=lambda s: s.__name__)
def test_schema_is_strict_mode_compatible(schema):
    json_schema = to_strict_json_schema(schema)
    text = json.dumps(json_schema)
    assert '"default"' not in text  # strict mode requires every field to be required
    assert json_schema["additionalProperties"] is False


@pytest.mark.parametrize("name", ["research", "content", "critic"])
def test_prompts_have_required_sections(name):
    prompt = load_prompt(name)
    for section in ("# Role", "# Objective", "# Available inputs", "# Output contract"):
        assert section in prompt
    assert "Limitations" in prompt or "# Rules" in prompt


def test_research_prompt_forbids_claiming_to_browse():
    prompt = load_prompt("research")
    assert "supplied by the application" in prompt
    assert "must not claim" in prompt
