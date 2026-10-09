"""Analytics through the API, usage and cost, lineage, secrets, and 0004 on a populated
Phase 5 database."""

import logging
from datetime import timedelta
from decimal import Decimal

from alembic import command
from pydantic import SecretStr

from social_growth_agent.errors import AnalyticsError
from social_growth_agent.models import AnalyticsFailureCategory
from social_growth_agent.persistence.migrate import alembic_config, current_revision, upgrade
from social_growth_agent.providers.mocks import MockAnalyticsProvider
from social_growth_agent.providers.x import XAnalyticsProvider
from social_growth_agent.providers.x.client import XApiClient
from tests.db.analytics_helpers import (
    HOUR,
    analytics_worker,
    count,
    job_id,
    jobs,
    post_id,
    publish_one,
    published_at,
    published_runtime,
)
from tests.db.conftest import (
    DB_PASSWORD,
    TEST_PRICES,
    create_scratch_database,
    drop_scratch_database,
    sql,
)
from tests.db.publishing_helpers import execute
from tests.db.test_secrets_and_schema import dump_everything
from tests.x_fakes import TOKEN, FakeX, error, full_metrics, ok


class BilledAnalytics(MockAnalyticsProvider):
    """The mock's numbers under the provider name ``x``, so the X read price applies."""

    provider_name = "x"


# --- 25-27. the API ---------------------------------------------------------------------


def test_metrics_are_ordered_by_target_age_and_summarized_on_the_publication(
    make_runtime, client_for, run_payload
):
    runtime, client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    url = runtime.db.libpq_url
    at = published_at(url, pid)
    w = analytics_worker(runtime.db)
    w.run_once(now=at + timedelta(hours=73))  # all three due in one late cycle
    snapshots = client.get(f"/publications/{pid}/metrics").json()

    assert [s["snapshot_age"] for s in snapshots] == ["1h", "24h", "72h"]
    # Taken together, late: each says its true age, and only 72h is on target.
    assert [s["on_target"] for s in snapshots] == [False, False, True]
    assert [s["capture_delay_seconds"] for s in snapshots] == [72 * 3600, 49 * 3600, 3600]
    assert {s["actual_age_seconds"] for s in snapshots} == {73 * 3600}

    publication = client.get(f"/publications/{pid}").json()
    summary = publication["analytics"]
    assert summary["jobs_by_status"] == {"collected": 3} and summary["snapshot_count"] == 3
    assert summary["latest"]["snapshot_age"] == "72h"
    run_id = publication["run_id"]
    [in_run] = client.get(f"/runs/{run_id}").json()["publications"]
    assert in_run["analytics"]["snapshot_count"] == 3


def test_a_publication_without_analytics_has_none(make_runtime, client_for, run_payload):
    _runtime, client, [pid] = published_runtime(
        make_runtime, client_for, run_payload, analytics_enqueue_on_publish=False
    )
    assert client.get(f"/publications/{pid}").json()["analytics"] is None
    assert client.get(f"/publications/{pid}/metrics").json() == []
    assert client.get("/publications/pub_missing/metrics").status_code == 404


def test_backfill_is_explicit_idempotent_and_can_be_dry_run(make_runtime, client_for, run_payload):
    runtime, client, [pid] = published_runtime(
        make_runtime, client_for, run_payload, analytics_enqueue_on_publish=False
    )
    url = runtime.db.libpq_url

    dry = client.post(f"/publications/{pid}/analytics/backfill", params={"dry_run": True})
    assert dry.status_code == 202
    assert dry.json()["dry_run"] is True and dry.json()["created_job_ids"] == ["1h", "24h", "72h"]
    assert count(url, "analytics_jobs") == 0

    first = client.post(f"/publications/{pid}/analytics/backfill").json()
    assert len(first["created_job_ids"]) == 3 and first["existing_job_ids"] == []
    assert first["schedule_basis"] == "recorded_published_at"
    assert {o for (o,) in jobs(url, pid, "origin")} == {"backfill"}
    again = client.post(f"/publications/{pid}/analytics/backfill").json()
    assert again["created_job_ids"] == [] and sorted(again["existing_job_ids"]) == sorted(
        first["created_job_ids"]
    )
    assert count(url, "analytics_jobs") == 3
    assert client.post("/publications/pub_missing/analytics/backfill").status_code == 404


def test_jobs_can_be_listed_filtered_inspected_and_retried(make_runtime, client_for, run_payload):
    runtime, client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    url = runtime.db.libpq_url
    provider = MockAnalyticsProvider(
        [AnalyticsError("HTTP 401", failure_category=AnalyticsFailureCategory.AUTH)]
    )
    analytics_worker(runtime.db, provider).run_once(now=published_at(url, pid) + HOUR)
    first = job_id(url, pid, "1h")

    listed = client.get("/analytics/jobs").json()
    assert [j["snapshot_age"] for j in listed] == ["1h", "24h", "72h"]
    [failed] = client.get("/analytics/jobs", params={"status": "failed"}).json()
    assert failed["id"] == first and failed["failure_category"] == "auth"
    assert len(client.get("/analytics/jobs", params={"publication_id": pid}).json()) == 3
    assert client.get("/analytics/jobs", params={"publication_id": "nope"}).json() == []

    detail = client.get(f"/analytics/jobs/{first}").json()
    assert detail["attempts"][0]["outcome"] == "failed"
    assert detail["schedule_basis"] == "recorded_published_at"
    assert client.get("/analytics/jobs/ajob_missing").status_code == 404
    assert client.post("/analytics/jobs/ajob_missing/retry").status_code == 404
    assert client.post(f"/analytics/jobs/{job_id(url, pid, '24h')}/retry").status_code == 409
    assert client.post(f"/analytics/jobs/{first}/retry").json()["status"] == "scheduled"
    assert client.get("/analytics/jobs", params={"status": "bogus"}).status_code == 422


def test_posts_lists_published_posts_with_their_latest_snapshot(
    make_runtime, client_for, run_payload, account
):
    runtime, client, ids = published_runtime(make_runtime, client_for, run_payload, count=2)
    url = runtime.db.libpq_url
    older, newer = ids
    analytics_worker(runtime.db).run_once(now=published_at(url, older) + HOUR)
    execute(
        url,
        "UPDATE publications SET published_at = published_at - interval '2 days' WHERE id = :id",
        id=older,
    )

    posts = client.get("/posts").json()
    assert [p["publication_id"] for p in posts] == [newer, older]
    by_id = {p["publication_id"]: p for p in posts}
    assert by_id[older]["analytics"]["latest"]["snapshot_age"] == "1h"
    assert by_id[newer]["analytics"]["latest"] is None
    assert by_id[older]["account_id"] == account.id
    assert by_id[older]["hook_type"] and by_id[older]["strategy_id"]

    since = (published_at(url, newer) - timedelta(days=1)).isoformat()
    assert [p["publication_id"] for p in client.get("/posts", params={"since": since}).json()] == [
        newer
    ]
    assert [p["publication_id"] for p in client.get("/posts", params={"until": since}).json()] == [
        older
    ]
    assert [
        p["publication_id"] for p in client.get("/posts", params={"status": "collected"}).json()
    ] == [older]
    assert client.get("/posts", params={"account_id": "someone-else"}).json() == []
    assert len(client.get("/posts", params={"account_id": account.id}).json()) == 2
    assert len(client.get("/posts", params={"limit": 1}).json()) == 1
    naive = client.get("/posts", params={"since": "2026-10-01T00:00:00"})
    assert naive.status_code == 422 and "timezone" in naive.text


def test_lineage_leads_from_a_publication_back_to_research_and_strategy(
    make_runtime, client_for, run_payload, strategy
):
    runtime, client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    url = runtime.db.libpq_url
    analytics_worker(runtime.db).run_once(now=published_at(url, pid) + HOUR)
    lineage = client.get(f"/publications/{pid}/lineage").json()

    publication = lineage["publication"]
    candidate = lineage["candidate"]
    assert candidate["id"] == publication["candidate_id"]
    assert lineage["revision_chain"][0] == candidate["id"]
    assert candidate["critiques"]  # the critique of record travels with the candidate
    assert lineage["approval"]["action"] == "approve"
    assert lineage["approval"]["candidate_id"] == candidate["id"]
    cited = set(candidate["research_finding_ids"])
    assert cited and {f["id"] for f in lineage["findings"]} == cited
    evidence = {s for f in lineage["findings"] for s in f["evidence_source_ids"]}
    assert {p["source_id"] for p in lineage["source_posts"]} == evidence
    assert lineage["strategy_id"] == strategy.id
    assert lineage["strategy"]["id"] == strategy.id
    assert [m["snapshot_age"] for m in lineage["metrics"]] == ["1h"]
    assert client.get("/publications/pub_missing/lineage").status_code == 404


# --- 24. usage and cost -----------------------------------------------------------------


def test_usage_and_cost_include_analytics_reads(make_runtime, client_for, run_payload):
    runtime, client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    url = runtime.db.libpq_url
    at = published_at(url, pid)
    provider = BilledAnalytics(
        [AnalyticsError("HTTP 503", failure_category=AnalyticsFailureCategory.TRANSIENT_SERVER)]
    )
    w = analytics_worker(runtime.db, provider)
    w.run_once(now=at + HOUR)  # failed request: no post read
    w.run_once(now=at + HOUR + timedelta(minutes=1))  # 1h
    w.run_once(now=at + timedelta(hours=24))  # 24h
    run_id = client.get(f"/publications/{pid}").json()["run_id"]

    report = client.get(f"/runs/{run_id}/usage").json()
    [usage] = report["usage"]["analytics"]
    assert usage == {
        "provider": "x",
        "requests": 3,
        "post_reads": 2,
        "snapshots": 2,
        "failed_requests": 1,
        "unfinished_requests": 0,
    }
    for basis in ("at_run_pricing", "at_current_pricing"):
        [line] = [li for li in report[basis]["lines"] if li["item"] == "analytics_post_reads"]
        assert Decimal(line["unit_price"]) == TEST_PRICES.x.post_read
        assert Decimal(line["cost"]) == 2 * TEST_PRICES.x.post_read


def test_mock_analytics_reads_are_counted_but_never_priced_as_x(
    make_runtime, client_for, run_payload
):
    runtime, client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    analytics_worker(runtime.db).run_once(now=published_at(runtime.db.libpq_url, pid) + HOUR)
    run_id = client.get(f"/publications/{pid}").json()["run_id"]

    report = client.get(f"/runs/{run_id}/usage").json()
    [usage] = report["usage"]["analytics"]
    assert (usage["provider"], usage["post_reads"]) == ("mock_analytics", 1)
    [line] = [
        li for li in report["at_run_pricing"]["lines"] if li["item"] == "analytics_post_reads"
    ]
    assert line["unit_price"] is None  # no price is invented for an unknown provider
    assert "mock_analytics:analytics_post_reads" in report["at_run_pricing"]["missing_prices"]


# --- 28. secrets ------------------------------------------------------------------------


def test_the_bearer_token_and_db_password_never_reach_analytics_rows_output_or_logs(
    db_url_with_secret_password, make_runtime, client_for, run_payload, clean_db, caplog
):
    caplog.set_level(logging.DEBUG)
    runtime = make_runtime(url=db_url_with_secret_password)
    client = client_for(runtime)
    pid = publish_one(runtime, client, run_payload)
    url = runtime.db.libpq_url
    at = published_at(clean_db, pid)
    the_post = post_id(clean_db, pid)
    fake = FakeX(
        error(401, {"title": "Unauthorized", "detail": "Unauthorized"}),
        ok(
            {
                "data": [
                    {
                        "id": the_post,
                        "created_at": "2026-10-04T09:30:00.000Z",
                        "public_metrics": full_metrics(),
                    }
                ]
            }
        ),
    )
    provider = XAnalyticsProvider(
        XApiClient(SecretStr(TOKEN), timeout_seconds=1.0, transport=fake.transport())
    )
    w = analytics_worker(runtime.db, provider)
    w.run_once(now=at + HOUR)
    first = job_id(clean_db, pid, "1h")
    assert client.post(f"/analytics/jobs/{first}/retry").status_code == 202
    w.run_once(now=at + HOUR + timedelta(minutes=5))
    assert fake.requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert count(clean_db, "post_metrics") == 1

    outputs = [
        client.get(path).text
        for path in (
            f"/publications/{pid}",
            f"/publications/{pid}/metrics",
            f"/publications/{pid}/lineage",
            "/posts",
            "/analytics/jobs",
            f"/analytics/jobs/{first}",
        )
    ]
    haystacks = [dump_everything(clean_db), *outputs, caplog.text, repr(provider), repr(w)]
    for secret in (TOKEN, DB_PASSWORD):
        for haystack in haystacks:
            assert secret not in haystack
    assert url  # the runtime really ran through the password-protected role
    provider.close()


# --- 29. migrating a populated Phase 5 database -----------------------------------------


def test_0004_applies_to_a_populated_phase_5_database_without_scheduling_anything(
    base_database_url,
):
    url = create_scratch_database(base_database_url, prefix="sga_phase5")
    try:
        command.upgrade(alembic_config(url), "0003")
        assert current_revision(url) == "0003"
        sql(
            url,
            "INSERT INTO pricing_versions (id, version, as_of, currency, content) "
            "VALUES (:id, 'test-1', '2026-10-01', 'USD', '{}') RETURNING id",
            id=TEST_PRICES.id,
        )
        sql(
            url,
            "INSERT INTO runs (id, status, research_query, account, strategy, config, "
            "strategy_id, strategy_version, research_attempts, generation_attempts, "
            "regeneration_rounds, edit_rounds, review_candidate_ids, pricing_id) VALUES "
            "('run_p5', 'approved', 'q', '{}', '{}', '{}', 's', 1, 1, 1, 0, 0, '[]', :p) "
            "RETURNING id",
            p=TEST_PRICES.id,
        )
        sql(
            url,
            "INSERT INTO content_candidates (id, run_id, generation_attempt, origin, content, "
            "topic, hook_type, format, target_audience, strategy_id, strategy_version, "
            "created_at, position) VALUES ('cand_p5', 'run_p5', 1, 'generated', "
            "'a phase 5 post', 't', 'number', 'single', 'a', 's', 1, now(), 0) RETURNING id",
        )
        sql(
            url,
            "INSERT INTO publications (id, run_id, candidate_id, platform, idempotency_key, "
            "content, content_sha256, status, attempt_count, provider, provider_post_id, "
            "published_at) VALUES ('pub_p5', 'run_p5', 'cand_p5', 'x', 'idem_p5', "
            "'a phase 5 post', 'sha', 'published', 1, 'x', '1908000000000000005', "
            "'2026-10-05T10:00:00Z') RETURNING id",
        )

        upgrade(url)  # 0003 -> 0004

        assert current_revision(url) == "0004"
        assert sql(
            url, "SELECT status, provider_post_id, provider_created_at FROM publications"
        ) == [("published", "1908000000000000005", None)]
        # The migration schedules nothing; backfilling is an explicit, separate step.
        for table in ("analytics_jobs", "analytics_requests", "analytics_attempts", "post_metrics"):
            assert count(url, table) == 0
    finally:
        drop_scratch_database(base_database_url, url)
