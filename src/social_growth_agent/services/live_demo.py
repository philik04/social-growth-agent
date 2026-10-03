"""Opt-in live run against the real OpenAI API. Costs money; never part of the test suite.

    RUN_LIVE_LLM_TESTS=1 OPENAI_API_KEY=... [OPENAI_MODEL=...] \\
        uv run python -m social_growth_agent.services.live_demo

Research material comes from the mock provider's synthetic fixture posts: X is not
integrated yet and the research agent is told so. The run stops at human review.
"""

import os
import sys

from social_growth_agent.config import AppSettings
from social_growth_agent.errors import ConfigurationError
from social_growth_agent.services.demo import demo_account, print_summary
from social_growth_agent.services.factory import build_dependencies
from social_growth_agent.services.trace import TracePrinter
from social_growth_agent.services.workflow import WorkflowService


def main() -> int:
    if os.environ.get("RUN_LIVE_LLM_TESTS") != "1":
        print("Set RUN_LIVE_LLM_TESTS=1 (and OPENAI_API_KEY) to run the live demo.")
        return 2
    settings = AppSettings(llm_provider="openai")
    try:
        deps = build_dependencies(settings)
    except ConfigurationError as exc:
        print(f"configuration error: {exc}")
        return 2

    print(f"provider=openai model={settings.openai_model}")
    account, strategy = demo_account()
    result = WorkflowService(deps).start_run(account, strategy, observer=TracePrinter())
    print_summary(result)
    calls = result.state.llm_calls
    tokens = sum(c.usage.total_tokens for c in calls if c.usage)
    print(f"llm calls={len(calls)} total_tokens={tokens}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
