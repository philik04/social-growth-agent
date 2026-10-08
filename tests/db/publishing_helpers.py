"""Shared setup for the publishing DB tests: an approved run, and worker factories."""

from social_growth_agent.config import AppSettings
from social_growth_agent.persistence import Database
from social_growth_agent.services.publisher import PublisherWorker


def execute(url: str, query: str, **params) -> None:
    """Run a statement that returns nothing (the ``sql`` helper expects rows)."""
    from tests.db.conftest import sql

    sql(url, query + " RETURNING id", **params)


def approved_run(client, run_payload, **config) -> tuple[str, str]:
    """Drive a run to ``approved`` through the API and return (run_id, candidate_id)."""
    created = client.post("/runs", json=run_payload(**config))
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    body = client.get(f"/runs/{run_id}").json()
    assert body["run"]["status"] == "awaiting_review"
    candidate_id = body["review"]["candidate_ids"][0]
    decided = client.post(
        f"/runs/{run_id}/review",
        json={"action": "approve", "candidate_id": candidate_id, "reviewer": "philipp"},
    )
    assert decided.status_code == 202, decided.text
    assert client.get(f"/runs/{run_id}").json()["run"]["status"] == "approved"
    return run_id, candidate_id


def worker(
    db: Database,
    publisher,
    *,
    name: str = "worker-1",
    lease_seconds: int = 60,
    max_attempts: int = 3,
    batch_size: int = 5,
) -> PublisherWorker:
    return PublisherWorker(
        db=db,
        publisher=publisher,
        lease_seconds=lease_seconds,
        max_attempts=max_attempts,
        batch_size=batch_size,
        name=name,
    )


def worker_on(url: str, publisher, **kwargs) -> tuple[PublisherWorker, Database]:
    """A worker on its own connection pool, as a second OS process would have."""
    db = Database.connect(url)
    return worker(db, publisher, **kwargs), db


def publish_settings(**overrides) -> AppSettings:
    return AppSettings(_env_file=None, **overrides)
