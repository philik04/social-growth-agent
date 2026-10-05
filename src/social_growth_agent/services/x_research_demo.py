"""Opt-in live research run against the real X API. Consumes X API usage.

    RUN_LIVE_X_TESTS=1 X_BEARER_TOKEN=... \\
        uv run python -m social_growth_agent.services.x_research_demo ["AI agents lang:en"]

Runs ONE small query (X_MAX_RESULTS_PER_QUERY posts, default 10, no pagination, no
retries beyond X_MAX_QUERIES_PER_RUN), then continues through research, generation and
critique. With OPENAI_API_KEY set the agents use OpenAI; otherwise the deterministic fake
agents interpret the real posts (and the output says so). Stops at human review.
Never prints credentials or raw API JSON.
"""

import os
import sys

from social_growth_agent.config import AppSettings
from social_growth_agent.errors import ConfigurationError
from social_growth_agent.graph import RunConfig
from social_growth_agent.models import ResearchQuery
from social_growth_agent.services.demo import demo_account, print_summary
from social_growth_agent.services.factory import build_dependencies
from social_growth_agent.services.trace import TracePrinter
from social_growth_agent.services.workflow import WorkflowService

DEFAULT_QUERY = "AI agents lang:en"


def main(argv: list[str]) -> int:
    if os.environ.get("RUN_LIVE_X_TESTS") != "1":
        print("Set RUN_LIVE_X_TESTS=1 (and X_BEARER_TOKEN) to run the live X research demo.")
        return 2
    settings = AppSettings(research_provider="x")
    llm_provider = "openai" if settings.openai_api_key is not None else "fake"
    settings = settings.model_copy(update={"llm_provider": llm_provider})
    try:
        deps = build_dependencies(settings)
    except ConfigurationError as exc:
        print(f"configuration error: {exc}")
        return 2

    query = ResearchQuery(text=argv[0] if argv else DEFAULT_QUERY)
    agents = (
        f"openai/{settings.openai_model}"
        if llm_provider == "openai"
        else "fake deterministic agents (no OPENAI_API_KEY): findings are rule-based"
    )
    print(f"query: {query.text!r}")
    print(
        f"limits: max_results_per_query={settings.x_max_results_per_query} "
        f"max_queries_per_run={settings.x_max_queries_per_run}"
    )
    print(f"agents: {agents}\n")

    account, strategy = demo_account()
    result = WorkflowService(deps).start_run(
        account, strategy, RunConfig(research_query=query), observer=TracePrinter()
    )
    print_summary(result)

    fetches = result.state.research_fetches
    requests = sum(f.requests_made for f in fetches)
    posts = sum(f.posts_fetched for f in fetches)
    users = sum(f.users_fetched for f in fetches)
    print(
        f"\nX usage: requests={requests} post_reads={posts} user_reads={users} "
        "(resource counts only; see X's current pricing for cost)"
    )
    for finding in result.state.research:
        print(f"finding [{finding.claim_type}] {finding.theme}: {finding.evidence_source_ids}")
    calls = result.state.llm_calls
    tokens = sum(c.usage.total_tokens for c in calls if c.usage)
    print(f"llm calls={len(calls)} total_tokens={tokens}")
    return 0 if result.state.research_brief is not None else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
