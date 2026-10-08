"""Run service: persisted, asynchronous runs on top of ``WorkflowService``.

Responsibilities (and nothing else; endpoints only translate HTTP to these calls):
- create a run row with immutable input snapshots and the price list it is estimated
  with, then execute the graph on a worker thread;
- validate review decisions against the paused checkpoint before resuming, under a row
  lock so a run can only be resumed once;
- project graph state into the domain tables after every step;
- flag runs a dead process left behind as ``stalled`` at startup, and continue them only
  when someone explicitly asks (no automatic provider spending at startup).
"""

from collections.abc import Callable
from concurrent.futures import Executor, Future
from typing import Any

from social_growth_agent.accounting import PriceList, estimate_cost
from social_growth_agent.errors import (
    InvalidReviewError,
    InvalidRunStateError,
    SocialGrowthError,
)
from social_growth_agent.graph import GraphState, RunConfig
from social_growth_agent.models import (
    Account,
    ContentStrategy,
    ReviewDecision,
    RunStatus,
    new_id,
)
from social_growth_agent.observability import get_logger
from social_growth_agent.persistence import Database, RunRecorder
from social_growth_agent.persistence import repository as repo
from social_growth_agent.persistence.tables import RunRow
from social_growth_agent.persistence.views import (
    PendingReview,
    RunDetail,
    RunSummary,
    UsageReport,
)
from social_growth_agent.policies import default_research_query
from social_growth_agent.services.workflow import RunResult, StateObserver, WorkflowService

_log = get_logger("runs")

_TERMINAL = (RunStatus.APPROVED, RunStatus.REJECTED, RunStatus.FAILED)


class InlineExecutor(Executor):
    """Runs work immediately on the caller's thread (tests, CLI demos)."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
        future.set_result(fn(*args, **kwargs))
        return future


class RunService:
    def __init__(
        self,
        *,
        workflow: WorkflowService,
        db: Database,
        prices: PriceList,
        executor: Executor,
    ) -> None:
        self._workflow = workflow
        self._db = db
        self._prices = prices
        self._executor = executor
        self._recorder = RunRecorder(db)

    # --- commands -------------------------------------------------------------------

    def create_run(
        self, account: Account, strategy: ContentStrategy, config: RunConfig | None = None
    ) -> RunSummary:
        config = config or RunConfig()
        run_id = new_id("run")
        query = config.research_query or default_research_query(strategy)
        with self._db.transaction() as session:
            pricing_id = repo.save_price_list(session, self._prices)
            repo.insert_run(
                session,
                RunRow(
                    id=run_id,
                    status=RunStatus.QUEUED.value,
                    research_query=query.text,
                    account=account.model_dump(mode="json"),
                    strategy=strategy.model_dump(mode="json"),
                    config=config.model_dump(mode="json"),
                    strategy_id=strategy.id,
                    strategy_version=strategy.version,
                    research_attempts=0,
                    generation_attempts=0,
                    regeneration_rounds=0,
                    edit_rounds=0,
                    review_candidate_ids=[],
                    pricing_id=pricing_id,
                ),
            )
        self._submit(run_id, lambda observe: self._start(run_id, observe))
        return self.get_summary(run_id)

    def submit_review(self, run_id: str, decision: ReviewDecision) -> RunSummary:
        """Validate against the paused checkpoint, claim the run, resume asynchronously.

        Raises ``InvalidRunStateError`` if the run is not awaiting review (including a
        second submission racing the first) and ``InvalidReviewError`` if the decision
        does not fit the pending review. Neither consumes the pending review.
        """
        with self._db.transaction() as session:
            row = repo.lock_run(session, run_id)
            if row.status != RunStatus.AWAITING_REVIEW.value:
                raise InvalidRunStateError(f"run {run_id} is not awaiting review ({row.status})")
            self._workflow.validate_review(run_id, decision)
            repo.set_status(session, run_id, RunStatus.RUNNING)
        self._submit(
            run_id,
            lambda observe: self._workflow.submit_review(run_id, decision, state_observer=observe),
        )
        return self.get_summary(run_id)

    def resume(self, run_id: str) -> RunSummary:
        """Continue a stalled run from its last checkpoint (explicit, never automatic).

        Steps already checkpointed (e.g. a completed X retrieval) are not repeated. A run
        that stalled while paused for review simply returns to ``awaiting_review``.
        """
        with self._db.transaction() as session:
            row = repo.lock_run(session, run_id)
            if row.status != RunStatus.STALLED.value:
                raise InvalidRunStateError(
                    f"run {run_id} is {row.status}; only stalled runs can be resumed"
                )
            repo.set_status(session, run_id, RunStatus.RUNNING)
            inputs = _inputs(row)
        self._submit(run_id, lambda observe: self._continue(run_id, inputs, observe))
        return self.get_summary(run_id)

    def recover_stalled(self) -> list[str]:
        """Called once at startup: flag runs a previous process left queued or running."""
        with self._db.transaction() as session:
            stalled = repo.mark_stalled(session)
        if stalled:
            _log.warning("stalled runs found", extra={"count": len(stalled)})
        return stalled

    # --- queries --------------------------------------------------------------------

    def get_summary(self, run_id: str) -> RunSummary:
        with self._db.transaction() as session:
            return repo.summary(repo.get_run_row(session, run_id))

    def get_run(self, run_id: str) -> RunDetail:
        with self._db.transaction() as session:
            return repo.run_detail(session, run_id, self._prices)

    def list_runs(self, *, status: RunStatus | None = None, limit: int = 50) -> list[RunSummary]:
        with self._db.transaction() as session:
            return repo.list_runs(session, status=status, limit=limit)

    def pending_reviews(self, *, limit: int = 50) -> list[PendingReview]:
        with self._db.transaction() as session:
            return repo.pending_reviews(session, limit=limit)

    def usage(self, run_id: str) -> UsageReport:
        with self._db.transaction() as session:
            row = repo.get_run_row(session, run_id)
            counts = repo.usage_counts(session, run_id)
            run_prices = repo.run_price_list(session, row) or self._prices
        return UsageReport(
            run_id=run_id,
            usage=counts,
            at_run_pricing=estimate_cost(counts, run_prices, basis="run_pricing"),
            at_current_pricing=estimate_cost(counts, self._prices, basis="current_pricing"),
        )

    # --- execution (worker threads) -------------------------------------------------

    def _start(self, run_id: str, observe: StateObserver) -> RunResult:
        with self._db.transaction() as session:
            row = repo.get_run_row(session, run_id)
            account, strategy, config = _inputs(row)
            repo.set_status(session, run_id, RunStatus.RUNNING)
        return self._workflow.start_run(
            account, strategy, config, run_id=run_id, state_observer=observe
        )

    def _continue(
        self,
        run_id: str,
        inputs: tuple[Account, ContentStrategy, RunConfig],
        observe: StateObserver,
    ) -> RunResult:
        if self._workflow.has_checkpoint(run_id):
            return self._workflow.continue_run(run_id, state_observer=observe)
        # The process died before the first checkpoint: nothing ran, start from snapshots.
        account, strategy, config = inputs
        return self._workflow.start_run(
            account, strategy, config, run_id=run_id, state_observer=observe
        )

    def _submit(self, run_id: str, work: Callable[[StateObserver], RunResult]) -> None:
        self._executor.submit(self._execute, run_id, work)

    def _execute(self, run_id: str, work: Callable[[StateObserver], RunResult]) -> None:
        """Worker entry point. Ordinary exceptions never escape: the run is marked failed.
        (A process crash leaves the row ``running``; the next startup flags it stalled.)"""
        try:
            result = work(self._observe)
        except InvalidReviewError:
            # Lost a race with another decision: the review is still pending.
            self._sync(run_id)
            return
        except Exception as exc:
            _log.exception("run execution failed", extra={"run_id": run_id})
            message = str(exc) if isinstance(exc, SocialGrowthError) else "internal error"
            with self._db.transaction() as session:
                repo.mark_failed(session, run_id, type(exc).__name__, message)
            return
        try:
            self._project(result.state)
        except Exception:  # row stays "running"; after a restart it is stalled and resumable
            _log.exception("final projection failed", extra={"run_id": run_id})

    def _observe(self, state: GraphState) -> None:
        """Project after every step while the run executes. Non-terminal states are shown
        as RUNNING until execution returns, so nobody can act on a half-resumed run."""
        status = state.status if state.status in _TERMINAL else RunStatus.RUNNING
        try:
            self._recorder.project(state, status=status)
        except Exception:  # the final projection (or a later resync) catches up
            _log.exception("projection failed", extra={"run_id": state.run_id})

    def _project(self, state: GraphState) -> None:
        self._recorder.project(state)

    def _sync(self, run_id: str) -> None:
        self._project(self._workflow.get_run(run_id).state)


def _inputs(row: RunRow) -> tuple[Account, ContentStrategy, RunConfig]:
    return (
        Account.model_validate(row.account),
        ContentStrategy.model_validate(row.strategy),
        RunConfig.model_validate(row.config),
    )
