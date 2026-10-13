"""FastAPI application. Endpoints translate HTTP to ``RunService`` calls and nothing more.

Without DATABASE_URL the app still starts (health reports it); run endpoints return 503.
Startup never executes or resumes a run: runs a previous process left running are only
flagged ``stalled`` and continue on an explicit ``POST /runs/{id}/resume``.

Publishing never happens in an endpoint. ``POST /runs/{id}/publish`` only records a
durable publication intent (202); the publisher worker makes the platform call. With
``PUBLISHER_EMBEDDED_WORKER=true`` that worker runs on a thread in this process, which
is a local-development convenience; the standalone ``sga-publisher-worker`` is the
canonical publishing process.

Analytics (Phase 6) is read-only from here: endpoints list snapshots, posts, jobs and
lineage, and create jobs only on an explicit backfill. No endpoint calls the platform,
and startup never schedules or collects anything; ``sga-analytics-worker`` does the
reads.

Strategy evaluation (Phase 7): ``POST /evaluations`` computes a deterministic evaluation
from stored snapshots (no platform or model call). ``POST /evaluations/{id}/proposals``
is the only endpoint that calls a model, and only when STRATEGY_PROPOSER is enabled.
Approve, reject, activate and register need the review token. Approval creates a version
and never activates it; activation is its own request. No read endpoint writes anything.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status

from social_growth_agent import __version__
from social_growth_agent.api.errors import PersistenceNotConfiguredError, install_error_handlers
from social_growth_agent.api.schemas import (
    ActivateBody,
    ActorBody,
    ApproveProposalBody,
    CreateRunRequest,
    HealthResponse,
    PublishRequestBody,
    RegisterVersionBody,
    RejectProposalBody,
    RequestProposalBody,
    ResolvePublicationBody,
    ReviewRequestBody,
)
from social_growth_agent.config import AppSettings
from social_growth_agent.models import AnalyticsJobStatus, PublicationStatus, RunStatus
from social_growth_agent.models.strategy import Observation, ProposalStatus, ReviewAuthMethod
from social_growth_agent.persistence.views import (
    ActiveStrategyView,
    AnalyticsJobView,
    EvaluationCreated,
    EvaluationView,
    LineageView,
    MetricSnapshotView,
    PendingReview,
    ProposalView,
    PublicationView,
    PublishedPostView,
    RunDetail,
    RunSummary,
    StrategyHistoryView,
    StrategyVersionView,
    UsageReport,
)
from social_growth_agent.services.analytics import AnalyticsService, BackfillResult
from social_growth_agent.services.evaluations import EvaluationRequest, EvaluationService
from social_growth_agent.services.publications import PublicationService
from social_growth_agent.services.runs import RunService
from social_growth_agent.services.runtime import Runtime, build_runtime
from social_growth_agent.services.strategies import StrategyService


def _run_service(request: Request) -> RunService:
    current: Runtime | None = getattr(request.app.state, "runtime", None)
    if current is None:
        raise PersistenceNotConfiguredError("persistence is not configured (DATABASE_URL)")
    return current.service


def _publication_service(request: Request) -> PublicationService:
    current: Runtime | None = getattr(request.app.state, "runtime", None)
    if current is None:
        raise PersistenceNotConfiguredError("persistence is not configured (DATABASE_URL)")
    return current.publications


def _analytics_service(request: Request) -> AnalyticsService:
    current: Runtime | None = getattr(request.app.state, "runtime", None)
    if current is None:
        raise PersistenceNotConfiguredError("persistence is not configured (DATABASE_URL)")
    return current.analytics


def _runtime(request: Request) -> Runtime:
    current: Runtime | None = getattr(request.app.state, "runtime", None)
    if current is None:
        raise PersistenceNotConfiguredError("persistence is not configured (DATABASE_URL)")
    return current


def _evaluation_service(request: Request) -> EvaluationService:
    service = _runtime(request).evaluations
    if service is None:
        raise PersistenceNotConfiguredError("strategy evaluation is not configured")
    return service


def _strategy_service(request: Request) -> StrategyService:
    service = _runtime(request).strategies
    if service is None:
        raise PersistenceNotConfiguredError("strategy evaluation is not configured")
    return service


def _review_auth(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> ReviewAuthMethod:
    """Strategy review authentication (see ``services.review_auth``)."""
    return _runtime(request).review_auth.authenticate(authorization)


def _aware(name: str, value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise HTTPException(status_code=422, detail=f"{name} must include a timezone")
    return value


RunServiceDep = Annotated[RunService, Depends(_run_service)]
AnalyticsServiceDep = Annotated[AnalyticsService, Depends(_analytics_service)]
PublicationServiceDep = Annotated[PublicationService, Depends(_publication_service)]
EvaluationServiceDep = Annotated[EvaluationService, Depends(_evaluation_service)]
StrategyServiceDep = Annotated[StrategyService, Depends(_strategy_service)]
ReviewAuthDep = Annotated[ReviewAuthMethod, Depends(_review_auth)]


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
            # Only ever an explicitly configured opt-in: see PUBLISHER_EMBEDDED_WORKER.
            current.start_embedded_worker()
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
    def create_run(body: CreateRunRequest, request: Request) -> RunSummary:
        """``use_active_strategy: true`` runs with the account's activated version (404 if
        none). An inline strategy that claims a registered version with a different body
        is refused (409); an unregistered inline strategy works as before."""
        runtime = _runtime(request)
        strategy = body.strategy or _strategy_service(request).active_strategy(body.account.id)
        return runtime.service.create_run(body.account, strategy, body.config)

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

    @app.post("/runs/{run_id}/publish", status_code=status.HTTP_202_ACCEPTED)
    def publish(
        run_id: str, body: PublishRequestBody, publications: PublicationServiceDep
    ) -> PublicationView:
        """Record the publication intent. Approval does not publish; this does not post:
        the worker makes the call. 404 unknown run or candidate, 409 not publishable or
        already requested, 422 an invalid or out-of-bounds schedule."""
        return publications.request(
            run_id,
            body.candidate_id,
            scheduled_for=body.scheduled_for,
            requested_by=body.requested_by,
        )

    @app.get("/publications/{publication_id}")
    def get_publication(
        publication_id: str, publications: PublicationServiceDep
    ) -> PublicationView:
        return publications.get(publication_id)

    @app.get("/publications")
    def list_publications(
        publications: PublicationServiceDep,
        publication_status: Annotated[PublicationStatus | None, Query(alias="status")] = None,
        run_id: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[PublicationView]:
        return publications.list(status=publication_status, run_id=run_id, limit=limit)

    @app.post("/publications/{publication_id}/retry", status_code=status.HTTP_202_ACCEPTED)
    def retry_publication(
        publication_id: str, body: ActorBody, publications: PublicationServiceDep
    ) -> PublicationView:
        """Queue a failed publication again, where the failure category allows it.
        An ``unknown`` publication is never retried; resolve it instead."""
        return publications.retry(publication_id, requested_by=body.reviewer)

    @app.post("/publications/{publication_id}/resolve", status_code=status.HTTP_202_ACCEPTED)
    def resolve_publication(
        publication_id: str, body: ResolvePublicationBody, publications: PublicationServiceDep
    ) -> PublicationView:
        """Close an ambiguous outcome with what a human established on the platform."""
        return publications.resolve(
            publication_id,
            published=body.outcome == "published",
            provider_post_id=body.provider_post_id,
            resolved_by=body.reviewer,
            note=body.note,
        )

    @app.post("/publications/{publication_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
    def cancel_publication(
        publication_id: str, body: ActorBody, publications: PublicationServiceDep
    ) -> PublicationView:
        return publications.cancel(publication_id, cancelled_by=body.reviewer)

    # --- analytics (Phase 6) ------------------------------------------------------

    @app.get("/publications/{publication_id}/metrics")
    def publication_metrics(
        publication_id: str, analytics: AnalyticsServiceDep
    ) -> list[MetricSnapshotView]:
        """Snapshots ordered by target age. Counts are as reported (null = not
        reported); ``on_target`` and ``capture_delay_seconds`` show when each was
        really taken. Observational: no snapshot explains *why* a post performed."""
        return analytics.metrics(publication_id)

    @app.get("/publications/{publication_id}/lineage")
    def publication_lineage(publication_id: str, analytics: AnalyticsServiceDep) -> LineageView:
        """Publication -> candidate -> critiques -> findings -> source posts -> strategy."""
        return analytics.lineage(publication_id)

    @app.post(
        "/publications/{publication_id}/analytics/backfill",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def backfill_analytics(
        publication_id: str,
        analytics: AnalyticsServiceDep,
        dry_run: Annotated[bool, Query()] = False,
    ) -> BackfillResult:
        """Create the configured snapshot jobs this published post is missing.
        Idempotent; reads nothing from the platform. 409 if it is not published."""
        return analytics.backfill(publication_id, dry_run=dry_run)

    @app.get("/posts")
    def list_posts(
        analytics: AnalyticsServiceDep,
        since: Annotated[datetime | None, Query()] = None,
        until: Annotated[datetime | None, Query()] = None,
        account_id: Annotated[str | None, Query(max_length=64)] = None,
        job_status: Annotated[AnalyticsJobStatus | None, Query(alias="status")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[PublishedPostView]:
        """Published posts (newest first) with their latest snapshot. ``status``
        keeps posts that have an analytics job in that state."""
        return analytics.posts(
            since=_aware("since", since),
            until=_aware("until", until),
            account_id=account_id,
            job_status=job_status,
            limit=limit,
        )

    @app.get("/analytics/jobs")
    def list_analytics_jobs(
        analytics: AnalyticsServiceDep,
        job_status: Annotated[AnalyticsJobStatus | None, Query(alias="status")] = None,
        publication_id: Annotated[str | None, Query(max_length=64)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[AnalyticsJobView]:
        return analytics.jobs(status=job_status, publication_id=publication_id, limit=limit)

    @app.get("/analytics/jobs/{job_id}")
    def get_analytics_job(job_id: str, analytics: AnalyticsServiceDep) -> AnalyticsJobView:
        return analytics.job(job_id)

    @app.post("/analytics/jobs/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
    def retry_analytics_job(job_id: str, analytics: AnalyticsServiceDep) -> AnalyticsJobView:
        """Queue a failed job again (e.g. ``auth`` after fixing the token). 409 for
        ``not_found``/``bad_request`` (a retry cannot succeed) or another state."""
        return analytics.retry_job(job_id)

    @app.get("/reviews/pending")
    def pending_reviews(
        runs: RunServiceDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
    ) -> list[PendingReview]:
        return runs.pending_reviews(limit=limit)

    # --- strategy evaluation (Phase 7) ----------------------------------------------

    @app.post("/evaluations", status_code=status.HTTP_201_CREATED)
    def create_evaluation(
        body: EvaluationRequest, response: Response, evaluations: EvaluationServiceDep
    ) -> EvaluationCreated:
        """Deterministic and descriptive; reads stored snapshots only (no platform or model
        call). An identical request returns the stored evaluation (200)."""
        created = evaluations.create(body)
        if not created.created:
            response.status_code = status.HTTP_200_OK
        return created

    @app.get("/evaluations")
    def list_evaluations(
        evaluations: EvaluationServiceDep,
        account_id: Annotated[str | None, Query(max_length=64)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[EvaluationView]:
        return evaluations.list_evaluations(account_id=account_id, limit=limit)

    @app.get("/evaluations/{evaluation_id}")
    def get_evaluation(evaluation_id: str, evaluations: EvaluationServiceDep) -> EvaluationView:
        return evaluations.get(evaluation_id)

    @app.get("/evaluations/{evaluation_id}/observations")
    def evaluation_observations(
        evaluation_id: str,
        evaluations: EvaluationServiceDep,
        eligible: Annotated[bool | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[Observation]:
        """Every post considered: eligible, or excluded with its reason."""
        return evaluations.observations(
            evaluation_id, eligible=eligible, limit=limit, offset=offset
        )

    @app.post("/evaluations/{evaluation_id}/proposals", status_code=status.HTTP_201_CREATED)
    def request_proposal(
        evaluation_id: str, body: RequestProposalBody, strategies: StrategyServiceDep
    ) -> ProposalView:
        """Opt-in (STRATEGY_PROPOSER). The only endpoint that may call a model. 409 when
        proposals are disabled or another proposal is open for the account."""
        return strategies.request_proposal(evaluation_id, requested_by=body.requested_by)

    @app.get("/strategy-proposals")
    def list_proposals(
        strategies: StrategyServiceDep,
        proposal_status: Annotated[ProposalStatus | None, Query(alias="status")] = None,
        account_id: Annotated[str | None, Query(max_length=64)] = None,
        evaluation_id: Annotated[str | None, Query(max_length=64)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[ProposalView]:
        return strategies.list_proposals(
            status=proposal_status, account_id=account_id, evaluation_id=evaluation_id, limit=limit
        )

    @app.get("/strategy-proposals/{proposal_id}")
    def get_proposal(proposal_id: str, strategies: StrategyServiceDep) -> ProposalView:
        return strategies.get_proposal(proposal_id)

    @app.post("/strategy-proposals/{proposal_id}/retry", status_code=status.HTTP_201_CREATED)
    def retry_proposal(
        proposal_id: str, body: RequestProposalBody, strategies: StrategyServiceDep
    ) -> ProposalView:
        """Retry a failed proposal, or one stuck generating past the stale threshold."""
        return strategies.retry_proposal(proposal_id, requested_by=body.requested_by)

    @app.post("/strategy-proposals/{proposal_id}/approve")
    def approve_proposal(
        proposal_id: str,
        body: ApproveProposalBody,
        auth: ReviewAuthDep,
        strategies: StrategyServiceDep,
    ) -> ProposalView:
        """All-or-nothing. Creates a new strategy version; does not activate it, publish
        anything or start a run."""
        return strategies.approve(
            proposal_id,
            reviewer=body.reviewer,
            auth=auth,
            summary=body.summary,
            proposal_sha256=body.proposal_sha256,
            evaluation_digest=body.evaluation_digest,
        )

    @app.post("/strategy-proposals/{proposal_id}/reject")
    def reject_proposal(
        proposal_id: str,
        body: RejectProposalBody,
        auth: ReviewAuthDep,
        strategies: StrategyServiceDep,
    ) -> ProposalView:
        return strategies.reject(
            proposal_id, reviewer=body.reviewer, auth=auth, summary=body.summary
        )

    @app.post("/strategies/versions", status_code=status.HTTP_201_CREATED)
    def register_strategy_version(
        body: RegisterVersionBody,
        response: Response,
        auth: ReviewAuthDep,
        strategies: StrategyServiceDep,
    ) -> StrategyVersionView:
        """Register a human-written strategy as a version (idempotent: 200 if identical)."""
        result = strategies.register_version(
            body.strategy, reviewer=body.reviewer, auth=auth, note=body.note
        )
        if not result.created:
            response.status_code = status.HTTP_200_OK
        return result.version

    @app.get("/strategies/{strategy_id}/versions")
    def strategy_history(strategy_id: str, strategies: StrategyServiceDep) -> StrategyHistoryView:
        return strategies.history(strategy_id)

    @app.post("/strategies/{strategy_id}/activate")
    def activate_strategy(
        strategy_id: str, body: ActivateBody, auth: ReviewAuthDep, strategies: StrategyServiceDep
    ) -> ActiveStrategyView:
        """Explicit and compare-and-set (409 if the active version changed)."""
        return strategies.activate(
            strategy_id,
            body.version,
            expected_active_version_id=body.expected_active_version_id,
            reviewer=body.reviewer,
            auth=auth,
            note=body.note,
        )

    @app.get("/accounts/{account_id}/active-strategy")
    def active_strategy(account_id: str, strategies: StrategyServiceDep) -> ActiveStrategyView:
        return strategies.active(account_id)

    return app
