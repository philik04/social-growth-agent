"""Shared setup for the Phase 7 DB tests: seeded posts, a registered and active strategy."""

from datetime import timedelta
from typing import Any

from pydantic import SecretStr

from social_growth_agent.agents.strategy import StrategyAgent
from social_growth_agent.models import Account, ContentStrategy, HookType, utc_now
from social_growth_agent.models.strategy import Proposer
from social_growth_agent.providers import LLMProvider, LLMSettings
from social_growth_agent.services.runtime import Runtime
from social_growth_agent.services.strategies import StrategyService
from social_growth_agent.services.strategy_seed import SeedPost, seed_posts
from tests.db.conftest import TEST_PRICES

TOKEN = "review-token-SECRET-7f3a9c21"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ACCOUNT = Account(id="acct_p7", handle="@p7", niche="AI engineering")
V1 = ContentStrategy(
    id="strat_p7",
    account_id=ACCOUNT.id,
    pillars=["agent engineering"],
    tone="plain",
    target_audience="engineers",
    preferred_hooks=[HookType.HOW_TO],
)


def strategy_runtime(make_runtime, **overrides: Any) -> Runtime:
    settings: dict[str, Any] = {
        "strategy_proposer": "fake",
        "strategy_review_token": SecretStr(TOKEN),
    }
    runtime: Runtime = make_runtime(**(settings | overrides))
    return runtime


def with_strategy_llm(runtime: Runtime, llm: LLMProvider, *, stale_seconds: float = 120) -> None:
    """Swap the runtime's Strategy Agent model (scripted failures, bad output)."""
    runtime.strategies = StrategyService(
        runtime.db,
        prices=TEST_PRICES,
        agent=StrategyAgent(llm, LLMSettings(provider="openai", model="gpt-test")),
        proposer=Proposer.OPENAI,
        stale_seconds=stale_seconds,
    )


def seed_comparable(
    runtime: Runtime,
    *,
    per_hook: int = 11,
    provider: str = "mock_analytics",
    strategy: ContentStrategy = V1,
) -> list[str]:
    """``per_hook`` number posts (more likes) and how_to posts, 24h snapshots."""
    start = utc_now() - timedelta(days=20)
    posts: list[SeedPost] = []
    for i in range(per_hook):
        for hook, likes in ((HookType.NUMBER, 40 + i), (HookType.HOW_TO, 20 + i)):
            posts.append(
                SeedPost(
                    account=ACCOUNT,
                    strategy=strategy,
                    hook_type=hook,
                    published_at=start + timedelta(hours=5 * len(posts)),
                    metrics={
                        "likes": likes,
                        "replies": 2,
                        "reposts": 1,
                        "quotes": 0,
                        "bookmarks": 1,
                        "impressions": 1000,
                    },
                    provider=provider,
                )
            )
    return seed_posts(runtime.db, posts)


def register_and_activate(client, strategy: ContentStrategy = V1) -> dict[str, Any]:
    response = client.post(
        "/strategies/versions",
        json={"strategy": strategy.model_dump(mode="json"), "reviewer": "philipp"},
        headers=AUTH,
    )
    assert response.status_code in (200, 201), response.text
    version = response.json()
    active = client.get(f"/accounts/{strategy.account_id}/active-strategy")
    expected = active.json()["version"]["id"] if active.status_code == 200 else None
    response = client.post(
        f"/strategies/{strategy.id}/activate",
        json={
            "version": strategy.version,
            "expected_active_version_id": expected,
            "reviewer": "philipp",
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    return dict(version)


def create_evaluation(client, **overrides: Any) -> dict[str, Any]:
    body = {"account_id": ACCOUNT.id, "snapshot_age": "24h", "synthetic": True} | overrides
    response = client.post("/evaluations", json=body)
    assert response.status_code in (200, 201), response.text
    return dict(response.json()["evaluation"])


def approve_body(proposal: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return {
        "reviewer": "philipp",
        "summary": "try it",
        "proposal_sha256": proposal["proposal_sha256"],
        "evaluation_digest": proposal["evaluation_digest"],
    } | overrides
