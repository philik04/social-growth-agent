"""FastAPI application. Endpoints translate HTTP to ``RunService`` calls and nothing more.

Without DATABASE_URL the app still starts (health reports it); run endpoints return 503.
Startup never executes or resumes a run: runs a previous process left running are only
flagged ``stalled`` and continue on an explicit ``POST /runs/{id}/resume``.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request, status

from social_growth_agent import __version__
from social_growth_agent.api.errors import PersistenceNotConfiguredError, install_error_handlers
from social_growth_agent.api.schemas import CreateRunRequest, HealthResponse, ReviewRequestBody
from social_growth_agent.config import AppSettings
from social_growth_agent.models import RunStatus
from social_growth_agent.persistence.views import (
    PendingReview,
    RunDetail,
    RunSummary,
    UsageReport,
)
from social_growth_agent.services.runs import RunService
from social_growth_agent.services.runtime import Runtime, build_runtime


def _run_service(request: Request) -> RunService:
    current: Runtime | None = getattr(request.app.state, "runtime", None)
    if current is None:
        raise PersistenceNotConfiguredError("persistence is not configured (DATABASE_URL)")
    return current.service


RunServiceDep = Annotated[RunService, Depends(_run_service)]


def create_app(settings: AppSettings | None = None, runtime: Runtime | None = None) -> FastAPI:
    """``runtime`` is injected by tests; otherwise it is built from settings at startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = None
        current = runtime
        if current is None:
            config = settings or AppSettings()
            if config.database_url is not None:
                current = owned = build_runtime(config)
        if current is not None:
            current.service.recover_stalled()
        app.state.runtime = current
        try:
            yield
        finally:
            if owned is not None:
                owned.close()

    app = FastAPI(title="social-growth-agent", version=__version__, lifespan=lifespan)
    install_error_handlers(app)

    @app.get("/health")
    def health(request: Request) -> HealthResponse:
        current: Runtime | None = getattr(request.app.state, "runtime", None)
        if current is None:
            database = "not_configured"
        else:
            database = "ok" if current.db.ping() else "unavailable"
        return HealthResponse(status="ok", version=__version__, database=database)

    @app.post("/runs", status_code=status.HTTP_202_ACCEPTED)
    def create_run(body: CreateRunRequest, runs: RunServiceDep) -> RunSummary:
        return runs.create_run(body.account, body.strategy, body.config)

    @app.get("/runs")
    def list_runs(
        runs: RunServiceDep,
        run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[RunSummary]:
        return runs.list_runs(status=run_status, limit=limit)

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, runs: RunServiceDep) -> RunDetail:
        return runs.get_run(run_id)

    @app.get("/runs/{run_id}/usage")
    def get_usage(run_id: str, runs: RunServiceDep) -> UsageReport:
        return runs.usage(run_id)

    @app.post("/runs/{run_id}/review", status_code=status.HTTP_202_ACCEPTED)
    def review(run_id: str, body: ReviewRequestBody, runs: RunServiceDep) -> RunSummary:
        return runs.submit_review(run_id, body.to_decision())

    @app.post("/runs/{run_id}/resume", status_code=status.HTTP_202_ACCEPTED)
    def resume(run_id: str, runs: RunServiceDep) -> RunSummary:
        return runs.resume(run_id)

    @app.get("/reviews/pending")
    def pending_reviews(
        runs: RunServiceDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
    ) -> list[PendingReview]:
        return runs.pending_reviews(limit=limit)

    return app
