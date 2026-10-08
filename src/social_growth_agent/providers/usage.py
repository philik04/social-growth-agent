"""Usage ports: where provider usage records go the moment a call returns, and the
started/finished ledger of provider-calling operations.

Graph state also carries every ``ResearchFetch`` and ``LLMCall``, but state from a
failed node attempt is discarded by LangGraph. A sink records usage at call time, so
paid-for calls stay visible even when the node that made them crashes. Records carry
stable ids, so writing the same record again (from state) is a no-op.
"""

from collections.abc import Sequence
from typing import Protocol

from social_growth_agent.models import LLMCall, ResearchFetch


class UsageSink(Protocol):
    def record(
        self, run_id: str, fetches: Sequence[ResearchFetch], llm_calls: Sequence[LLMCall]
    ) -> None:
        """Persist usage. Must not raise: a sink failure never fails the run."""
        ...


class OperationLedger(Protocol):
    """Started/finished ledger for provider-calling operations (Phase 5).

    ``started`` is called (and must be durable) *before* the operation may call a
    provider; ``finished`` after it returns or raises. If the process dies in between,
    the started record stays visible. Implementations must never invent usage or cost
    for an unfinished operation, and must not raise.
    """

    def started(
        self, run_id: str, *, node: str, provider: str, operation: str, generation_attempt: int
    ) -> str | None:
        """Record the start; returns an id for ``finished`` (None if recording failed)."""
        ...

    def finished(
        self,
        operation_id: str,
        *,
        outcome: str,
        error_type: str | None,
        latency_ms: float,
        usage_records: int,
    ) -> None: ...
