"""Optional live demo: one real OpenAI strategy proposal from a saved evaluation.

    RUN_LIVE_STRATEGY_PROPOSAL_DEMO=1 OPENAI_API_KEY=... DATABASE_URL=postgresql://... \\
        uv run python -m social_growth_agent.services.strategy_live_demo <evaluation_id>

Opt-in twice: the gate variable and an explicit evaluation id. It makes exactly one model
call with the evidence package (aggregates and the current strategy, never post text),
validates the output deterministically, stores the proposal as ``pending_review`` (or
``failed``) and prints it with its token usage and estimated cost.

It never approves, rejects or activates anything, never publishes and never starts a
run: a human decides the stored proposal through the API afterwards. The model is
``STRATEGY_LLM_MODEL`` (default ``OPENAI_MODEL``).
"""

import os
import sys

from social_growth_agent.config import AppSettings
from social_growth_agent.errors import ConfigurationError, SocialGrowthError
from social_growth_agent.persistence.migrate import current_revision, head_revision
from social_growth_agent.services.runtime import build_runtime

GATE_ENV = "RUN_LIVE_STRATEGY_PROPOSAL_DEMO"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if os.environ.get(GATE_ENV) != "1":
        print(f"refusing to call OpenAI: set {GATE_ENV}=1 to opt in", file=sys.stderr)
        return 2
    if len(args) != 1 or not args[0].startswith("eval_"):
        print("usage: strategy_live_demo <evaluation_id>", file=sys.stderr)
        return 2
    settings = AppSettings(strategy_proposer="openai")
    if settings.database_url is None:
        print("Set DATABASE_URL (see docs/DATABASE.md).", file=sys.stderr)
        return 2
    revision = current_revision(settings.database_url.get_secret_value())
    if revision != head_revision():
        print(f"the database is at revision {revision}; run `sga-db upgrade` first")
        return 2
    try:
        runtime = build_runtime(settings)
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    strategies = runtime.strategies
    assert strategies is not None
    try:
        proposal = strategies.request_proposal(args[0], requested_by="strategy_live_demo")
    except SocialGrowthError as exc:
        print(f"no proposal: {exc}")
        runtime.close()
        return 1
    print(
        f"proposal {proposal.id}: {proposal.status.value} ({proposal.proposer}, {proposal.model})"
    )
    if proposal.validation_errors:
        for reason in proposal.validation_errors:
            print(f"  rejected by validation: {reason}")
    if proposal.error_type:
        print(f"  error: {proposal.error_type}: {proposal.error_message}")
    if proposal.proposal is not None:
        print(f"  outcome: {proposal.proposal.outcome.value}")
        print(f"  summary: {proposal.proposal.summary}")
        for change in proposal.proposal.changes:
            print(f"  {change.field.value}: {change.current_value} -> {change.proposed_value}")
            print(f"    evidence: {', '.join(change.evidence_refs)}")
            print(f"    {change.hypothesis}; confidence {change.confidence.value}")
    for llm in proposal.usage.usage.llm:
        print(f"  tokens: {llm.input_tokens} in, {llm.output_tokens} out ({llm.model})")
    estimate = proposal.usage.at_current_pricing
    if estimate.total is not None:
        print(f"  estimated cost: {estimate.total} {estimate.pricing.currency}")
    else:
        print(f"  estimated cost: unknown; missing prices: {', '.join(estimate.missing_prices)}")
    print("Nothing was approved or activated. Decide it via POST /strategy-proposals/{id}/...")
    runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
