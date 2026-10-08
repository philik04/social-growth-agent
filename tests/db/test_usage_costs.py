"""Usage counts from the ledger and reproducible cost estimates."""

from decimal import Decimal

from social_growth_agent.accounting.pricing import XPrices
from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.graph import AgentSettings
from social_growth_agent.models import TokenUsage
from social_growth_agent.providers import LLMSettings
from tests.db.conftest import TEST_PRICES, sql
from tests.db.helpers import TokenReportingLLM
from tests.x_fakes import FakeX, ok, sample_body

OPENAI = AgentSettings(
    research=LLMSettings(provider="openai", model="gpt-test"),
    content=LLMSettings(provider="openai", model="gpt-test"),
    critic=LLMSettings(provider="openai", model="gpt-test"),
)


def priced_runtime(make_runtime, prices=TEST_PRICES):
    return make_runtime(
        llm=TokenReportingLLM(build_fake_llm(), TokenUsage(input_tokens=1000, output_tokens=200)),
        research=FakeX(ok(sample_body(5))).provider(),
        agent_settings=OPENAI,
        prices=prices,
    )


def start(client, run_payload):
    response = client.post("/runs", json=run_payload(research_query={"text": "AI agents"}))
    assert response.status_code == 202
    return response.json()["id"]


def test_usage_is_persisted_with_post_and_user_reads_separate(
    make_runtime, client_for, run_payload
):
    runtime = priced_runtime(make_runtime)
    client = client_for(runtime)
    run_id = start(client, run_payload)

    usage = client.get(f"/runs/{run_id}/usage").json()["usage"]
    [x] = usage["research"]
    assert (x["provider"], x["requests"], x["post_reads"], x["user_reads"]) == ("x", 1, 5, 5)
    [llm] = usage["llm"]
    assert (llm["calls"], llm["input_tokens"], llm["output_tokens"]) == (5, 5000, 1000)
    rows = sql(runtime.db.libpq_url, "SELECT count(*) FROM llm_calls WHERE run_id = :id", id=run_id)
    assert rows == [(5,)]


def test_cost_is_estimated_from_configured_prices(make_runtime, client_for, run_payload):
    client = client_for(priced_runtime(make_runtime))
    run_id = start(client, run_payload)

    report = client.get(f"/runs/{run_id}/usage").json()
    estimate = report["at_run_pricing"]
    assert estimate["estimated"] is True and "estimate" in report["note"]
    # 5 post reads x 0.005 + 5 user reads x 0.010 + 5000 x 1.00/M + 1000 x 4.00/M
    assert Decimal(estimate["total"]) == Decimal("0.084")
    assert estimate["pricing"] == {
        "id": TEST_PRICES.id,
        "version": "test-1",
        "as_of": "2026-10-01",
        "currency": "USD",
        "source": "unit-test values, not real prices",
    }
    assert Decimal(client.get(f"/runs/{run_id}").json()["cost"]["total"]) == Decimal("0.084")


def test_historical_estimate_is_reproducible_after_prices_change(
    make_runtime, client_for, run_payload
):
    run_id = start(client_for(priced_runtime(make_runtime)), run_payload)

    newer = TEST_PRICES.model_copy(
        update={
            "version": "test-2",
            "as_of": "2026-11-01",
            "x": XPrices(post_read=Decimal("0.010"), user_read=Decimal("0.010")),
        }
    )
    client = client_for(priced_runtime(make_runtime, prices=newer))
    report = client.get(f"/runs/{run_id}/usage").json()

    assert report["at_run_pricing"]["pricing"]["version"] == "test-1"
    assert Decimal(report["at_run_pricing"]["total"]) == Decimal("0.084")
    assert report["at_current_pricing"]["pricing"]["version"] == "test-2"
    assert Decimal(report["at_current_pricing"]["total"]) == Decimal("0.109")


def test_missing_prices_give_no_total(make_runtime, client_for, run_payload):
    no_llm_prices = TEST_PRICES.model_copy(update={"openai": {}})
    client = client_for(priced_runtime(make_runtime, prices=no_llm_prices))
    run_id = start(client, run_payload)

    estimate = client.get(f"/runs/{run_id}/usage").json()["at_run_pricing"]
    assert estimate["total"] is None
    assert "openai:gpt-test input_tokens" in estimate["missing_prices"]
