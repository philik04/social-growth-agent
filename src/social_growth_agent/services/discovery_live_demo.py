"""Opt-in live discovery session: real feeds, GitHub releases and Hacker News over HTTPS.

    RUN_LIVE_DISCOVERY_DEMO=1 DATABASE_URL=postgresql://... \\
        uv run python -m social_growth_agent.services.discovery_live_demo [account_id]

Prints the plan first, then runs ONE bounded session with the default caps (one GET per
feed, repository and search; nothing paginates or retries) and prints the fetch outcomes
and the top opportunities. X sources read the real X API only when RESEARCH_PROVIDER=x
(with X_BEARER_TOKEN; at most the planned post reads, billed by X); otherwise they use
the synthetic X fixtures and the session is labelled synthetic. No model is called, no
run is started and nothing is posted, replied to, mentioned, followed or liked. External
text is stored as untrusted data under the retention rules in docs/CONTENT_DISCOVERY.md.
"""

import os
import secrets
import sys

from pydantic import SecretStr

from social_growth_agent.config import AppSettings
from social_growth_agent.errors import ConfigurationError
from social_growth_agent.models.discovery import DiscoveryRequest
from social_growth_agent.persistence.migrate import upgrade
from social_growth_agent.services.review_auth import ReviewAuth
from social_growth_agent.services.runs import InlineExecutor
from social_growth_agent.services.runtime import build_runtime

GATE = "RUN_LIVE_DISCOVERY_DEMO"


def main(argv: list[str]) -> int:
    if os.environ.get(GATE) != "1":
        print(f"Set {GATE}=1 (and DATABASE_URL) to run the live discovery demo.", file=sys.stderr)
        return 2
    settings = AppSettings()
    if settings.database_url is None:
        print("Set DATABASE_URL to a PostgreSQL database (see docs/DATABASE.md).", file=sys.stderr)
        return 2
    upgrade(settings.database_url.get_secret_value())
    settings = settings.model_copy(update={"discovery_sources": "live", "llm_provider": "fake"})
    try:
        runtime = build_runtime(settings, executor=InlineExecutor())
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        discovery = runtime.discovery
        assert discovery is not None
        request = DiscoveryRequest(
            account_id=argv[0] if argv else "acct_live_demo", requested_by="live-demo"
        )
        plan = discovery.plan(request)
        print(f"plan: {len(plan.calls)} source call(s); skipped {len(plan.skipped)}")
        for call in plan.calls:
            print(f"    {call.source_id:<22} {call.note}")
        print(
            f"    max X reads: {plan.max_x_post_reads} posts, {plan.max_x_user_reads} users; "
            f"estimated max cost: {plan.estimated_max_cost or 'unknown (prices unset)'}"
        )
        # the demo authorizes itself like the offline demos; the token is never printed
        token = secrets.token_urlsafe(24)
        auth = ReviewAuth(token=SecretStr(token)).authenticate(f"Bearer {token}")
        session = discovery.run_session(request, auth=auth)
        print(f"\nsession {session.id}: {session.status.value}, synthetic={session.synthetic}")
        for fetch in session.fetches:
            note = f" ({fetch.error_message})" if fetch.error_message else ""
            print(
                f"    {fetch.source_id:<22} {fetch.outcome.value:<12} "
                f"items={fetch.items_returned} x_reads={fetch.post_reads}{note}"
            )
        for opp in discovery.opportunities(session.id)[:5]:
            intents = ", ".join(i.value for i in opp.suggested_intents)
            print(f"#{opp.rank} {opp.score:.3f} {opp.headline[:90]}")
            print(f"    intents: {intents}; grok-eligible: {opp.grok.eligible}")
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
