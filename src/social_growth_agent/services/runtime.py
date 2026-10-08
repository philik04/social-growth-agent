"""Builds the persisted run service from settings: database, checkpointer, executor.

The only place that opens connections. ``Runtime.close()`` releases them.
"""

import dataclasses
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

from langgraph.types import RetryPolicy
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from social_growth_agent.accounting import PriceList, load_price_list
from social_growth_agent.config import AppSettings
from social_growth_agent.errors import ConfigurationError
from social_growth_agent.graph import DEFAULT_RETRY_POLICY, Dependencies
from social_growth_agent.persistence import Database, OperationRecorder, UsageLedger
from social_growth_agent.persistence.checkpointer import open_pool, postgres_checkpointer
from social_growth_agent.policies import ScheduleWindow
from social_growth_agent.providers import SocialPublisher
from social_growth_agent.providers.factory import build_publisher
from social_growth_agent.services.factory import build_dependencies
from social_growth_agent.services.publications import PublicationService
from social_growth_agent.services.publisher import EmbeddedWorker, PublisherWorker
from social_growth_agent.services.runs import RunService
from social_growth_agent.services.workflow import WorkflowService


@dataclass
class Runtime:
    service: RunService
    publications: PublicationService
    db: Database
    pool: ConnectionPool[Connection[DictRow]]
    executor: Executor
    worker: PublisherWorker | None = None
    """Built only when publishing is configured; used by the embedded worker and demos."""
    embedded_worker: EmbeddedWorker | None = None
    """Set when PUBLISHER_EMBEDDED_WORKER is true (local development only)."""

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
) -> Runtime:
    """Raises ``ConfigurationError`` if DATABASE_URL, the price list or a configured
    provider is missing. Opening connections spends nothing; no run is started and
    nothing is published here (the embedded worker, when enabled, is started by the
    caller after startup recovery)."""
    if settings.database_url is None:
        raise ConfigurationError("DATABASE_URL is not set")
    prices = prices or load_price_list(settings.pricing_file)
    deps = deps or build_dependencies(settings)
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
    publications = PublicationService(db, window=window)
    worker = build_publisher_worker(settings, db, publisher=publisher)
    embedded = (
        EmbeddedWorker(worker, poll_seconds=settings.publisher_poll_seconds)
        if settings.publisher_embedded_worker
        else None
    )
    return Runtime(
        service=service,
        publications=publications,
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
    )
