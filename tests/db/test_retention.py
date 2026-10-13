"""Phase 9 retention on PostgreSQL: deletion requests and the X retention window remove
third-party text from items, run evidence and checkpoints, and it never comes back."""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from social_growth_agent.graph.checkpointing import checkpoint_serializer
from social_growth_agent.models import utc_now
from social_growth_agent.persistence.retention import PURGED_TEXT
from tests.db.conftest import sql
from tests.db.discovery_helpers import Env
from tests.db.strategy_helpers import AUTH

AMY = "1900000000000000101"
AMY_TEXT = "how do you grade tool use when the tool itself is flaky"


@pytest.fixture
def env(make_runtime, client_for) -> Env:
    return Env(make_runtime, client_for)


def x_opportunity(env: Env, session_id: str) -> dict[str, object]:
    for opp in env.opportunities(session_id):
        items = env.client.get(f"/discovery/opportunities/{opp['id']}/items").json()
        if any(i["provider_item_id"] == AMY for i in items):
            return dict(opp)
    raise AssertionError("no opportunity cites the fixture post")


def stored_text(url: str, thread: str) -> str:
    """Everything the checkpointer stored for a run, decoded."""
    serde = checkpoint_serializer()
    parts = [
        str(row)
        for row in sql(url, "SELECT checkpoint FROM checkpoints WHERE thread_id = :t", t=thread)
    ]
    for kind, blob in sql(
        url,
        "SELECT type, blob FROM checkpoint_blobs WHERE thread_id = :t AND blob IS NOT NULL",
        t=thread,
    ) + sql(
        url,
        "SELECT type, blob FROM checkpoint_writes WHERE thread_id = :t AND blob IS NOT NULL",
        t=thread,
    ):
        assert isinstance(blob, bytes)
        parts.append(repr(serde.loads_typed((str(kind), blob))))
    return "\n".join(parts)


def purge(env: Env, *ids: str) -> dict[str, Any]:
    response = env.client.post(
        "/discovery/purge",
        json={"provider_item_ids": list(ids), "requested_by": "philipp"},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_a_deletion_removes_the_text_everywhere_and_blocks_it_from_returning(env: Env):
    session = env.session()
    opp = x_opportunity(env, str(session["id"]))
    response = env.start(str(opp["id"]), "original_insight")
    assert response.status_code == 202, response.text
    run_id = response.json()["run"]["id"]
    url = env.runtime.db.libpq_url
    assert AMY_TEXT in stored_text(url, run_id)  # the run's evidence holds the text

    assert (
        env.client.post(
            "/discovery/purge", json={"provider_item_ids": [AMY], "requested_by": "p"}
        ).status_code
        == 401
    )
    result = purge(env, AMY)
    assert result["discovery_items"] == 1 and result["tombstones"] == 1
    assert result["source_posts"] >= 1 and result["checkpoint_blobs"] >= 1

    assert AMY_TEXT not in stored_text(url, run_id)
    rows = sql(
        url,
        "SELECT text, title, author, purge_reason FROM discovery_items WHERE provider_item_id = :p",
        p=AMY,
    )
    assert rows == [(None, None, None, "deletion_request")]
    posts = sql(
        url,
        "SELECT text, author_username FROM source_posts WHERE run_id = :r AND "
        "source_id IN (SELECT id FROM discovery_items WHERE provider_item_id = :p)",
        r=run_id,
        p=AMY,
    )
    assert posts == [(PURGED_TEXT, None)]
    items = env.client.get(f"/discovery/opportunities/{opp['id']}/items").json()
    purged = next(i for i in items if i["provider_item_id"] == AMY)
    assert purged["purged"] is True and purged["text"] is None and purged["url"]
    detail = env.client.get(f"/runs/{run_id}").json()
    assert AMY_TEXT not in str(detail)

    # the run still resumes from its (redacted) checkpoint
    env.approve(run_id)
    assert env.client.get(f"/runs/{run_id}").json()["run"]["status"] == "approved"

    # a later session sees the post again but does not store it; replay skips it
    again = env.session(refresh=True)
    x_fetch = next(f for f in again["fetches"] if f["source_id"] == "x-ai-engineering")
    assert "tombstone" in (x_fetch["error_message"] or "")
    assert sql(url, "SELECT count(*) FROM discovery_items WHERE provider_item_id = :p", p=AMY) == [
        (1,)
    ]
    assert sql(url, "SELECT text FROM discovery_items WHERE provider_item_id = :p", p=AMY) == [
        (None,)
    ]
    # repeating the purge is harmless
    assert purge(env, AMY)["tombstones"] == 0


def test_purged_text_cannot_be_restored_in_the_database(env: Env):
    env.session()
    purge(env, AMY)
    url = env.runtime.db.libpq_url
    with pytest.raises(DBAPIError):
        sql(
            url,
            "UPDATE discovery_items SET text = 'back' WHERE provider_item_id = :p RETURNING id",
            p=AMY,
        )
    with pytest.raises(DBAPIError):
        sql(url, "DELETE FROM content_purges RETURNING id")


def test_a_run_from_purged_evidence_is_refused_when_nothing_is_left(env: Env):
    session = env.session()
    opp = x_opportunity(env, str(session["id"]))
    items = env.client.get(f"/discovery/opportunities/{opp['id']}/items").json()
    x_ids = [i["provider_item_id"] for i in items if i["platform"] == "x"]
    others = [i for i in items if i["platform"] != "x"]
    purge(env, *x_ids)
    response = env.start(str(opp["id"]), "original_insight")
    if others:
        assert response.status_code == 202
    else:
        assert response.status_code == 409 and "purged" in response.json()["detail"]


def test_the_retention_sweep_purges_old_x_text_only(env: Env):
    env.session()
    discovery = env.runtime.discovery
    assert discovery is not None
    assert discovery.purge_expired().provider_item_ids == []  # nothing is old yet
    discovery._clock = lambda: utc_now() + timedelta(days=31)
    result = discovery.purge_expired()
    assert AMY in result.provider_item_ids and result.reason == "retention"
    url = env.runtime.db.libpq_url
    assert sql(
        url, "SELECT count(*) FROM discovery_items WHERE platform = 'x' AND text IS NOT NULL"
    ) == [(0,)]
    assert sql(
        url, "SELECT count(*) FROM discovery_items WHERE platform <> 'x' AND text IS NULL"
    ) == [(0,)]
    assert discovery.purge_expired().provider_item_ids == []  # idempotent
