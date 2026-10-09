"""Shared setup for the analytics DB tests: published posts, workers, row readers."""

from datetime import datetime, timedelta

from social_growth_agent.persistence import Database
from social_growth_agent.policies.retry import Backoff
from social_growth_agent.providers import SocialAnalyticsProvider
from social_growth_agent.providers.mocks import MockAnalyticsProvider, MockPublisher
from social_growth_agent.services.analytics_worker import AnalyticsWorker
from tests.db.conftest import sql
from tests.db.publishing_helpers import approved_run

HOUR = timedelta(hours=1)


def intent(client, run_id: str, candidate_id: str) -> str:
    response = client.post(f"/runs/{run_id}/publish", json={"candidate_id": candidate_id})
    assert response.status_code == 202, response.text
    return str(response.json()["id"])


def publish_one(runtime, client, run_payload) -> str:
    """Approve a run, request publishing and let the runtime's publisher worker post it
    (the worker enqueues the analytics jobs in the same transaction)."""
    run_id, candidate_id = approved_run(client, run_payload)
    publication_id = intent(client, run_id, candidate_id)
    assert runtime.worker is not None
    assert runtime.worker.run_once().published == (publication_id,)
    return publication_id


def published_runtime(make_runtime, client_for, run_payload, *, count: int = 1, **settings):
    runtime = make_runtime(publisher=MockPublisher(), **settings)
    client = client_for(runtime)
    ids = [publish_one(runtime, client, run_payload) for _ in range(count)]
    return runtime, client, ids


def analytics_worker(
    db: Database,
    provider: SocialAnalyticsProvider | None = None,
    *,
    name: str = "analytics-1",
    lease_seconds: int = 60,
    max_attempts: int = 3,
    batch_size: int = 50,
    backoff: Backoff | None = None,
) -> AnalyticsWorker:
    return AnalyticsWorker(
        db=db,
        provider=provider or MockAnalyticsProvider(),
        lease_seconds=lease_seconds,
        max_attempts=max_attempts,
        batch_size=batch_size,
        backoff=backoff or Backoff(base_seconds=60, max_seconds=3600),
        name=name,
    )


def published_at(url: str, publication_id: str) -> datetime:
    [(value,)] = sql(url, "SELECT published_at FROM publications WHERE id = :id", id=publication_id)
    assert isinstance(value, datetime)
    return value


def post_id(url: str, publication_id: str) -> str:
    [(value,)] = sql(
        url, "SELECT provider_post_id FROM publications WHERE id = :id", id=publication_id
    )
    return str(value)


def jobs(
    url: str, publication_id: str, columns: str = "snapshot_age, status"
) -> list[tuple[object, ...]]:
    return sql(
        url,
        f"SELECT {columns} FROM analytics_jobs WHERE publication_id = :id ORDER BY age_seconds",
        id=publication_id,
    )


def job_id(url: str, publication_id: str, age: str) -> str:
    [(value,)] = sql(
        url,
        "SELECT id FROM analytics_jobs WHERE publication_id = :id AND snapshot_age = :age",
        id=publication_id,
        age=age,
    )
    return str(value)


def job(url: str, job_id_: str, columns: str) -> tuple[object, ...]:
    [values] = sql(url, f"SELECT {columns} FROM analytics_jobs WHERE id = :id", id=job_id_)
    return values


def count(url: str, table: str, where: str = "true", **params) -> int:
    [(value,)] = sql(url, f"SELECT count(*) FROM {table} WHERE {where}", **params)
    return int(str(value))
