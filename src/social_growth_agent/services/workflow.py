"""Application service: the one entry point the API, CLI and future workers call.

It hides LangGraph specifics (thread ids, ``Command(resume=...)``, snapshots) behind
``start_run`` / ``submit_review`` / ``continue_run`` / ``get_run``.
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
    validate_review,
)
from social_growth_agent.models import (
    Account,
    ContentStrategy,
    GraphRun,
    ReviewDecision,
    ReviewRequest,
    RunStatus,
    new_id,
)
from social_growth_agent.models.base import DomainModel

type RunObserver = Callable[[str, Mapping[str, Any]], None]
"""Called with (node_name, partial_update) after every node, e.g. to print a trace."""

type StateObserver = Callable[[GraphState], None]
"""Called with the full state after every step, e.g. to persist it."""


class RunResult(DomainModel):
    state: GraphState
    pending_review: ReviewRequest | None = None
    next_nodes: tuple[str, ...] = ()
    """Nodes the checkpoint would run next; non-empty without a review means unfinished."""

    @property
    def status(self) -> RunStatus:
        return self.state.status

    @property
    def unfinished(self) -> bool:
        return self.pending_review is None and bool(self.next_nodes)

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
        run_id: str | None = None,
        observer: RunObserver | None = None,
        state_observer: StateObserver | None = None,
    ) -> RunResult:
        state = GraphState(
            run_id=run_id or new_id("run"),
            account=account,
            strategy=strategy,
            config=config or RunConfig(),
        )
        self._execute(state, state.run_id, observer, state_observer)
        return self.get_run(state.run_id)

    def submit_review(
        self,
        run_id: str,
        decision: ReviewDecision,
        *,
        observer: RunObserver | None = None,
        state_observer: StateObserver | None = None,
    ) -> RunResult:
        self.validate_review(run_id, decision)
        resume: Command[None] = Command(resume=decision.model_dump(mode="json"))
        self._execute(resume, run_id, observer, state_observer)
        return self.get_run(run_id)

    def validate_review(self, run_id: str, decision: ReviewDecision) -> RunResult:
        """Check a decision against the paused run without resuming it."""
        current = self.get_run(run_id)
        if current.pending_review is None:
            raise InvalidReviewError(f"run {run_id} is not awaiting review ({current.status})")
        validate_review(current.state, decision)
        return current

    def continue_run(
        self,
        run_id: str,
        *,
        observer: RunObserver | None = None,
        state_observer: StateObserver | None = None,
    ) -> RunResult:
        """Continue an interrupted (crashed) run from its last checkpoint.

        Completed steps are not executed again: their results are in the checkpoint.
        A run paused for review, or finished, is returned unchanged.
        """
        current = self.get_run(run_id)
        if current.unfinished:
            self._execute(None, run_id, observer, state_observer)
            return self.get_run(run_id)
        return current

    def _execute(
        self,
        graph_input: Any,
        run_id: str,
        observer: RunObserver | None,
        state_observer: StateObserver | None,
    ) -> None:
        # "sync" durability: a step's checkpoint is written before the next step starts,
        # so a crash never loses a completed provider call.
        config = _thread(run_id)
        if observer is None and state_observer is None:
            self._graph.invoke(graph_input, config, durability="sync")
            return
        for mode, chunk in self._graph.stream(
            graph_input, config, stream_mode=["updates", "values"], durability="sync"
        ):
            if mode == "updates" and observer is not None and isinstance(chunk, Mapping):
                for node, update in chunk.items():
                    observer(node, update if isinstance(update, Mapping) else {})
            elif mode == "values" and state_observer is not None and "__interrupt__" not in chunk:
                state_observer(GraphState.model_validate(chunk))

    def get_run(self, run_id: str) -> RunResult:
        snapshot = self._graph.get_state(_thread(run_id))
        if not snapshot.values:
            raise RunNotFoundError(run_id)
        pending = next((ReviewRequest.model_validate(i.value) for i in snapshot.interrupts), None)
        return RunResult(
            state=GraphState.model_validate(snapshot.values),
            pending_review=pending,
            next_nodes=tuple(snapshot.next),
        )

    def has_checkpoint(self, run_id: str) -> bool:
        return bool(self._graph.get_state(_thread(run_id)).values)


def _thread(run_id: str) -> Any:
    return {"configurable": {"thread_id": run_id}}
