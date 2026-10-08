"""Node wrapper and error handler: timing, logging, and conversion of failures into run state."""

from dataclasses import dataclass
from typing import Literal, Protocol

from langgraph.errors import NodeError
from langgraph.types import Command

from social_growth_agent.errors import ProviderError, RunAbortError, SocialGrowthError
from social_growth_agent.graph.state import GraphState, StateUpdate
from social_growth_agent.models import (
    LLMCall,
    NodeEvent,
    ResearchFetch,
    RunError,
    RunStatus,
    utc_now,
)
from social_growth_agent.observability import Timer, get_logger, timed
from social_growth_agent.providers import OperationLedger, UsageSink

_log = get_logger("graph")


class NodeFn(Protocol):
    """A graph node. The parameter must be named ``state``; LangGraph's typing requires it."""

    def __call__(self, state: GraphState) -> StateUpdate: ...


class ErrorHandlerFn(Protocol):
    def __call__(self, state: GraphState, error: NodeError) -> Command[Literal["failed"]]: ...


@dataclass(frozen=True)
class OperationLabel:
    """What a provider-calling node is recorded as in the operation ledger."""

    ledger: OperationLedger
    provider: str
    operation: str


def instrument(
    name: str,
    fn: NodeFn,
    usage_sink: UsageSink | None = None,
    operation: OperationLabel | None = None,
) -> NodeFn:
    """Wrap a node so every execution appends a ``NodeEvent``.

    ``RunAbortError`` is recorded in ``errors`` and flips status to FAILED, so the
    graph routes to the ``failed`` node instead of crashing. Other exceptions
    propagate: provider errors go to the node's retry policy and error handler,
    and LangGraph's interrupt signal must pass through untouched.

    With a ``usage_sink``, every ``ResearchFetch`` and ``LLMCall`` the node produced is
    recorded before the node returns or raises, including on attempts that fail.

    With an ``operation`` label, a started row is written before the node runs and
    finalized afterwards (succeeded / aborted / failed). A process that dies mid-node
    leaves the started row, so an in-flight external call is never invisible.
    """

    def run(state: GraphState) -> StateUpdate:
        op_id = _start(operation, name, state)
        with timed() as timer:
            try:
                update = fn(state)
                outcome = "ok"
            except SocialGrowthError as exc:
                usage = _error_usage(exc)
                _record(usage_sink, state.run_id, usage)
                aborted = isinstance(exc, RunAbortError)
                _finish(operation, op_id, "aborted" if aborted else "failed", exc, timer, usage)
                if not aborted:
                    raise
                _log.warning(
                    "node aborted", extra={"node": name, "run_id": state.run_id, "error": str(exc)}
                )
                update = _failure_update(name, state, exc)
                outcome = "error"
            except Exception as exc:
                _finish(operation, op_id, "failed", exc, timer, ([], []))
                raise
            else:
                usage = (update.get("research_fetches", []), update.get("llm_calls", []))
                _record(usage_sink, state.run_id, usage)
                _finish(operation, op_id, "succeeded", None, timer, usage)
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
    if exc.research_fetch is not None:
        update["research_fetches"] = [exc.research_fetch]
    return update


type Usage = tuple[list[ResearchFetch], list[LLMCall]]


def _error_usage(exc: SocialGrowthError) -> Usage:
    return (
        [exc.research_fetch] if exc.research_fetch else [],
        [exc.llm_call] if exc.llm_call else [],
    )


def _record(sink: UsageSink | None, run_id: str, usage: Usage) -> None:
    fetches, calls = usage
    if sink is None or not (fetches or calls):
        return
    try:
        sink.record(run_id, fetches, calls)
    except Exception:  # a sink must not fail the run; the checkpoint still holds usage
        _log.exception("usage sink failed", extra={"run_id": run_id})


def _start(label: OperationLabel | None, node: str, state: GraphState) -> str | None:
    if label is None:
        return None
    try:
        return label.ledger.started(
            state.run_id,
            node=node,
            provider=label.provider,
            operation=label.operation,
            generation_attempt=state.generation_attempts,
        )
    except Exception:  # never fails the run; the operation still runs and is recorded
        _log.exception("operation ledger failed", extra={"run_id": state.run_id, "node": node})
        return None


def _finish(
    label: OperationLabel | None,
    op_id: str | None,
    outcome: str,
    exc: BaseException | None,
    timer: Timer,
    usage: Usage,
) -> None:
    if label is None or op_id is None:
        return
    fetches, calls = usage
    try:
        label.ledger.finished(
            op_id,
            outcome=outcome,
            error_type=type(exc).__name__ if exc is not None else None,
            latency_ms=timer.elapsed_ms(),
            usage_records=len(fetches) + len(calls),
        )
    except Exception:
        _log.exception("operation ledger failed", extra={"operation_id": op_id})
