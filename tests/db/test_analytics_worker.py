"""The analytics worker against real PostgreSQL: scheduling, claiming, leases, crashes,
the retry policy and the reconciliation of the verified creation time.

The guarantees under test: jobs exist only for published posts and are created by
publishing (or an explicit backfill), never by the worker; each (publication, age) has
at most one job and one snapshot; a started request and attempt row exist before every
provider call; two workers never collect the same job; a dead worker's jobs come back;
retries are bounded and wait in ``retry_not_before`` without sleeping; a missing metric
stays NULL; a late capture is never presented as taken at its target age.
"""

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from social_growth_agent.errors import (
    AnalyticsError,
    PublishNotSentError,
    PublishOutcomeUnknownError,
    PublishRejectedError,
)
from social_growth_agent.models import (
    AnalyticsFailureCategory,
    AnalyticsFetch,
    PostMetricMiss,
    PostMetricRecord,
    PublishFailureCategory,
)
from social_growth_agent.persistence import Database
from social_growth_agent.persistence import analytics as adb
from social_growth_agent.policies.analytics import parse_snapshot_ages
from social_growth_agent.providers.mocks import MockAnalyticsProvider, MockPublisher
from social_growth_agent.services.analytics import AnalyticsSchedule, AnalyticsService
from social_growth_agent.services.publisher import PublisherWorker
from social_growth_agent.services.runtime import build_analytics_worker
from tests.db.analytics_helpers import (
    HOUR,
    analytics_worker,
    count,
    intent,
    job,
    job_id,
    jobs,
    post_id,
    published_at,
    published_runtime,
)
from tests.db.conftest import sql
from tests.db.helpers import SimulatedCrash
from tests.db.publishing_helpers import approved_run, execute, publish_settings, worker

AGES = [("1h",), ("24h",), ("72h",)]


def error(category, message="boom", **kwargs) -> AnalyticsError:
    return AnalyticsError(message, failure_category=category, **kwargs)


# --- 1, 2. jobs are created by publishing, and only for published posts -----------------


def test_publishing_enqueues_one_job_per_age_from_the_recorded_time(
    make_runtime, client_for, run_payload
):
    runtime, _client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    url = runtime.db.libpq_url
    at = published_at(url, pid)

    rows = jobs(url, pid, "snapshot_age, status, schedule_basis, basis_at, scheduled_for, origin")
    assert [r[0] for r in rows] == ["1h", "24h", "72h"]
    for age, status, basis, basis_at, scheduled_for, origin in rows:
        hours = int(age.removesuffix("h"))
        assert (status, basis, origin) == ("scheduled", "recorded_published_at", "publish")
        assert basis_at == at
        assert scheduled_for == at + timedelta(hours=hours)
    assert count(url, "post_metrics") == 0 and count(url, "analytics_requests") == 0


def test_resolving_an_unknown_publication_as_published_enqueues_jobs(
    make_runtime, client_for, run_payload
):
    publisher = MockPublisher(
        [PublishOutcomeUnknownError("timed out", failure_category=PublishFailureCategory.TIMEOUT)]
    )
    runtime = make_runtime(publisher=publisher)
    client = client_for(runtime)
    run_id, candidate_id = approved_run(client, run_payload)
    pid = intent(client, run_id, candidate_id)
    runtime.worker.run_once()
    url = runtime.db.libpq_url
    assert count(url, "analytics_jobs") == 0  # unknown: nothing scheduled

    resolved = client.post(
        f"/publications/{pid}/resolve",
        json={"outcome": "published", "provider_post_id": "1908111111111111111", "reviewer": "p"},
    )

    assert resolved.status_code == 202
    assert jobs(url, pid) == [(a, "scheduled") for (a,) in AGES]
    # The resolve time is the only time known; it is labelled as the recorded time.
    assert {r[0] for r in jobs(url, pid, "schedule_basis")} == {"recorded_published_at"}
    assert published_at(url, pid) is not None


@pytest.mark.parametrize(
    "script",
    [
        [],  # left ready (never run)
        [PublishRejectedError("dup", failure_category=PublishFailureCategory.DUPLICATE_CONTENT)],
        [PublishOutcomeUnknownError("t", failure_category=PublishFailureCategory.TIMEOUT)],
    ],
)
def test_no_jobs_for_publications_that_are_not_published(
    make_runtime, client_for, run_payload, script
):
    publisher = MockPublisher(script)
    runtime = make_runtime(publisher=publisher)
    client = client_for(runtime)
    run_id, candidate_id = approved_run(client, run_payload)
    pid = intent(client, run_id, candidate_id)
    if script:
        runtime.worker.run_once()

    assert count(runtime.db.libpq_url, "analytics_jobs") == 0
    backfill = client.post(f"/publications/{pid}/analytics/backfill")
    assert backfill.status_code == 409
    assert "only published posts" in backfill.json()["detail"]


def test_a_cancelled_publication_gets_no_jobs(make_runtime, client_for, run_payload):
    runtime = make_runtime()
    client = client_for(runtime)
    run_id, candidate_id = approved_run(client, run_payload)
    pid = intent(client, run_id, candidate_id)
    assert client.post(f"/publications/{pid}/cancel", json={}).status_code == 202
    assert client.post(f"/publications/{pid}/analytics/backfill").status_code == 409
    assert count(runtime.db.libpq_url, "analytics_jobs") == 0


def test_enqueue_on_publish_can_be_disabled_and_the_worker_never_backfills(
    make_runtime, client_for, run_payload
):
    runtime, _client, [pid] = published_runtime(
        make_runtime, client_for, run_payload, analytics_enqueue_on_publish=False
    )
    url = runtime.db.libpq_url
    provider = MockAnalyticsProvider()
    # Starting the configured worker and running it far in the future creates nothing.
    settings = publish_settings(analytics_enqueue_on_publish=False)
    started = build_analytics_worker(settings, runtime.db, provider=provider)
    report = started.run_once(now=published_at(url, pid) + timedelta(days=30))

    assert report.claimed == () and provider.calls == []
    assert count(url, "analytics_jobs") == 0 and count(url, "analytics_requests") == 0


# --- 3. uniqueness ----------------------------------------------------------------------


def test_the_database_refuses_a_second_job_or_snapshot_for_the_same_age(
    make_runtime, client_for, run_payload
):
    runtime, _client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    url = runtime.db.libpq_url
    analytics_worker(runtime.db).run_once(now=published_at(url, pid) + HOUR)
    first = job_id(url, pid, "1h")

    with pytest.raises(IntegrityError, match="uq_analytics_jobs_target"):
        execute(
            url,
            "INSERT INTO analytics_jobs (id, publication_id, snapshot_age, age_seconds, "
            "schedule_basis, basis_at, scheduled_for, origin, status, attempt_count, "
            "attempt_base, created_at, updated_at) SELECT 'ajob_dup', publication_id, "
            "snapshot_age, age_seconds, schedule_basis, basis_at, scheduled_for, origin, "
            "'scheduled', 0, 0, now(), now() FROM analytics_jobs WHERE id = :id",
            id=first,
        )
    with pytest.raises(IntegrityError, match="uq_post_metrics_job_id"):
        execute(
            url,
            "INSERT INTO post_metrics (id, job_id, publication_id, request_id, platform, "
            "provider, metrics_scope, provider_post_id, snapshot_age, target_age_seconds, "
            "scheduled_for, captured_at) SELECT 'pmet_dup', job_id, publication_id, "
            "request_id, platform, provider, metrics_scope, provider_post_id, snapshot_age, "
            "target_age_seconds, scheduled_for, captured_at FROM post_metrics",
        )
    assert count(url, "post_metrics") == 1


def test_enqueueing_again_is_idempotent(make_runtime, client_for, run_payload):
    runtime, _client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    before = jobs(runtime.db.libpq_url, pid, "id, scheduled_for")
    with runtime.db.transaction() as session:
        result = adb.enqueue_jobs(session, pid, AnalyticsSchedule().ages, origin="backfill")
    assert result.created == [] and len(result.existing) == 3
    assert jobs(runtime.db.libpq_url, pid, "id, scheduled_for") == before


# --- 4-7. due, future, races, leases ----------------------------------------------------


def test_a_future_job_is_not_claimed_and_nothing_is_read(make_runtime, client_for, run_payload):
    runtime, _client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    provider = MockAnalyticsProvider()
    report = analytics_worker(runtime.db, provider).run_once(
        now=published_at(runtime.db.libpq_url, pid) + timedelta(minutes=59)
    )
    assert report.claimed == () and report.requests == 0 and provider.calls == []


def test_a_due_job_is_collected_with_its_timing_and_ledger(make_runtime, client_for, run_payload):
    runtime, client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    url = runtime.db.libpq_url
    provider = MockAnalyticsProvider()
    at = published_at(url, pid)
    now = at + HOUR + timedelta(seconds=30)

    report = analytics_worker(runtime.db, provider).run_once(now=now)

    first = job_id(url, pid, "1h")
    assert report.claimed == (first,) and report.collected == (first,)
    assert provider.calls == [[post_id(url, pid)]]
    assert jobs(url, pid) == [("1h", "collected"), ("24h", "scheduled"), ("72h", "scheduled")]
    assert sql(
        url, "SELECT outcome, finished_at IS NOT NULL, posts_returned FROM analytics_requests"
    ) == [("succeeded", True, 1)]
    assert sql(url, "SELECT attempt, outcome FROM analytics_attempts") == [(1, "collected")]
    [snapshot] = client.get(f"/publications/{pid}/metrics").json()
    assert snapshot["snapshot_age"] == "1h" and snapshot["target_age_seconds"] == 3600
    assert snapshot["capture_delay_seconds"] == 30.0
    assert snapshot["age_basis"] == "recorded_published_at"
    assert snapshot["actual_age_seconds"] == 3630.0 and snapshot["on_target"] is True
    expected = provider.metrics_for(post_id(url, pid))
    assert (snapshot["likes"], snapshot["impressions"]) == (expected.likes, expected.impressions)


def test_two_workers_never_collect_the_same_job(
    make_runtime, client_for, run_payload, database_url
):
    _runtime, _client, ids = published_runtime(make_runtime, client_for, run_payload, count=3)
    now = max(published_at(database_url, p) for p in ids) + timedelta(hours=73)
    db_a, db_b = Database.connect(database_url), Database.connect(database_url)
    provider_a, provider_b = MockAnalyticsProvider(), MockAnalyticsProvider()
    first = analytics_worker(db_a, provider_a, name="a", batch_size=4)
    second = analytics_worker(db_b, provider_b, name="b", batch_size=4)
    reports: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def run(label, which):
        barrier.wait()
        reports[label] = which.run_once(now=now)

    try:
        threads = [
            threading.Thread(target=run, args=("a", first)),
            threading.Thread(target=run, args=("b", second)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        # Whatever was left (batch size 4 each, 9 jobs) goes to a later cycle.
        rest = first.run_once(now=now)
    finally:
        db_a.dispose()
        db_b.dispose()

    claimed_a = set(reports["a"].claimed) | set(rest.claimed)
    claimed_b = set(reports["b"].claimed)
    assert claimed_a & claimed_b == set()
    assert len(claimed_a | claimed_b) == 9
    assert count(database_url, "post_metrics") == 9
    assert count(database_url, "analytics_attempts") == 9  # one attempt per job, no repeat
    per_target = sql(
        database_url,
        "SELECT count(*) FROM post_metrics GROUP BY publication_id, snapshot_age",
    )
    assert {c for (c,) in per_target} == {1}


def test_a_claim_whose_lease_expired_before_the_call_is_claimed_again(
    make_runtime, client_for, run_payload
):
    runtime, _client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    url = runtime.db.libpq_url
    now = published_at(url, pid) + HOUR
    with runtime.db.transaction() as session:  # a worker claims, then dies before beginning
        claimed = adb.claim_due(session, worker_id="dead", lease_seconds=60, limit=5, now=now)
    assert claimed == [job_id(url, pid, "1h")]

    provider = MockAnalyticsProvider()
    assert (
        analytics_worker(runtime.db, provider).run_once(now=now + timedelta(seconds=30)).claimed
        == ()
    )
    report = analytics_worker(runtime.db, provider).run_once(now=now + timedelta(seconds=61))
    assert report.collected == tuple(claimed) and len(provider.calls) == 1


# --- 8, 9. NULL stays NULL, 0 stays 0 ---------------------------------------------------


def test_metrics_the_platform_does_not_report_stay_null(make_runtime, client_for, run_payload):
    runtime, client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    url = runtime.db.libpq_url
    provider = MockAnalyticsProvider(omit_impressions=True)
    analytics_worker(runtime.db, provider).run_once(now=published_at(url, pid) + HOUR)

    assert sql(url, "SELECT impressions, likes IS NOT NULL FROM post_metrics") == [(None, True)]
    [snapshot] = client.get(f"/publications/{pid}/metrics").json()
    assert snapshot["impressions"] is None
    derived = snapshot["derived"]
    assert derived["observed_engagement_count"] is not None
    assert derived["observed_engagement_rate_by_impressions"] is None
    assert derived["observed_reply_rate"] is None


def test_zeros_reported_by_the_platform_are_stored_as_zero(make_runtime, client_for, run_payload):
    runtime, client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    url = runtime.db.libpq_url
    record = PostMetricRecord(
        provider_post_id=post_id(url, pid),
        likes=0,
        reposts=0,
        replies=0,
        quotes=0,
        bookmarks=0,
        impressions=0,
    )
    provider = MockAnalyticsProvider([AnalyticsFetch(records=[record], posts_returned=1)])
    analytics_worker(runtime.db, provider).run_once(now=published_at(url, pid) + HOUR)

    assert sql(
        url, "SELECT likes, reposts, replies, quotes, bookmarks, impressions FROM post_metrics"
    ) == [(0, 0, 0, 0, 0, 0)]
    [snapshot] = client.get(f"/publications/{pid}/metrics").json()
    assert snapshot["derived"]["observed_engagement_count"] == 0
    # 0 impressions: a rate would divide by zero, so it is unknown rather than 0.
    assert snapshot["derived"]["observed_engagement_rate_by_impressions"] is None


# --- 12-19. the retry policy ------------------------------------------------------------


def due(make_runtime, client_for, run_payload, provider, **worker_kwargs):
    """A published post whose 1h job is due at ``now``, and a worker for it."""
    runtime, client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    url = runtime.db.libpq_url
    now = published_at(url, pid) + HOUR
    return runtime, client, pid, url, now, analytics_worker(runtime.db, provider, **worker_kwargs)


def test_an_auth_failure_needs_a_manual_retry(make_runtime, client_for, run_payload):
    provider = MockAnalyticsProvider([error(AnalyticsFailureCategory.AUTH, "HTTP 401")])
    _runtime, client, pid, url, now, w = due(make_runtime, client_for, run_payload, provider)

    report = w.run_once(now=now)
    first = job_id(url, pid, "1h")
    assert report.failed == (first,) and report.retrying == ()
    assert job(url, first, "status, failure_category, retry_not_before") == (
        "failed",
        "auth",
        None,
    )
    later = now + timedelta(hours=12)  # before the 24h job is due
    assert w.run_once(now=later).claimed == ()  # never on its own

    retried = client.post(f"/analytics/jobs/{first}/retry")
    assert retried.status_code == 202 and retried.json()["status"] == "scheduled"
    assert w.run_once(now=later).collected == (first,)
    assert job(url, first, "attempt_count, attempt_base") == (2, 1)
    assert sql(
        url,
        "SELECT attempt, outcome FROM analytics_attempts WHERE job_id = :id ORDER BY attempt",
        id=first,
    ) == [(1, "failed"), (2, "collected")]


def test_a_rate_limit_waits_for_its_reset_without_sleeping(make_runtime, client_for, run_payload):
    reset_after = timedelta(minutes=15)
    provider = MockAnalyticsProvider()
    _runtime, _client, pid, url, now, w = due(make_runtime, client_for, run_payload, provider)
    reset = now + reset_after
    provider._script.append(
        error(
            AnalyticsFailureCategory.RATE_LIMITED,
            "HTTP 429",
            http_status=429,
            rate_limit_reset_at=reset,
        )
    )
    first = job_id(url, pid, "1h")

    started = time.monotonic()
    report = w.run_once(now=now)
    assert time.monotonic() - started < 5  # nothing slept for fifteen minutes

    assert report.retrying == (first,) and report.retry_not_before == {first: reset}
    assert job(url, first, "status, retry_not_before, rate_limit_reset_at") == (
        "scheduled",
        reset,
        reset,
    )
    assert sql(
        url,
        "SELECT outcome, error_category, http_status, rate_limit_reset_at FROM analytics_requests",
    ) == [("failed", "rate_limited", 429, reset)]
    assert w.run_once(now=reset - timedelta(seconds=1)).claimed == ()
    assert w.run_once(now=reset).collected == (first,)


def test_a_rate_limit_without_a_reset_backs_off_and_is_bounded(
    make_runtime, client_for, run_payload
):
    limited = error(AnalyticsFailureCategory.RATE_LIMITED, "HTTP 429", http_status=429)
    provider = MockAnalyticsProvider([limited, limited, limited])
    _runtime, client, pid, url, now, w = due(
        make_runtime, client_for, run_payload, provider, max_attempts=3
    )
    first = job_id(url, pid, "1h")

    assert w.run_once(now=now).retry_not_before == {first: now + timedelta(seconds=60)}
    second = now + timedelta(seconds=60)
    assert w.run_once(now=second).retry_not_before == {first: second + timedelta(seconds=120)}
    third = w.run_once(now=second + timedelta(seconds=120))
    assert third.failed == (first,)
    status, category, message = job(url, first, "status, failure_category, failure_message")
    assert (status, category) == ("failed", "rate_limited")
    assert "no attempt left after 3" in message
    assert len(provider.calls) == 3
    # Out of automatic attempts, a person may queue it again with a fresh budget.
    assert client.post(f"/analytics/jobs/{first}/retry").status_code == 202


@pytest.mark.parametrize(
    "category", [AnalyticsFailureCategory.TIMEOUT, AnalyticsFailureCategory.TRANSIENT_SERVER]
)
def test_timeouts_and_server_errors_back_off_exponentially(
    make_runtime, client_for, run_payload, category
):
    provider = MockAnalyticsProvider([error(category), error(category)])
    _runtime, _client, pid, url, now, w = due(
        make_runtime, client_for, run_payload, provider, max_attempts=5
    )
    first = job_id(url, pid, "1h")

    one = w.run_once(now=now)
    assert one.retry_not_before == {first: now + timedelta(seconds=60)}
    assert w.run_once(now=now + timedelta(seconds=59)).claimed == ()
    two = w.run_once(now=now + timedelta(seconds=60))
    assert two.retry_not_before == {first: now + timedelta(seconds=60 + 120)}
    assert w.run_once(now=now + timedelta(seconds=180)).collected == (first,)
    assert sql(
        url, "SELECT outcome, failure_category FROM analytics_attempts ORDER BY attempt"
    ) == [
        ("failed", category.value),
        ("failed", category.value),
        ("collected", None),
    ]


def test_a_bad_request_is_terminal_and_cannot_be_retried(make_runtime, client_for, run_payload):
    provider = MockAnalyticsProvider([error(AnalyticsFailureCategory.BAD_REQUEST, "HTTP 400")])
    _runtime, client, pid, url, now, w = due(make_runtime, client_for, run_payload, provider)
    first = job_id(url, pid, "1h")

    assert w.run_once(now=now).failed == (first,)
    retried = client.post(f"/analytics/jobs/{first}/retry")
    assert retried.status_code == 409 and "cannot succeed" in retried.json()["detail"]


def test_a_deleted_post_fails_its_job_and_cancels_the_later_ones(
    make_runtime, client_for, run_payload
):
    runtime, _client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    url = runtime.db.libpq_url
    provider = MockAnalyticsProvider(deleted=[post_id(url, pid)])
    report = analytics_worker(runtime.db, provider).run_once(now=published_at(url, pid) + HOUR)

    assert report.failed == (job_id(url, pid, "1h"),)
    assert set(report.cancelled) == {job_id(url, pid, "24h"), job_id(url, pid, "72h")}
    assert jobs(url, pid, "snapshot_age, status, failure_category") == [
        ("1h", "failed", "not_found"),
        ("24h", "cancelled", "not_found"),
        ("72h", "cancelled", "not_found"),
    ]
    assert sql(url, "SELECT outcome FROM analytics_requests") == [("partial",)]
    assert sql(url, "SELECT outcome FROM analytics_attempts") == [("not_found",)]
    assert count(url, "post_metrics") == 0
    later = analytics_worker(runtime.db, provider).run_once(
        now=published_at(url, pid) + timedelta(days=5)
    )
    assert later.claimed == () and len(provider.calls) == 1


def test_a_post_missing_from_a_successful_response_is_retried_and_bounded(
    make_runtime, client_for, run_payload
):
    empty = AnalyticsFetch(records=[], posts_returned=0)
    provider = MockAnalyticsProvider([empty, empty])
    _runtime, _client, pid, url, now, w = due(
        make_runtime, client_for, run_payload, provider, max_attempts=2
    )
    first = job_id(url, pid, "1h")

    assert w.run_once(now=now).retrying == (first,)
    assert job(url, first, "failure_category") == ("malformed_response",)
    assert w.run_once(now=now + timedelta(seconds=60)).failed == (first,)


def test_an_unexpected_provider_exception_is_recorded_and_retried(
    make_runtime, client_for, run_payload
):
    provider = MockAnalyticsProvider([RuntimeError("bug in the provider")])
    _runtime, _client, pid, url, now, w = due(make_runtime, client_for, run_payload, provider)
    first = job_id(url, pid, "1h")

    assert w.run_once(now=now).retrying == (first,)
    assert sql(url, "SELECT outcome, error_category FROM analytics_requests") == [
        ("failed", "unknown")
    ]


# --- 20. one batch for several publications --------------------------------------------


def test_a_batch_maps_shuffled_results_back_to_each_publication(
    make_runtime, client_for, run_payload
):
    runtime, _client, ids = published_runtime(make_runtime, client_for, run_payload, count=3)
    url = runtime.db.libpq_url
    posts = {pid: post_id(url, pid) for pid in ids}
    now = max(published_at(url, p) for p in ids) + HOUR
    a, b, c = ids
    base = MockAnalyticsProvider()

    def shuffled(post_ids):
        assert sorted(post_ids) == sorted(posts.values())  # one request for all three
        return AnalyticsFetch(
            records=[
                base.metrics_for(posts[c]).model_copy(update={"likes": 3}),
                base.metrics_for(posts[a]).model_copy(update={"likes": 1}),
            ],
            misses=[],
            posts_returned=2,
        )

    provider = MockAnalyticsProvider([shuffled])
    report = analytics_worker(runtime.db, provider).run_once(now=now)

    assert len(provider.calls) == 1 and report.requests == 1
    assert set(report.collected) == {job_id(url, a, "1h"), job_id(url, c, "1h")}
    assert report.retrying == (job_id(url, b, "1h"),)
    stored = dict(sql(url, "SELECT publication_id, likes FROM post_metrics"))
    assert stored == {a: 1, c: 3}
    assert job(url, job_id(url, b, "1h"), "failure_category") == ("malformed_response",)


# --- 21-23. crashes ---------------------------------------------------------------------


def test_a_crash_during_the_call_leaves_a_visible_unfinished_request_and_recovers(
    make_runtime, client_for, run_payload
):
    provider = MockAnalyticsProvider([SimulatedCrash()])
    runtime, client, pid, url, now, w = due(make_runtime, client_for, run_payload, provider)
    first = job_id(url, pid, "1h")

    with pytest.raises(SimulatedCrash):
        w.run_once(now=now)

    # Committed before the call: the job is in flight, and the read is on the ledger.
    assert job(url, first, "status, attempt_count") == ("collecting", 1)
    assert sql(url, "SELECT outcome, finished_at FROM analytics_requests") == [("started", None)]
    assert sql(url, "SELECT outcome FROM analytics_attempts") == [("started",)]
    assert client.get(f"/analytics/jobs/{first}").json()["status"] == "collecting"

    restarted = analytics_worker(runtime.db, provider, name="analytics-2")
    assert restarted.run_once(now=now + timedelta(seconds=30)).swept == ()  # lease still held
    swept = restarted.run_once(now=now + timedelta(seconds=61))
    assert swept.swept == (first,) and swept.claimed == ()
    assert job(url, first, "status, failure_category") == ("scheduled", "lease_expired")
    assert sql(url, "SELECT outcome, failure_category FROM analytics_attempts") == [
        ("failed", "lease_expired")
    ]
    collected = restarted.run_once(now=now + timedelta(seconds=61 + 60))
    assert collected.collected == (first,)
    assert count(url, "post_metrics") == 1
    # The dead process's request stays visible as started and unfinished.
    assert sql(url, "SELECT outcome FROM analytics_requests ORDER BY started_at") == [
        ("started",),
        ("succeeded",),
    ]


def test_a_crash_after_the_call_repeats_the_read_and_stores_one_snapshot(
    make_runtime, client_for, run_payload, monkeypatch
):
    provider = MockAnalyticsProvider()
    runtime, _client, pid, url, now, w = due(make_runtime, client_for, run_payload, provider)
    first = job_id(url, pid, "1h")

    def crash(*args, **kwargs):
        raise SimulatedCrash

    with monkeypatch.context() as patch:
        patch.setattr(adb, "record_snapshot", crash)
        with pytest.raises(SimulatedCrash):
            w.run_once(now=now)

    assert len(provider.calls) == 1
    assert count(url, "post_metrics") == 0  # the result transaction rolled back
    assert job(url, first, "status") == ("collecting",)
    assert sql(url, "SELECT finished_at FROM analytics_requests") == [(None,)]

    later = analytics_worker(runtime.db, provider, name="analytics-2")
    later.run_once(now=now + timedelta(seconds=61))  # sweep
    assert later.run_once(now=now + timedelta(seconds=121)).collected == (first,)
    assert len(provider.calls) == 2  # a read is safe to repeat
    assert count(url, "post_metrics") == 1


def test_a_worker_that_lost_its_job_stores_nothing(make_runtime, client_for, run_payload):
    """A slow worker whose lease was swept and re-collected by another stores nothing."""
    runtime, _client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    url = runtime.db.libpq_url
    now = published_at(url, pid) + HOUR
    first = job_id(url, pid, "1h")
    other = analytics_worker(runtime.db, name="fast")

    def slow(post_ids):
        # While this worker is "in the call", its lease expires and another worker
        # sweeps and collects the same job.
        other.run_once(now=now + timedelta(seconds=61))
        other.run_once(now=now + timedelta(seconds=121))

    slow_worker = analytics_worker(runtime.db, MockAnalyticsProvider([slow]), name="slow")
    report = slow_worker.run_once(now=now)

    assert report.skipped == (first,) and report.collected == ()
    assert count(url, "post_metrics") == 1
    [(stored_by,)] = sql(
        url,
        "SELECT r.worker_id FROM post_metrics m JOIN analytics_requests r ON r.id = m.request_id",
    )
    assert stored_by == "fast"


# --- D9. the verified creation time -----------------------------------------------------


def test_the_first_creation_time_reschedules_waiting_jobs_and_keeps_collected_ones(
    make_runtime, client_for, run_payload
):
    runtime, client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    url = runtime.db.libpq_url
    recorded = published_at(url, pid)
    created = recorded - timedelta(minutes=20)  # e.g. resolved later than it was posted
    provider = MockAnalyticsProvider(created_at={post_id(url, pid): created})
    w = analytics_worker(runtime.db, provider)
    before = {age: job_id(url, pid, age) for (age,) in AGES}

    report = w.run_once(now=recorded + HOUR)

    assert report.collected == (before["1h"],)
    assert set(report.rescheduled) == {before["24h"], before["72h"]}
    assert sql(url, "SELECT published_at, provider_created_at FROM publications") == [
        (recorded, created)
    ]
    rows = jobs(
        url, pid, "snapshot_age, status, schedule_basis, scheduled_for, original_scheduled_for"
    )
    assert rows == [
        ("1h", "collected", "recorded_published_at", recorded + HOUR, None),
        (
            "24h",
            "scheduled",
            "provider_created_at",
            created + timedelta(hours=24),
            recorded + timedelta(hours=24),
        ),
        (
            "72h",
            "scheduled",
            "provider_created_at",
            created + timedelta(hours=72),
            recorded + timedelta(hours=72),
        ),
    ]
    assert {age: job_id(url, pid, age) for (age,) in AGES} == before  # no job added
    # The 1h snapshot says it was really taken 80 minutes after the post was created.
    [snapshot] = client.get(f"/publications/{pid}/metrics").json()
    assert snapshot["age_basis"] == "provider_created_at"
    assert snapshot["actual_age_seconds"] == 80 * 60
    assert snapshot["capture_delay_seconds"] == 0 and snapshot["on_target"] is False
    assert client.get(f"/publications/{pid}").json()["provider_created_at"] is not None

    # Reconciling again (every later snapshot carries the same time) moves nothing.
    later = w.run_once(now=created + timedelta(hours=24))
    assert later.collected == (before["24h"],) and later.rescheduled == ()
    second = client.get(f"/publications/{pid}/metrics").json()[1]
    assert second["on_target"] is True and second["actual_age_seconds"] == 24 * 3600


def test_backfill_after_the_creation_time_is_known_schedules_from_it(
    make_runtime, client_for, run_payload
):
    runtime, _client, [pid] = published_runtime(
        make_runtime, client_for, run_payload, analytics_snapshot_ages="1h"
    )
    url = runtime.db.libpq_url
    created = published_at(url, pid) - timedelta(minutes=5)
    provider = MockAnalyticsProvider(created_at={post_id(url, pid): created})
    analytics_worker(runtime.db, provider).run_once(now=published_at(url, pid) + HOUR)

    # The operator later adds a 24h snapshot and backfills explicitly.
    service = AnalyticsService(
        runtime.db, schedule=AnalyticsSchedule(ages=parse_snapshot_ages("1h,24h"))
    )
    result = service.backfill(pid)

    assert len(result.created_job_ids) == 1 and len(result.existing_job_ids) == 1
    assert result.schedule_basis == "provider_created_at"
    assert jobs(url, pid, "snapshot_age, scheduled_for")[1] == (
        "24h",
        created + timedelta(hours=24),
    )


def test_a_creation_time_does_not_move_a_job_another_worker_is_collecting(
    make_runtime, client_for, run_payload
):
    runtime, _client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    url = runtime.db.libpq_url
    recorded = published_at(url, pid)
    execute(
        url,
        "UPDATE analytics_jobs SET status = 'collecting', claimed_by = 'other', "
        "lease_expires_at = :lease WHERE publication_id = :id AND snapshot_age = '24h'",
        id=pid,
        lease=recorded + timedelta(days=10),
    )
    with runtime.db.transaction() as session:
        moved = adb.reconcile_with_creation_time(
            session, pid, recorded - timedelta(minutes=3), now=recorded
        )
    assert set(moved) == {job_id(url, pid, "1h"), job_id(url, pid, "72h")}
    assert job(url, job_id(url, pid, "24h"), "scheduled_for, original_scheduled_for") == (
        recorded + timedelta(hours=24),
        None,
    )


# --- D8. the Phase 5 not_sent retry now backs off ---------------------------------------


def test_a_not_sent_publish_failure_waits_for_its_backoff(make_runtime, client_for, run_payload):
    not_sent = PublishNotSentError("refused", failure_category=PublishFailureCategory.NOT_SENT)
    publisher = MockPublisher([not_sent])
    runtime = make_runtime(publisher=publisher, publish_backoff_base_seconds=30)
    client = client_for(runtime)
    run_id, candidate_id = approved_run(client, run_payload)
    pid = intent(client, run_id, candidate_id)

    before = datetime.now(UTC)
    report = runtime.worker.run_once()
    after = datetime.now(UTC)
    assert report.requeued == (pid,)
    [(status, wait)] = sql(
        runtime.db.libpq_url,
        "SELECT status, retry_not_before FROM publications WHERE id = :id",
        id=pid,
    )
    assert status == "ready"
    assert before + timedelta(seconds=30) <= wait <= after + timedelta(seconds=30)
    assert runtime.worker.run_once().claimed == ()  # not due yet
    assert runtime.worker.run_once(now=wait).published == (pid,)
    assert len(publisher.calls) == 2


def test_a_publisher_worker_without_a_backoff_keeps_the_phase_5_behaviour(
    make_runtime, client_for, run_payload
):
    not_sent = PublishNotSentError("refused", failure_category=PublishFailureCategory.NOT_SENT)
    publisher = MockPublisher([not_sent])
    runtime = make_runtime(publisher=publisher)
    client = client_for(runtime)
    run_id, candidate_id = approved_run(client, run_payload)
    pid = intent(client, run_id, candidate_id)
    plain = worker(runtime.db, publisher)
    assert isinstance(plain, PublisherWorker)

    assert plain.run_once().requeued == (pid,)
    assert plain.run_once().published == (pid,)


# --- misc -------------------------------------------------------------------------------


def test_a_partial_answer_records_misses_and_rate_limit_headers(
    make_runtime, client_for, run_payload
):
    runtime, _client, ids = published_runtime(make_runtime, client_for, run_payload, count=2)
    url = runtime.db.libpq_url
    a, b = ids
    reset = datetime(2030, 1, 1, tzinfo=UTC)
    base = MockAnalyticsProvider()
    fetch = AnalyticsFetch(
        records=[base.metrics_for(post_id(url, a))],
        misses=[
            PostMetricMiss(
                provider_post_id=post_id(url, b),
                category=AnalyticsFailureCategory.UNKNOWN,
                detail="unexpected per-post error",
            )
        ],
        http_status=200,
        posts_returned=1,
        rate_limit_remaining=12,
        rate_limit_reset_at=reset,
    )
    now = max(published_at(url, p) for p in ids) + HOUR
    analytics_worker(runtime.db, MockAnalyticsProvider([fetch])).run_once(now=now)

    assert sql(
        url,
        "SELECT outcome, posts_returned, rate_limit_remaining, rate_limit_reset_at "
        "FROM analytics_requests",
    ) == [("partial", 1, 12, reset)]
    assert job(url, job_id(url, b, "1h"), "status, failure_category") == ("scheduled", "unknown")
