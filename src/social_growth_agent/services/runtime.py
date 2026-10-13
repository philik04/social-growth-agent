"""Builds the persisted run service from settings: database, checkpointer, executor.

The only place that opens connections. ``Runtime.close()`` releases them.
"""

import dataclasses
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta

from langgraph.types import RetryPolicy
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from social_growth_agent.accounting import PriceList, load_price_list
from social_growth_agent.agents.strategy import StrategyAgent
from social_growth_agent.config import AppSettings
from social_growth_agent.errors import ConfigurationError
from social_growth_agent.graph import DEFAULT_RETRY_POLICY, Dependencies
from social_growth_agent.models.strategy import Proposer
from social_growth_agent.persistence import Database, OperationRecorder, UsageLedger
from social_growth_agent.persistence.analytics import DEFAULT_ON_TARGET_SECONDS
from social_growth_agent.persistence.checkpointer import open_pool, postgres_checkpointer
from social_growth_agent.policies import ScheduleWindow
from social_growth_agent.policies.analytics import parse_snapshot_ages
from social_growth_agent.policies.retry import Backoff
from social_growth_agent.providers import (
    LLMProvider,
    LLMSettings,
    SocialAnalyticsProvider,
    SocialPublisher,
)
from social_growth_agent.providers.factory import build_analytics_provider, build_publisher
from social_growth_agent.services.analytics import AnalyticsSchedule, AnalyticsService
from social_growth_agent.services.analytics_worker import AnalyticsWorker
from social_growth_agent.services.evaluations import EvaluationDefaults, EvaluationService
from social_growth_agent.services.experiments import ExperimentService
from social_growth_agent.services.factory import build_dependencies
from social_growth_agent.services.publications import PublicationService
from social_growth_agent.services.publisher import EmbeddedWorker, PublisherWorker
from social_growth_agent.services.review_auth import ReviewAuth
from social_growth_agent.services.runs import RunService
from social_growth_agent.services.strategies import StrategyService
from social_growth_agent.services.workflow import WorkflowService


@dataclass
class Runtime:
    service: RunService
    publications: PublicationService
    analytics: AnalyticsService
    db: Database
    pool: ConnectionPool[Connection[DictRow]]
    executor: Executor
    worker: PublisherWorker | None = None
    """Built only when publishing is configured; used by the embedded worker and demos."""
    embedded_worker: EmbeddedWorker | None = None
    """Set when PUBLISHER_EMBEDDED_WORKER is true (local development only)."""
    evaluations: EvaluationService | None = None
    """Phase 7; always set by ``build_runtime``."""
    strategies: StrategyService | None = None
    experiments: ExperimentService | None = None
    """Phase 8; always set by ``build_runtime``."""
    review_auth: ReviewAuth = field(default_factory=ReviewAuth)

    def start_embedded_worker(self) -> None:
        if self.embedded_worker is not None:
            self.embedded_worker.start()

    def close(self) -> None:
        if self.embedded_worker is not None:
            self.embedded_worker.stop()
        self.executor.shutdown(wait=True)
        self.pool.close()
        self.db.dispose()


def build_runtime(
    settings: AppSettings,
    *,
    deps: Dependencies | None = None,
    executor: Executor | None = None,
    prices: PriceList | None = None,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    publisher: SocialPublisher | None = None,
    strategy_llm: LLMProvider | None = None,
) -> Runtime:
    """Raises ``ConfigurationError`` if DATABASE_URL, the price list or a configured
    provider is missing. Opening connections spends nothing; no run is started and
    nothing is published here (the embedded worker, when enabled, is started by the
    caller after startup recovery)."""
    if settings.database_url is None:
        raise ConfigurationError("DATABASE_URL is not set")
    prices = prices or load_price_list(settings.pricing_file)
    deps = deps or build_dependencies(settings)
    # before any connection opens, so a missing key leaks no pool
    agent = build_strategy_agent(settings, llm=strategy_llm)
    workers = settings.api_max_concurrent_runs
    db = Database.connect(settings.database_url, pool_size=workers + 2)
    pool = open_pool(db.libpq_url, max_size=workers + 2)
    workflow = WorkflowService(
        dataclasses.replace(
            deps, usage_sink=UsageLedger(db), operation_ledger=OperationRecorder(db)
        ),
        checkpointer=postgres_checkpointer(pool),
        retry_policy=retry_policy,
    )
    executor = executor or ThreadPoolExecutor(max_workers=workers, thread_name_prefix="run")
    service = RunService(workflow=workflow, db=db, prices=prices, executor=executor)
    window = ScheduleWindow(
        max_ahead=timedelta(days=settings.publish_max_schedule_days),
        past_tolerance=timedelta(seconds=settings.publish_past_tolerance_seconds),
    )
    schedule = analytics_schedule(settings)
    publications = PublicationService(db, window=window, analytics=schedule)
    analytics = AnalyticsService(db, schedule=schedule)
    evaluations = EvaluationService(db, defaults=evaluation_defaults(settings), prices=prices)
    experiments = ExperimentService(
        db, runs=service, defaults=evaluation_defaults(settings), prices=prices
    )
    strategies = StrategyService(
        db,
        prices=prices,
        agent=agent,
        proposer=None if agent is None else Proposer(settings.strategy_proposer),
        stale_seconds=2 * settings.llm_timeout_seconds,
    )
    worker = build_publisher_worker(settings, db, publisher=publisher)
    embedded = (
        EmbeddedWorker(worker, poll_seconds=settings.publisher_poll_seconds)
        if settings.publisher_embedded_worker
        else None
    )
    return Runtime(
        service=service,
        publications=publications,
        analytics=analytics,
        evaluations=evaluations,
        strategies=strategies,
        experiments=experiments,
        review_auth=ReviewAuth.from_settings(settings),
        db=db,
        pool=pool,
        executor=executor,
        worker=worker,
        embedded_worker=embedded,
    )


def build_publisher_worker(
    settings: AppSettings, db: Database, *, publisher: SocialPublisher | None = None
) -> PublisherWorker:
    """Builds the worker (and, for ``PUBLISHER_PROVIDER=x``, its credentialed client).
    Building it publishes nothing: only ``run_once``/``run_forever`` ever call out."""
    return PublisherWorker(
        db=db,
        publisher=publisher or build_publisher(settings),
        lease_seconds=settings.publisher_lease_seconds,
        max_attempts=settings.publish_max_attempts,
        analytics=analytics_schedule(settings),
        backoff=Backoff(
            base_seconds=settings.publish_backoff_base_seconds,
            max_seconds=settings.publish_backoff_max_seconds,
        ),
    )


def analytics_schedule(settings: AppSettings) -> AnalyticsSchedule:
    return AnalyticsSchedule(
        ages=parse_snapshot_ages(settings.analytics_snapshot_ages),
        enqueue_on_publish=settings.analytics_enqueue_on_publish,
    )


def build_analytics_worker(
    settings: AppSettings, db: Database, *, provider: SocialAnalyticsProvider | None = None
) -> AnalyticsWorker:
    """Builds the worker (and, for ``ANALYTICS_PROVIDER=x``, its bearer-token client).
    Building it reads nothing: only ``run_once``/``run_forever`` call the platform, and
    only for jobs that already exist."""
    return AnalyticsWorker(
        db=db,
        provider=provider or build_analytics_provider(settings),
        lease_seconds=settings.analytics_lease_seconds,
        max_attempts=settings.analytics_max_attempts,
        batch_size=settings.analytics_batch_size,
        backoff=Backoff(
            base_seconds=settings.analytics_backoff_base_seconds,
            max_seconds=settings.analytics_backoff_max_seconds,
        ),
    )


def evaluation_defaults(settings: AppSettings) -> EvaluationDefaults:
    return EvaluationDefaults(
        ages=parse_snapshot_ages(settings.analytics_snapshot_ages),
        min_group_n=settings.eval_min_group_n,
        min_compare_n=settings.eval_min_compare_n,
        min_total_n=settings.eval_min_total_n,
        hour_block_hours=settings.eval_hour_block_hours,
        length_bucket_edges=settings.length_bucket_edges(),
        on_target_min_seconds=DEFAULT_ON_TARGET_SECONDS,
    )


def build_strategy_agent(
    settings: AppSettings, *, llm: LLMProvider | None = None
) -> StrategyAgent | None:
    """None for STRATEGY_PROPOSER=disabled (the default): no model is ever called.
    'openai' needs OPENAI_API_KEY; there is no fallback to the fake proposer. Building the
    agent calls nothing."""
    if settings.strategy_proposer == "disabled":
        return None
    if settings.strategy_proposer == "fake":
        from social_growth_agent.agents.fakes import build_fake_llm

        return StrategyAgent(
            llm or build_fake_llm(),
            LLMSettings(provider="fake", model="fake-deterministic", temperature=0.0),
        )
    from social_growth_agent.providers.openai_provider import OpenAIProvider

    provider = llm or OpenAIProvider.from_api_key(
        settings.openai_api_key, timeout_seconds=settings.llm_timeout_seconds
    )
    return StrategyAgent(
        provider,
        LLMSettings(
            provider="openai",
            model=settings.strategy_llm_model or settings.openai_model,
            temperature=0.0 if settings.llm_temperature_enabled else None,
            max_output_tokens=2048,
        ),
    )
