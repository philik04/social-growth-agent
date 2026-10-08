"""Builds the persisted run service from settings: database, checkpointer, executor.

The only place that opens connections. ``Runtime.close()`` releases them.
"""

import dataclasses
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass

from langgraph.types import RetryPolicy
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from social_growth_agent.accounting import PriceList, load_price_list
from social_growth_agent.config import AppSettings
from social_growth_agent.errors import ConfigurationError
from social_growth_agent.graph import DEFAULT_RETRY_POLICY, Dependencies
from social_growth_agent.persistence import Database, UsageLedger
from social_growth_agent.persistence.checkpointer import open_pool, postgres_checkpointer
from social_growth_agent.services.factory import build_dependencies
from social_growth_agent.services.runs import RunService
from social_growth_agent.services.workflow import WorkflowService


@dataclass
class Runtime:
    service: RunService
    db: Database
    pool: ConnectionPool[Connection[DictRow]]
    executor: Executor

    def close(self) -> None:
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
) -> Runtime:
    """Raises ``ConfigurationError`` if DATABASE_URL, the price list or a configured
    provider is missing. Opening connections spends nothing; no run is started here."""
    if settings.database_url is None:
        raise ConfigurationError("DATABASE_URL is not set")
    prices = prices or load_price_list(settings.pricing_file)
    deps = deps or build_dependencies(settings)
    workers = settings.api_max_concurrent_runs
    db = Database.connect(settings.database_url, pool_size=workers + 2)
    pool = open_pool(db.libpq_url, max_size=workers + 2)
    workflow = WorkflowService(
        dataclasses.replace(deps, usage_sink=UsageLedger(db)),
        checkpointer=postgres_checkpointer(pool),
        retry_policy=retry_policy,
    )
    executor = executor or ThreadPoolExecutor(max_workers=workers, thread_name_prefix="run")
    service = RunService(workflow=workflow, db=db, prices=prices, executor=executor)
    return Runtime(service=service, db=db, pool=pool, executor=executor)
