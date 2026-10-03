"""Node wrapper and error handler: timing, logging, and conversion of failures into run state."""

from typing import Literal, Protocol

from langgraph.errors import NodeError
from langgraph.types import Command

from social_growth_agent.errors import ProviderError, RunAbortError, SocialGrowthError
from social_growth_agent.graph.state import GraphState, StateUpdate
from social_growth_agent.models import NodeEvent, RunError, RunStatus, utc_now
from social_growth_agent.observability import get_logger, timed

_log = get_logger("graph")


class NodeFn(Protocol):
    """A graph node. The parameter must be named ``state``; LangGraph's typing requires it."""

    def __call__(self, state: GraphState) -> StateUpdate: ...


class ErrorHandlerFn(Protocol):
    def __call__(self, state: GraphState, error: NodeError) -> Command[Literal["failed"]]: ...


def instrument(name: str, fn: NodeFn) -> NodeFn:
    """Wrap a node so every execution appends a ``NodeEvent``.

    ``RunAbortError`` is recorded in ``errors`` and flips status to FAILED, so the
    graph routes to the ``failed`` node instead of crashing. Other exceptions
    propagate: provider errors go to the node's retry policy and error handler,
    and LangGraph's interrupt signal must pass through untouched.
    """

    def run(state: GraphState) -> StateUpdate:
        with timed() as timer:
            try:
                update = fn(state)
                outcome = "ok"
            except RunAbortError as exc:
                _log.warning(
                    "node aborted", extra={"node": name, "run_id": state.run_id, "error": str(exc)}
                )
                update = _failure_update(name, state, exc)
                outcome = "error"
        update["events"] = [
            NodeEvent(
                node=name,
                generation_attempt=update.get("generation_attempts", state.generation_attempts),
                outcome=outcome,
                duration_ms=timer.duration_ms,
                started_at=timer.started_at,
            )
        ]
        _log.info("node finished", extra={"node": name, "run_id": state.run_id, "outcome": outcome})
        return update

    return run


def provider_error_handler(name: str) -> ErrorHandlerFn:
    """Runs after the node's retry policy is exhausted (or for non-retryable errors).

    Provider failures become a recorded ``RunError`` and the run ends in ``failed``;
    nothing is silently replaced with fake content. Any other exception is a bug
    and is re-raised so it fails loudly.
    """

    def handle(state: GraphState, error: NodeError) -> Command[Literal["failed"]]:
        exc = error.error
        if not isinstance(exc, ProviderError):
            raise exc
        _log.error(
            "provider failure", extra={"node": name, "run_id": state.run_id, "error": str(exc)}
        )
        update = _failure_update(name, state, exc)
        update["events"] = [
            NodeEvent(
                node=name,
                generation_attempt=state.generation_attempts,
                outcome="provider_error",
                duration_ms=0.0,
                started_at=utc_now(),
            )
        ]
        return Command(update=update, goto="failed")

    return handle


def _failure_update(name: str, state: GraphState, exc: SocialGrowthError) -> StateUpdate:
    error = RunError(
        node=name,
        message=str(exc),
        generation_attempt=state.generation_attempts,
        error_type=type(exc).__name__,
    )
    update: StateUpdate = {"status": RunStatus.FAILED, "errors": [error]}
    if exc.llm_call is not None:
        update["llm_calls"] = [exc.llm_call]
    return update
