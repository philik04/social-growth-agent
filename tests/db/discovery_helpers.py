"""Shared setup for the Phase 9 discovery DB tests."""

from typing import Any

from social_growth_agent.models import PlatformPost, utc_now
from social_growth_agent.providers.mocks import MockAnalyticsProvider, MockResearchProvider
from social_growth_agent.providers.mocks.discovery import (
    FixtureWeb,
    MockDiscoveryProviders,
    discovery_x_posts,
)
from social_growth_agent.services.runtime import Runtime
from tests.db.strategy_helpers import ACCOUNT, AUTH, register_and_activate, strategy_runtime

OWNER_ID = "4242"
OWNER = "p7_account"


class Env:
    def __init__(self, make_runtime, client_for, *, fail: tuple[str, ...] = ()) -> None:
        self.web = FixtureWeb(fail=fail)
        self.x = MockResearchProvider(discovery_x_posts(utc_now()))
        self.providers = MockDiscoveryProviders(web=self.web, research=self.x)
        self.verifier = MockAnalyticsProvider()
        self.runtime: Runtime = strategy_runtime(
            make_runtime,
            discovery_providers=self.providers,
            post_verifier=lambda: self.verifier,
            x_account_user_id=OWNER_ID,
        )
        self.client = client_for(self.runtime)
        register_and_activate(self.client)

    def session(self, **body: Any) -> dict[str, Any]:
        response = self.client.post(
            "/discovery/sessions",
            json={"account_id": ACCOUNT.id, "requested_by": "philipp"} | body,
            headers=AUTH,
        )
        assert response.status_code == 201, response.text
        return dict(response.json())

    def opportunities(self, session_id: str) -> list[dict[str, Any]]:
        response = self.client.get(f"/discovery/sessions/{session_id}/opportunities")
        assert response.status_code == 200, response.text
        return list(response.json())

    def start(self, opportunity_id: str, intent: str, **body: Any) -> Any:
        return self.client.post(
            f"/discovery/opportunities/{opportunity_id}/runs",
            json={
                "account": ACCOUNT.model_dump(mode="json"),
                "intent": intent,
                "reviewer": "philipp",
            }
            | body,
            headers=AUTH,
        )

    def approve(self, run_id: str) -> str:
        detail = self.client.get(f"/runs/{run_id}").json()
        assert detail["run"]["status"] == "awaiting_review", detail["run"]
        candidate_id = str(detail["review"]["candidate_ids"][0])
        decided = self.client.post(
            f"/runs/{run_id}/review",
            json={"action": "approve", "candidate_id": candidate_id, "reviewer": "philipp"},
        )
        assert decided.status_code == 202, decided.text
        return candidate_id

    def post_as_owner(self, post_id: str, text: str, *, author_id: str = OWNER_ID) -> None:
        self.verifier.add_post(
            PlatformPost(
                provider_post_id=post_id,
                author_id=author_id,
                author_username=OWNER,
                text=text,
                created_at=utc_now(),
            )
        )


def by_intent(opportunities: list[dict[str, Any]], intent: str) -> dict[str, Any]:
    return next(o for o in opportunities if intent in o["suggested_intents"])
