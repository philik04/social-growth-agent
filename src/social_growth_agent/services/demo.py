"""Run the workflow end to end with deterministic fake agents and mock providers.

uv run python -m social_growth_agent.services.demo
"""

from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.graph import Dependencies
from social_growth_agent.models import (
    Account,
    ContentStrategy,
    HookType,
    ReviewAction,
    ReviewDecision,
)
from social_growth_agent.providers.mocks import MockResearchProvider
from social_growth_agent.services.trace import TracePrinter
from social_growth_agent.services.workflow import RunResult, WorkflowService


def demo_account() -> tuple[Account, ContentStrategy]:
    account = Account(handle="@agentic_builder", niche="AI engineering")
    strategy = ContentStrategy(
        account_id=account.id,
        pillars=["agent engineering", "llm evaluation"],
        tone="practical, specific, no hype",
        target_audience="engineers shipping LLM features",
        preferred_hooks=[HookType.NUMBER, HookType.HOW_TO],
        avoid_topics=["crypto"],
    )
    return account, strategy


def print_summary(result: RunResult) -> None:
    state = result.state
    print(f"\nrun {state.run_id}: {result.status} after {state.generation_attempts} attempts")
    if result.pending_review is not None:
        for cand in result.pending_review.candidates:
            print(f"  awaiting review: {cand.id} [{cand.hook_type}] {cand.content}")


def main() -> None:
    service = WorkflowService(
        Dependencies(llm=build_fake_llm(), research_provider=MockResearchProvider())
    )
    account, strategy = demo_account()
    result = service.start_run(account, strategy, observer=TracePrinter())
    print_summary(result)
    if result.pending_review is None:
        return

    chosen = result.pending_review.candidates[0].id
    decision = ReviewDecision(action=ReviewAction.APPROVE, candidate_id=chosen, reviewer="demo")
    print(f"resumed: {service.submit_review(result.state.run_id, decision).status}")


if __name__ == "__main__":
    main()
