"""Usage sink port: where provider usage records go the moment a call returns.

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
