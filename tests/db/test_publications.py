"""The publication API and the durable intent, against real PostgreSQL.

Covers: intent creation, every refusal the policy makes, duplicate requests, the
database's own uniqueness guarantees, scheduling, listing, retry/resolve/cancel, and
the rule that approval alone never publishes.
"""

import time
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from social_growth_agent.models import CritiqueVerdict
from social_growth_agent.policies import idempotency_key
from social_growth_agent.providers.mocks import MockPublisher
from tests.conftest import always, make_llm
from tests.db.conftest import sql
from tests.db.publishing_helpers import approved_run, worker


def publish(client, run_id, candidate_id, **body):
    return client.post(f"/runs/{run_id}/publish", json={"candidate_id": candidate_id, **body})


def request_publish(client, run_id, candidate_id, **body):
    response = publish(client, run_id, candidate_id, **body)
    assert response.status_code == 202, response.text
    return response.json()


# --- 1. intent creation ----------------------------------------------------------------


def test_publish_request_creates_one_durable_intent_and_returns_202(
    make_runtime, client_for, run_payload
):
    runtime = make_runtime()
    client = client_for(runtime)
    run_id, candidate_id = approved_run(client, run_payload)

    body = request_publish(client, run_id, candidate_id, requested_by="philipp")

    assert body["status"] == "ready"
    assert body["run_id"] == run_id and body["candidate_id"] == candidate_id
    assert body["platform"] == "x"
    assert body["attempt_count"] == 0
    assert body["provider_post_id"] is None and body["published_at"] is None
    assert body["requested_by"] == "philipp"
    [(key, status, content_hash, attempts)] = sql(
        runtime.db.libpq_url,
        "SELECT idempotency_key, status, content_sha256, attempt_count "
        "FROM publications WHERE id = :id",
        id=body["id"],
    )
    assert key == idempotency_key("x", run_id, candidate_id)
    assert status == "ready" and attempts == 0 and len(content_hash) == 64


# --- 19. approval alone does not publish -----------------------------------------------


def test_approval_alone_creates_no_publication_and_calls_no_platform(
    make_runtime, client_for, run_payload
):
    publisher_calls = []

    class RecordingPublisher:
        platform = "x"
        provider_name = "mock_publisher"

        def publish(self, request):  # pragma: no cover - must never run
            publisher_calls.append(request)
            raise AssertionError("approval must not publish")

    runtime = make_runtime(publisher=RecordingPublisher())
    client = client_for(runtime)
    run_id, _ = approved_run(client, run_payload)

    assert sql(runtime.db.libpq_url, "SELECT count(*) FROM publications") == [(0,)]
    assert sql(runtime.db.libpq_url, "SELECT count(*) FROM publication_attempts") == [(0,)]
    assert publisher_calls == []
    assert client.get(f"/runs/{run_id}").json()["publications"] == []


# --- 2, 3. refusals --------------------------------------------------------------------


def test_an_unapproved_candidate_is_refused_with_409(make_runtime, client_for, run_payload):
    client = client_for(make_runtime())
    created = client.post("/runs", json=run_payload())
    run_id = created.json()["id"]
    body = client.get(f"/runs/{run_id}").json()
    candidate_id = body["review"]["candidate_ids"][0]

    response = publish(client, run_id, candidate_id)

    assert response.status_code == 409
    assert "not approved" in response.json()["detail"]


def test_a_candidate_whose_critique_failed_is_refused(make_runtime, client_for, run_payload):
    # The second candidate of this run is told to revise, so it never passes.
    llm = make_llm(lambda _a, index: CritiqueVerdict.PASS if index == 0 else CritiqueVerdict.REVISE)
    runtime = make_runtime(llm=llm)
    client = client_for(runtime)
    run_id, approved = approved_run(client, run_payload, candidates_per_attempt=2)
    revised = [
        c["id"]
        for c in client.get(f"/runs/{run_id}").json()["candidates"]
        if c["critiques"] and c["critiques"][-1]["verdict"] != "pass"
    ]
    assert revised, "the test needs a candidate with a failing critique"

    response = publish(client, run_id, revised[0])

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "not the approved candidate" in detail and "not pass" in detail
    assert approved != revised[0]


def test_unknown_run_candidate_and_publication_return_404(make_runtime, client_for, run_payload):
    client = client_for(make_runtime())
    run_id, candidate_id = approved_run(client, run_payload)

    assert publish(client, "run_missing", candidate_id).status_code == 404
    assert publish(client, run_id, "cand_missing").status_code == 404
    assert client.get("/publications/pub_missing").status_code == 404
    assert client.post("/publications/pub_missing/retry", json={}).status_code == 404


# --- 4, 5. duplicates ------------------------------------------------------------------


def test_a_duplicate_request_creates_no_second_intent(make_runtime, client_for, run_payload):
    runtime = make_runtime()
    client = client_for(runtime)
    run_id, candidate_id = approved_run(client, run_payload)
    first = request_publish(client, run_id, candidate_id)

    second = publish(client, run_id, candidate_id)

    assert second.status_code == 409
    assert second.json()["publication_id"] == first["id"]
    assert second.json()["status"] == "ready"
    assert sql(runtime.db.libpq_url, "SELECT count(*) FROM publications") == [(1,)]


def test_the_database_itself_refuses_a_second_intent_for_the_same_target(
    make_runtime, client_for, run_payload
):
    runtime = make_runtime()
    client = client_for(runtime)
    run_id, candidate_id = approved_run(client, run_payload)
    request_publish(client, run_id, candidate_id)
    insert = (
        "INSERT INTO publications (id, run_id, candidate_id, platform, idempotency_key, "
        "content, content_sha256, status, attempt_count) VALUES (:id, :run, :cand, 'x', "
        ":key, 'text', 'hash', 'ready', 0)"
    )

    with pytest.raises(IntegrityError, match="uq_publications_target"):
        sql(
            runtime.db.libpq_url,
            insert,
            id="pub_dup",
            run=run_id,
            cand=candidate_id,
            key="a-different-key",
        )
    with pytest.raises(IntegrityError, match="idempotency_key"):
        sql(
            runtime.db.libpq_url,
            insert.replace("'x'", "'mastodon'"),
            id="pub_dup2",
            run=run_id,
            cand=candidate_id,
            key=idempotency_key("x", run_id, candidate_id),
        )


def test_a_publication_cannot_reference_a_candidate_of_another_run(
    make_runtime, client_for, run_payload
):
    runtime = make_runtime()
    client = client_for(runtime)
    run_a, candidate_a = approved_run(client, run_payload)
    run_b, _ = approved_run(client, run_payload)

    with pytest.raises(IntegrityError, match="fk_publications_run_candidate"):
        sql(
            runtime.db.libpq_url,
            "INSERT INTO publications (id, run_id, candidate_id, platform, idempotency_key, "
            "content, content_sha256, status, attempt_count) VALUES ('pub_x', :run, :cand, "
            "'x', 'key', 'text', 'hash', 'ready', 0)",
            run=run_b,
            cand=candidate_a,
        )
    assert run_a != run_b


# --- 14. scheduling --------------------------------------------------------------------


def test_a_scheduled_publication_is_stored_as_utc_and_stays_scheduled(
    make_runtime, client_for, run_payload
):
    runtime = make_runtime()
    client = client_for(runtime)
    run_id, candidate_id = approved_run(client, run_payload)
    # 10:30 in a +02:00 zone is 08:30 UTC.
    local = datetime(2026, 11, 2, 10, 30, tzinfo=timezone(timedelta(hours=2)))

    body = request_publish(client, run_id, candidate_id, scheduled_for=local.isoformat())

    assert body["status"] == "scheduled"
    [(stored,)] = sql(
        runtime.db.libpq_url,
        "SELECT scheduled_for FROM publications WHERE id = :id",
        id=body["id"],
    )
    assert stored.astimezone(UTC) == datetime(2026, 11, 2, 8, 30, tzinfo=UTC)
    assert client.get(f"/publications/{body['id']}").json()["status"] == "scheduled"


def test_a_slightly_past_schedule_counts_as_publish_now(make_runtime, client_for, run_payload):
    client = client_for(make_runtime())
    run_id, candidate_id = approved_run(client, run_payload)
    just_past = datetime.now(UTC) - timedelta(minutes=2)

    body = request_publish(client, run_id, candidate_id, scheduled_for=just_past.isoformat())

    assert body["status"] == "ready" and body["scheduled_for"] is None


@pytest.mark.parametrize(
    "scheduled_for",
    [
        "2026-11-02T10:30:00",  # no timezone
        "not-a-datetime",
    ],
)
def test_an_invalid_schedule_returns_422(make_runtime, client_for, run_payload, scheduled_for):
    client = client_for(make_runtime())
    run_id, candidate_id = approved_run(client, run_payload)

    response = publish(client, run_id, candidate_id, scheduled_for=scheduled_for)

    assert response.status_code == 422


@pytest.mark.parametrize("days", [-1, 31])
def test_an_out_of_bounds_schedule_returns_422(make_runtime, client_for, run_payload, days):
    client = client_for(make_runtime())
    run_id, candidate_id = approved_run(client, run_payload)
    when = datetime.now(UTC) + timedelta(days=days)

    response = publish(client, run_id, candidate_id, scheduled_for=when.isoformat())

    assert response.status_code == 422
    assert "scheduled_for" in response.json()["detail"]


# --- 22. listing and status ------------------------------------------------------------


def test_listing_and_status_endpoints_filter_by_status_and_run(
    make_runtime, client_for, run_payload
):
    runtime = make_runtime()
    client = client_for(runtime)
    run_a, candidate_a = approved_run(client, run_payload)
    run_b, candidate_b = approved_run(client, run_payload)
    ready = request_publish(client, run_a, candidate_a)
    later = request_publish(
        client,
        run_b,
        candidate_b,
        scheduled_for=(datetime.now(UTC) + timedelta(days=2)).isoformat(),
    )

    everything = client.get("/publications").json()
    assert {p["id"] for p in everything} == {ready["id"], later["id"]}
    by_status = client.get("/publications", params={"status": "scheduled"}).json()
    assert [p["id"] for p in by_status] == [later["id"]]
    by_run = client.get("/publications", params={"run_id": run_a}).json()
    assert [p["id"] for p in by_run] == [ready["id"]]
    single = client.get(f"/publications/{ready['id']}").json()
    assert single["attempts"] == [] and single["status"] == "ready"
    assert [p["id"] for p in client.get(f"/runs/{run_a}").json()["publications"]] == [ready["id"]]


# --- 21. invalid state transitions -----------------------------------------------------


def test_retry_resolve_and_cancel_are_refused_in_the_wrong_state(
    make_runtime, client_for, run_payload
):
    runtime = make_runtime()
    client = client_for(runtime)
    run_id, candidate_id = approved_run(client, run_payload)
    publication = request_publish(client, run_id, candidate_id)
    pid = publication["id"]

    assert client.post(f"/publications/{pid}/retry", json={}).status_code == 409
    resolve = client.post(
        f"/publications/{pid}/resolve", json={"outcome": "not_published", "reviewer": "philipp"}
    )
    assert resolve.status_code == 409 and "only unknown" in resolve.json()["detail"]
    # Resolving as published without the post id never reaches the service.
    assert (
        client.post(
            f"/publications/{pid}/resolve", json={"outcome": "published", "reviewer": "p"}
        ).status_code
        == 422
    )

    cancelled = client.post(f"/publications/{pid}/cancel", json={"reviewer": "philipp"})
    assert cancelled.status_code == 202 and cancelled.json()["status"] == "cancelled"
    assert client.post(f"/publications/{pid}/cancel", json={}).status_code == 409


def test_a_published_publication_cannot_be_requested_retried_or_cancelled_again(
    make_runtime, client_for, run_payload
):
    publisher = MockPublisher()
    runtime = make_runtime(publisher=publisher)
    client = client_for(runtime)
    run_id, candidate_id = approved_run(client, run_payload)
    publication = request_publish(client, run_id, candidate_id)
    report = worker(runtime.db, publisher).run_once()
    assert report.published == (publication["id"],)

    assert publish(client, run_id, candidate_id).status_code == 409
    assert client.post(f"/publications/{publication['id']}/retry", json={}).status_code == 409
    assert client.post(f"/publications/{publication['id']}/cancel", json={}).status_code == 409


def test_a_cancelled_publication_is_revived_rather_than_duplicated(
    make_runtime, client_for, run_payload
):
    runtime = make_runtime()
    client = client_for(runtime)
    run_id, candidate_id = approved_run(client, run_payload)
    first = request_publish(client, run_id, candidate_id)
    client.post(f"/publications/{first['id']}/cancel", json={"reviewer": "philipp"})

    again = request_publish(client, run_id, candidate_id)

    assert again["id"] == first["id"] and again["status"] == "ready"
    assert sql(runtime.db.libpq_url, "SELECT count(*) FROM publications") == [(1,)]


def test_publishing_a_rejected_run_is_refused(make_runtime, client_for, run_payload):
    client = client_for(make_runtime(llm=make_llm(always(CritiqueVerdict.PASS))))
    created = client.post("/runs", json=run_payload())
    run_id = created.json()["id"]
    candidate_id = client.get(f"/runs/{run_id}").json()["review"]["candidate_ids"][0]
    client.post(f"/runs/{run_id}/review", json={"action": "reject", "reviewer": "philipp"})

    response = publish(client, run_id, candidate_id)

    assert response.status_code == 409
    assert "rejected" in response.json()["detail"]


# --- the embedded worker is opt-in -----------------------------------------------------


def test_the_api_process_publishes_nothing_unless_the_embedded_worker_is_enabled(
    make_runtime, client_for, run_payload
):
    publisher = MockPublisher()
    runtime = make_runtime(publisher=publisher)
    assert runtime.embedded_worker is None  # PUBLISHER_EMBEDDED_WORKER defaults to false
    client = client_for(runtime)  # the lifespan ran: startup recovery, no publishing
    run_id, candidate_id = approved_run(client, run_payload)
    publication = request_publish(client, run_id, candidate_id)

    # A second API process starting up must not publish queued work either.
    second = client_for(make_runtime(publisher=publisher))
    assert second.get(f"/publications/{publication['id']}").json()["status"] == "ready"
    assert publisher.calls == []


def test_the_embedded_worker_publishes_when_it_is_explicitly_enabled(
    make_runtime, client_for, run_payload
):
    publisher = MockPublisher()
    runtime = make_runtime(
        publisher=publisher, publisher_embedded_worker=True, publisher_poll_seconds=0.05
    )
    assert runtime.embedded_worker is not None
    client = client_for(runtime)
    run_id, candidate_id = approved_run(client, run_payload)
    publication = request_publish(client, run_id, candidate_id)

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if client.get(f"/publications/{publication['id']}").json()["status"] == "published":
            break
        time.sleep(0.05)

    body = client.get(f"/publications/{publication['id']}").json()
    assert body["status"] == "published" and body["provider_post_id"]
    assert len(publisher.calls) == 1
