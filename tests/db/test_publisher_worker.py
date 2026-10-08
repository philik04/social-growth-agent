"""The publisher worker against real PostgreSQL: claiming, leases, crashes and retries.

The guarantees under test, in the order the plan states them: the intent and a started
attempt row exist before any platform call; a published publication is never posted
again; an outcome that may or may not have created a post becomes ``unknown`` and is
never retried automatically; only a failure proven to have happened before the request
was sent, or a rate limit once its reset has passed, is retried, bounded; two workers
never claim the same row.
"""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from social_growth_agent.errors import (
    PublishNotSentError,
    PublishOutcomeUnknownError,
    PublishRejectedError,
)
from social_growth_agent.models import PublishFailureCategory
from social_growth_agent.persistence import publications as pubs
from social_growth_agent.providers.mocks import MockPublisher
from social_growth_agent.services.publications import PublicationService
from tests.db.conftest import sql
from tests.db.helpers import SimulatedCrash
from tests.db.publishing_helpers import approved_run, execute, worker, worker_on


def intent(client, run_id, candidate_id, **body):
    response = client.post(f"/runs/{run_id}/publish", json={"candidate_id": candidate_id, **body})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def ready_publication(make_runtime, client_for, run_payload, publisher=None, **settings):
    """An approved run with one publication intent waiting to be published."""
    publisher = publisher or MockPublisher()
    runtime = make_runtime(publisher=publisher, **settings)
    client = client_for(runtime)
    run_id, candidate_id = approved_run(client, run_payload)
    publication_id = intent(client, run_id, candidate_id)
    return runtime, client, publisher, publication_id


def row(url, publication_id, columns="status, attempt_count, provider_post_id, failure_category"):
    [values] = sql(url, f"SELECT {columns} FROM publications WHERE id = :id", id=publication_id)
    return values


def attempts(url, publication_id):
    return sql(
        url,
        "SELECT attempt, outcome, finished_at IS NOT NULL, failure_category, provider_post_id "
        "FROM publication_attempts WHERE publication_id = :id ORDER BY attempt",
        id=publication_id,
    )


# --- 6, 23. a successful publish, and the started ledger -------------------------------


def test_a_successful_publish_stores_the_post_id_and_a_finished_attempt(
    make_runtime, client_for, run_payload
):
    runtime, client, publisher, pid = ready_publication(make_runtime, client_for, run_payload)

    report = worker(runtime.db, publisher).run_once()

    assert report.published == (pid,) and report.calls_made == 1
    status, count, post_id, category = row(runtime.db.libpq_url, pid)
    assert (status, count, category) == ("published", 1, None)
    assert post_id == publisher.published[0].provider_post_id
    assert attempts(runtime.db.libpq_url, pid) == [(1, "succeeded", True, None, post_id)]
    body = client.get(f"/publications/{pid}").json()
    assert body["status"] == "published"
    assert body["provider_post_url"].endswith(post_id)
    assert body["published_at"] is not None
    assert len(publisher.calls) == 1
    assert publisher.calls[0].idempotency_key


def test_the_started_attempt_row_is_committed_before_the_platform_call(
    make_runtime, client_for, run_payload, database_url
):
    """Asserted from inside the publisher, over a separate connection, so it reads what
    another process would see while the call is in flight."""
    seen = {}

    def inspect(_request):
        seen["publication"] = row(database_url, pid)
        seen["attempts"] = attempts(database_url, pid)

    publisher = MockPublisher([inspect])
    runtime, _, publisher, pid = ready_publication(
        make_runtime, client_for, run_payload, publisher=publisher
    )

    worker(runtime.db, publisher).run_once()

    assert seen["publication"][:2] == ("publishing", 1)
    assert seen["attempts"] == [(1, "started", False, None, None)]


# --- 7, 9. never a second post ---------------------------------------------------------


def test_a_published_publication_is_never_posted_again(make_runtime, client_for, run_payload):
    runtime, client, publisher, pid = ready_publication(make_runtime, client_for, run_payload)
    publishing = worker(runtime.db, publisher)
    publishing.run_once()

    for _ in range(3):
        report = publishing.run_once()
        assert report.claimed == () and report.calls_made == 0
    assert len(publisher.calls) == 1
    assert client.post(f"/publications/{pid}/retry", json={}).status_code == 409


def test_a_crash_after_the_intent_but_before_the_call_publishes_exactly_once(
    make_runtime, client_for, run_payload, database_url
):
    runtime, _, publisher, pid = ready_publication(
        make_runtime, client_for, run_payload, publisher=MockPublisher([SimulatedCrash("died")])
    )
    first = worker(runtime.db, publisher, name="worker-dead", lease_seconds=10)
    with pytest.raises(SimulatedCrash):
        first.run_once()
    assert row(database_url, pid)[:2] == ("publishing", 1)  # visible as in flight

    # A second worker must not take an in-flight row, whatever it does later.
    second_publisher = MockPublisher()
    fresh, db = worker_on(database_url, second_publisher, name="worker-2")
    try:
        assert fresh.run_once().claimed == ()
        # Once the dead worker's lease expires the row becomes unknown, never ready.
        execute(
            database_url,
            "UPDATE publications SET lease_expires_at = now() - interval '1 second' WHERE id = :id",
            id=pid,
        )
        report = fresh.run_once()
        assert report.swept == (pid,) and report.claimed == ()
        assert row(database_url, pid)[0] == "unknown"
        assert fresh.run_once().calls_made == 0
    finally:
        db.dispose()
    assert second_publisher.calls == []


# --- 8. a crash before the call resumes safely -----------------------------------------


def test_a_claim_lost_before_the_call_is_simply_claimed_again_and_published_once(
    make_runtime, client_for, run_payload, database_url
):
    runtime, _, publisher, pid = ready_publication(make_runtime, client_for, run_payload)
    # A worker claimed the row and died before it could begin an attempt: still 'ready',
    # so nothing was sent and the lease simply expires.
    execute(
        database_url,
        "UPDATE publications SET claimed_by = 'worker-dead', claimed_at = now(), "
        "lease_expires_at = now() - interval '1 second' WHERE id = :id",
        id=pid,
    )

    report = worker(runtime.db, publisher, name="worker-2").run_once()

    assert report.published == (pid,) and len(publisher.calls) == 1
    assert row(database_url, pid)[:2] == ("published", 1)
    assert attempts(database_url, pid) == [
        (1, "succeeded", True, None, publisher.published[0].provider_post_id)
    ]


# --- 10, 24. ambiguous outcomes --------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (
            PublishOutcomeUnknownError(
                "timed out after sending", failure_category=PublishFailureCategory.TIMEOUT
            ),
            "timeout",
        ),
        (
            PublishOutcomeUnknownError(
                "HTTP 503", failure_category=PublishFailureCategory.SERVER_ERROR
            ),
            "server_error",
        ),
        (
            PublishOutcomeUnknownError(
                "no post id", failure_category=PublishFailureCategory.MALFORMED_RESPONSE
            ),
            "malformed_response",
        ),
    ],
)
def test_an_ambiguous_outcome_becomes_unknown_and_is_never_retried(
    make_runtime, client_for, run_payload, error, category
):
    runtime, client, publisher, pid = ready_publication(
        make_runtime, client_for, run_payload, publisher=MockPublisher([error])
    )
    publishing = worker(runtime.db, publisher)

    report = publishing.run_once()

    assert report.unknown == (pid,)
    status, count, post_id, failure = row(runtime.db.libpq_url, pid)
    assert (status, count, post_id, failure) == ("unknown", 1, None, category)
    assert publishing.run_once().calls_made == 0  # no automatic retry, ever
    assert client.post(f"/publications/{pid}/retry", json={}).status_code == 409
    assert len(publisher.calls) == 1
    assert attempts(runtime.db.libpq_url, pid) == [(1, "unknown", True, category, None)]


def test_an_unknown_publication_is_resolved_only_by_a_human(make_runtime, client_for, run_payload):
    runtime, client, publisher, pid = ready_publication(
        make_runtime,
        client_for,
        run_payload,
        publisher=MockPublisher(
            [
                PublishOutcomeUnknownError(
                    "timed out", failure_category=PublishFailureCategory.TIMEOUT
                )
            ]
        ),
    )
    worker(runtime.db, publisher).run_once()

    resolved = client.post(
        f"/publications/{pid}/resolve",
        json={
            "outcome": "published",
            "provider_post_id": "1908111111111111111",
            "reviewer": "philipp",
            "note": "found on the timeline",
        },
    )

    assert resolved.status_code == 202
    body = resolved.json()
    assert body["status"] == "published"
    assert body["provider_post_id"] == "1908111111111111111"
    assert body["resolved_by"] == "philipp" and body["resolution_note"] == "found on the timeline"
    assert worker(runtime.db, publisher).run_once().calls_made == 0
    assert len(publisher.calls) == 1  # the resolve itself posted nothing


def test_resolving_as_not_published_queues_the_publication_again(
    make_runtime, client_for, run_payload
):
    publisher = MockPublisher(
        [PublishOutcomeUnknownError("timed out", failure_category=PublishFailureCategory.TIMEOUT)]
    )
    runtime, client, publisher, pid = ready_publication(
        make_runtime, client_for, run_payload, publisher=publisher
    )
    worker(runtime.db, publisher).run_once()

    client.post(
        f"/publications/{pid}/resolve", json={"outcome": "not_published", "reviewer": "philipp"}
    )

    assert row(runtime.db.libpq_url, pid)[0] == "ready"
    report = worker(runtime.db, publisher).run_once()
    assert report.published == (pid,)
    assert len(publisher.calls) == 2  # exactly one more call, after the human decision
    assert [a[0] for a in attempts(runtime.db.libpq_url, pid)] == [1, 2]


# --- 11, 12, 13. retry classification --------------------------------------------------


@pytest.mark.parametrize(
    ("category", "retryable"),
    [
        (PublishFailureCategory.AUTH, True),
        (PublishFailureCategory.BAD_REQUEST, False),
        (PublishFailureCategory.DUPLICATE_CONTENT, False),
    ],
)
def test_definite_rejections_fail_without_retrying(
    make_runtime, client_for, run_payload, category, retryable
):
    error = PublishRejectedError(f"rejected ({category.value})", failure_category=category)
    runtime, client, publisher, pid = ready_publication(
        make_runtime, client_for, run_payload, publisher=MockPublisher([error])
    )
    publishing = worker(runtime.db, publisher)

    assert publishing.run_once().failed == (pid,)
    status, count, _post, failure = row(runtime.db.libpq_url, pid)
    assert (status, count, failure) == ("failed", 1, category.value)
    assert publishing.run_once().calls_made == 0  # never retried on its own
    assert len(publisher.calls) == 1

    # A manual retry is allowed only where fixing the cause can help.
    manual = client.post(f"/publications/{pid}/retry", json={"reviewer": "philipp"})
    assert manual.status_code == (202 if retryable else 409)


def rate_limited(reset_at):
    return PublishRejectedError(
        "rate limited",
        failure_category=PublishFailureCategory.RATE_LIMITED,
        rate_limit_reset_at=reset_at,
    )


def pass_the_reset(url, publication_id):
    """Simulate the clock passing the reset, without anyone waiting for it."""
    execute(
        url,
        "UPDATE publications SET retry_not_before = now() - interval '1 second' WHERE id = :id",
        id=publication_id,
    )


def test_a_rate_limit_waits_for_its_reset_without_sleeping_then_retries_on_its_own(
    make_runtime, client_for, run_payload
):
    reset = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
    runtime, client, publisher, pid = ready_publication(
        make_runtime, client_for, run_payload, publisher=MockPublisher([rate_limited(reset), None])
    )
    publishing = worker(runtime.db, publisher)

    started = datetime.now(UTC)
    assert publishing.run_once().requeued == (pid,)
    assert datetime.now(UTC) - started < timedelta(seconds=5)  # nothing waited for the reset

    body = client.get(f"/publications/{pid}").json()
    assert (body["status"], body["failure_category"]) == ("ready", "rate_limited")
    assert datetime.fromisoformat(body["rate_limit_reset_at"]) == reset
    assert datetime.fromisoformat(body["retry_not_before"]) == reset
    assert [a["outcome"] for a in body["attempts"]] == ["rejected"]

    # Before the reset the poller ignores the row: no claim, no call, not even due.
    for _ in range(3):
        cycle = publishing.run_once()
        assert (cycle.claimed, cycle.calls_made) == ((), 0)
    assert PublicationService(runtime.db).due_count() == 0
    assert PublicationService(runtime.db).due_count(now=reset) == 1
    with runtime.db.transaction() as session:
        just_before = reset - timedelta(seconds=1)
        assert (
            pubs.claim_due(session, worker_id="w", lease_seconds=60, limit=5, now=just_before) == []
        )
    # It is not ``failed``, so there is nothing to retry by hand.
    assert client.post(f"/publications/{pid}/retry", json={}).status_code == 409
    assert len(publisher.calls) == 1

    pass_the_reset(runtime.db.libpq_url, pid)
    assert publishing.run_once().published == (pid,)
    body = client.get(f"/publications/{pid}").json()
    assert (body["status"], body["attempt_count"], body["retry_not_before"]) == (
        "published",
        2,
        None,
    )
    assert [a["outcome"] for a in body["attempts"]] == ["rejected", "succeeded"]
    assert len(publisher.published) == 1


def test_rate_limit_retries_are_bounded_by_max_attempts(make_runtime, client_for, run_payload):
    reset = datetime.now(UTC) + timedelta(hours=1)
    runtime, client, publisher, pid = ready_publication(
        make_runtime,
        client_for,
        run_payload,
        publisher=MockPublisher([rate_limited(reset), rate_limited(reset), None]),
    )
    publishing = worker(runtime.db, publisher, max_attempts=2)
    url = runtime.db.libpq_url

    assert publishing.run_once().requeued == (pid,)
    pass_the_reset(url, pid)
    assert publishing.run_once().failed == (pid,)

    body = client.get(f"/publications/{pid}").json()
    assert (body["status"], body["attempt_count"], body["retry_not_before"]) == ("failed", 2, None)
    assert "no attempt left after 2 of 2" in body["failure_message"]
    pass_the_reset(url, pid)  # no gate is left to pass; the row stays failed
    assert publishing.run_once().calls_made == 0
    assert len(publisher.calls) == 2
    # The manual retry rule is unchanged: refused until the reset has passed.
    refused = client.post(f"/publications/{pid}/retry", json={})
    assert refused.status_code == 409 and "rate limited until" in refused.json()["detail"]


def test_a_rate_limit_without_a_reset_time_needs_a_manual_retry(
    make_runtime, client_for, run_payload
):
    runtime, client, publisher, pid = ready_publication(
        make_runtime, client_for, run_payload, publisher=MockPublisher([rate_limited(None)])
    )
    publishing = worker(runtime.db, publisher)

    assert publishing.run_once().failed == (pid,)
    assert publishing.run_once().calls_made == 0
    body = client.get(f"/publications/{pid}").json()
    assert (body["status"], body["retry_not_before"]) == ("failed", None)
    assert client.post(f"/publications/{pid}/retry", json={}).status_code == 202


def test_a_failure_before_the_request_was_sent_is_retried_up_to_the_bound(
    make_runtime, client_for, run_payload
):
    not_sent = PublishNotSentError(
        "connection refused", failure_category=PublishFailureCategory.NOT_SENT
    )
    publisher = MockPublisher([not_sent, not_sent, not_sent, None])
    runtime, client, publisher, pid = ready_publication(
        make_runtime, client_for, run_payload, publisher=publisher
    )
    publishing = worker(runtime.db, publisher, max_attempts=3)

    assert publishing.run_once().requeued == (pid,)
    assert row(runtime.db.libpq_url, pid)[:2] == ("ready", 1)
    assert publishing.run_once().requeued == (pid,)
    third = publishing.run_once()

    assert third.failed == (pid,) and third.requeued == ()
    status, count, _post, failure = row(runtime.db.libpq_url, pid)
    assert (status, count, failure) == ("failed", 3, "not_sent")
    assert len(publisher.calls) == 3  # bounded: never a fourth call
    assert publishing.run_once().calls_made == 0
    detail = client.get(f"/publications/{pid}").json()["failure_message"]
    assert "no attempt left after 3 of 3" in detail
    assert [a[1] for a in attempts(runtime.db.libpq_url, pid)] == ["not_sent"] * 3


def test_an_unexpected_publisher_error_is_treated_as_unknown(make_runtime, client_for, run_payload):
    runtime, _, publisher, pid = ready_publication(
        make_runtime,
        client_for,
        run_payload,
        publisher=MockPublisher([RuntimeError("a bug in the publisher")]),
    )

    report = worker(runtime.db, publisher).run_once()

    assert report.unknown == (pid,)
    assert row(runtime.db.libpq_url, pid)[3] == "internal"


# --- 15, 16. the scheduler claims due work only ----------------------------------------


def test_a_future_publication_is_ignored_until_it_is_due(make_runtime, client_for, run_payload):
    publisher = MockPublisher()
    runtime = make_runtime(publisher=publisher)
    client = client_for(runtime)
    run_id, candidate_id = approved_run(client, run_payload)
    later = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
    pid = intent(client, run_id, candidate_id, scheduled_for=later)
    publishing = worker(runtime.db, publisher)

    assert publishing.run_once().claimed == ()
    assert publisher.calls == []
    assert row(runtime.db.libpq_url, pid)[0] == "scheduled"

    # The same worker, once the scheduled time has passed.
    execute(
        runtime.db.libpq_url,
        "UPDATE publications SET scheduled_for = now() - interval '1 minute' WHERE id = :id",
        id=pid,
    )
    assert publishing.run_once().published == (pid,)
    assert len(publisher.calls) == 1


def test_only_due_publications_are_claimed_and_the_earliest_goes_first(
    make_runtime, client_for, run_payload
):
    publisher = MockPublisher()
    runtime = make_runtime(publisher=publisher)
    client = client_for(runtime)
    due_ids = []
    for offset in (-20, -5):
        # Requested for a future slot, then backdated so it is due (the API refuses a
        # schedule further in the past than the tolerance).
        run_id, candidate_id = approved_run(client, run_payload)
        pid = intent(
            client,
            run_id,
            candidate_id,
            scheduled_for=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
        execute(
            runtime.db.libpq_url,
            "UPDATE publications SET scheduled_for = :when WHERE id = :id",
            when=datetime.now(UTC) + timedelta(minutes=offset),
            id=pid,
        )
        due_ids.append(pid)
    future_run, future_candidate = approved_run(client, run_payload)
    future = intent(
        client,
        future_run,
        future_candidate,
        scheduled_for=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    cancelled_run, cancelled_candidate = approved_run(client, run_payload)
    cancelled = intent(client, cancelled_run, cancelled_candidate)
    client.post(f"/publications/{cancelled}/cancel", json={})

    report = worker(runtime.db, publisher).run_once()

    assert report.claimed == tuple(due_ids)  # oldest schedule first
    assert future not in report.claimed and cancelled not in report.claimed
    assert row(runtime.db.libpq_url, future)[0] == "scheduled"
    assert row(runtime.db.libpq_url, cancelled)[0] == "cancelled"


# --- 17, 18. two workers -------------------------------------------------------------


def test_two_workers_never_claim_or_publish_the_same_publication(
    make_runtime, client_for, run_payload, database_url
):
    publisher_a, publisher_b = MockPublisher(), MockPublisher()
    runtime = make_runtime(publisher=publisher_a)
    client = client_for(runtime)
    ids = []
    for _ in range(6):
        run_id, candidate_id = approved_run(client, run_payload)
        ids.append(intent(client, run_id, candidate_id))

    # Two workers, each on its own connection pool, racing on real threads.
    first, db_a = worker_on(database_url, publisher_a, name="worker-a")
    second, db_b = worker_on(database_url, publisher_b, name="worker-b")
    reports: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def run(label, which):
        barrier.wait()
        reports[label] = which.run_once()

    try:
        threads = [
            threading.Thread(target=run, args=("a", first)),
            threading.Thread(target=run, args=("b", second)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    finally:
        db_a.dispose()
        db_b.dispose()

    claimed_a = set(reports["a"].claimed)
    claimed_b = set(reports["b"].claimed)
    assert claimed_a | claimed_b == set(ids)
    assert claimed_a & claimed_b == set()  # SKIP LOCKED: never the same row
    posts = [c.candidate_id for c in (*publisher_a.calls, *publisher_b.calls)]
    assert len(posts) == len(set(posts)) == 6  # exactly one call per publication
    published = sql(database_url, "SELECT count(*) FROM publications WHERE status = 'published'")
    assert published == [(6,)]
    per_publication = sql(
        database_url,
        "SELECT publication_id, count(*) FROM publication_attempts GROUP BY publication_id",
    )
    assert {count for _pid, count in per_publication} == {1}


def test_an_expired_lease_on_an_unsent_publication_is_reclaimable(
    make_runtime, client_for, run_payload, database_url
):
    runtime, _, publisher, pid = ready_publication(make_runtime, client_for, run_payload)
    unexpired = worker(runtime.db, publisher, name="worker-a", lease_seconds=600)
    assert unexpired.run_once.__self__ is unexpired  # sanity: same worker object reused

    # Worker A claims the row and then stops (without starting an attempt).
    with runtime.db.transaction() as session:
        from social_growth_agent.persistence import publications as pubs

        assert pubs.claim_due(session, worker_id="worker-a", lease_seconds=600, limit=5) == [pid]

    other_publisher = MockPublisher()
    other, db = worker_on(database_url, other_publisher, name="worker-b")
    try:
        assert other.run_once().claimed == ()  # the lease is still valid
        assert other_publisher.calls == []
        execute(
            database_url,
            "UPDATE publications SET lease_expires_at = now() - interval '1 second' WHERE id = :id",
            id=pid,
        )
        report = other.run_once()
    finally:
        db.dispose()

    assert report.published == (pid,)
    assert len(other_publisher.calls) == 1 and publisher.calls == []
    [(claimed_by,)] = sql(
        database_url, "SELECT claimed_by FROM publications WHERE id = :id", id=pid
    )
    assert claimed_by is None  # the lease is released when the outcome is recorded


def test_the_worker_refuses_to_publish_when_the_policy_no_longer_allows_it(
    make_runtime, client_for, run_payload
):
    runtime, _, publisher, pid = ready_publication(make_runtime, client_for, run_payload)
    # The stored content and the candidate drift apart (a database edit, not our code).
    execute(
        runtime.db.libpq_url,
        "UPDATE content_candidates SET content = 'tampered' WHERE id = "
        "(SELECT candidate_id FROM publications WHERE id = :id)",
        id=pid,
    )

    report = worker(runtime.db, publisher).run_once()

    assert report.failed == (pid,)
    status, count, _post, category = row(runtime.db.libpq_url, pid)
    assert (status, count, category) == ("failed", 0, "policy")
    assert publisher.calls == []  # refused before any call


def test_the_service_reports_due_work_without_publishing(make_runtime, client_for, run_payload):
    runtime, _, publisher, _pid = ready_publication(make_runtime, client_for, run_payload)

    assert PublicationService(runtime.db).due_count() == 1
    assert publisher.calls == []
