"""Application service: the one entry point the API, CLI and future workers call.

It hides LangGraph specifics (thread ids, ``Command(resume=...)``, snapshots) behind
``start_run`` / ``submit_review`` / ``get_run``.
"""

from collections.abc import Callable, Mapping
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command, RetryPolicy

from social_growth_agent.errors import InvalidReviewError, RunNotFoundError
from social_growth_agent.graph import (
    DEFAULT_RETRY_POLICY,
    Dependencies,
    GraphState,
    RunConfig,
    build_graph,
)
from social_growth_agent.models import (
    Account,
    ContentStrategy,
    GraphRun,
    ReviewDecision,
    ReviewRequest,
    RunStatus,
)
from social_growth_agent.models.base import DomainModel

type RunObserver = Callable[[str, Mapping[str, Any]], None]
"""Called with (node_name, partial_update) after every node, e.g. to print a trace."""


class RunResult(DomainModel):
    state: GraphState
    pending_review: ReviewRequest | None = None

    @property
    def status(self) -> RunStatus:
        return self.state.status

    def summary(self) -> GraphRun:
        s = self.state
        return GraphRun(
            id=s.run_id,
            account_id=s.account.id,
            strategy_id=s.strategy.id,
            strategy_version=s.strategy.version,
            status=s.status,
            generation_attempts=s.generation_attempts,
            started_at=s.started_at,
        )


class WorkflowService:
    def __init__(
        self,
        deps: Dependencies,
        *,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    ) -> None:
        self._graph = build_graph(deps, checkpointer=checkpointer, retry_policy=retry_policy)

    def start_run(
        self,
        account: Account,
        strategy: ContentStrategy,
        config: RunConfig | None = None,
        *,
        observer: RunObserver | None = None,
    ) -> RunResult:
        state = GraphState(account=account, strategy=strategy, config=config or RunConfig())
        self._execute(state, state.run_id, observer)
        return self.get_run(state.run_id)

    def submit_review(
        self, run_id: str, decision: ReviewDecision, *, observer: RunObserver | None = None
    ) -> RunResult:
        current = self.get_run(run_id)
        if current.pending_review is None:
            raise InvalidReviewError(f"run {run_id} is not awaiting review ({current.status})")
        self._execute(Command(resume=decision.model_dump(mode="json")), run_id, observer)
        return self.get_run(run_id)

    def _execute(self, graph_input: Any, run_id: str, observer: RunObserver | None) -> None:
        if observer is None:
            self._graph.invoke(graph_input, _thread(run_id))
            return
        for chunk in self._graph.stream(graph_input, _thread(run_id), stream_mode="updates"):
            for node, update in chunk.items():
                observer(node, update if isinstance(update, Mapping) else {})

    def get_run(self, run_id: str) -> RunResult:
        snapshot = self._graph.get_state(_thread(run_id))
        if not snapshot.values:
            raise RunNotFoundError(run_id)
        pending = next((ReviewRequest.model_validate(i.value) for i in snapshot.interrupts), None)
        return RunResult(state=GraphState.model_validate(snapshot.values), pending_review=pending)


def _thread(run_id: str) -> Any:
    return {"configurable": {"thread_id": run_id}}
