"""Price list loading and cost estimation (pure, no database)."""

from decimal import Decimal
from pathlib import Path

import pytest

from social_growth_agent.accounting import (
    LLMUsage,
    UsageCounts,
    estimate_cost,
    load_price_list,
)
from social_growth_agent.accounting.costs import ResearchUsage
from social_growth_agent.errors import ConfigurationError

ROOT = Path(__file__).resolve().parents[1]

PRICES = """
version = "v7"
as_of = "2026-10-01"
currency = "USD"
unbilled_providers = ["mock_fixtures", "fake"]

[x]
post_read = 0.005
user_read = 0.01

[openai.models."gpt-test"]
input_per_million = 0.4
output_per_million = 1.6
"""

USAGE = UsageCounts(
    research=[ResearchUsage(provider="x", fetches=2, requests=2, post_reads=10, user_reads=4)],
    llm=[
        LLMUsage(
            provider="openai", model="gpt-test", calls=3, input_tokens=5000, output_tokens=1000
        )
    ],
)


def write(tmp_path, content):
    path = tmp_path / "pricing.toml"
    path.write_text(content)
    return path


def test_shipped_price_list_is_valid_and_invents_no_prices():
    prices = load_price_list(ROOT / "pricing.toml")
    assert prices.x.post_read is None and prices.x.user_read is None
    assert all(p.input_per_million is None for p in prices.openai.values())
    estimate = estimate_cost(USAGE, prices, basis="current_pricing")
    assert estimate.total is None
    assert "x:post_reads" in estimate.missing_prices


def test_estimate_uses_configured_prices_exactly(tmp_path):
    prices = load_price_list(write(tmp_path, PRICES))
    estimate = estimate_cost(USAGE, prices, basis="run_pricing")

    costs = {line.item: line.cost for line in estimate.lines}
    assert costs["post_reads"] == Decimal("0.050")
    assert costs["user_reads"] == Decimal("0.04")
    assert costs["gpt-test input_tokens"] == Decimal("0.002")
    assert costs["gpt-test output_tokens"] == Decimal("0.0016")
    assert estimate.total == Decimal("0.0936")
    assert estimate.estimated is True
    assert estimate.missing_prices == []
    assert (estimate.pricing.version, estimate.pricing.as_of, estimate.pricing.currency) == (
        "v7",
        "2026-10-01",
        "USD",
    )


def test_price_list_id_is_stable_and_changes_with_any_price(tmp_path):
    first = load_price_list(write(tmp_path, PRICES))
    again = load_price_list(write(tmp_path, PRICES))
    changed = load_price_list(write(tmp_path, PRICES.replace("0.005", "0.006")))
    assert first.id == again.id and first.id.startswith("v7:")
    assert changed.id != first.id


def test_unbilled_providers_cost_nothing_and_unknown_models_are_missing(tmp_path):
    prices = load_price_list(write(tmp_path, PRICES))
    usage = UsageCounts(
        research=[
            ResearchUsage(
                provider="mock_fixtures", fetches=1, requests=0, post_reads=6, user_reads=0
            )
        ],
        llm=[
            LLMUsage(
                provider="fake",
                model="fake-deterministic",
                calls=5,
                input_tokens=9,
                output_tokens=9,
            ),
            LLMUsage(
                provider="openai", model="gpt-unknown", calls=1, input_tokens=10, output_tokens=0
            ),
        ],
    )
    estimate = estimate_cost(usage, prices, basis="current_pricing")
    assert estimate.missing_prices == ["openai:gpt-unknown input_tokens"]
    assert estimate.total is None  # never a partial total presented as complete


@pytest.mark.parametrize(
    "content",
    [
        "not toml = = =",
        'version = "v"\nas_of = "yesterday"\ncurrency = "USD"',
        PRICES + "\nbogus = 1",
    ],
)
def test_invalid_price_list_is_a_configuration_error(tmp_path, content):
    with pytest.raises(ConfigurationError):
        load_price_list(write(tmp_path, content))


def test_missing_price_list_is_a_configuration_error(tmp_path):
    with pytest.raises(ConfigurationError, match="not found"):
        load_price_list(tmp_path / "absent.toml")
