"""The offline analytics demo end to end, and the live demo's gates (offline)."""

from pydantic import SecretStr

from social_growth_agent.providers.x import XAnalyticsProvider
from social_growth_agent.providers.x.client import XApiClient
from social_growth_agent.services import analytics_demo, x_analytics_demo
from tests.db.analytics_helpers import count, jobs, post_id, published_runtime
from tests.x_fakes import TOKEN, FakeX, full_metrics, ok


def test_the_offline_analytics_demo_runs_end_to_end(clean_db, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", clean_db)
    assert analytics_demo.main() == 0
    out = capsys.readouterr().out

    assert "startup cycle: requests=0" in out
    assert "retry" in out and "not before T+1h10m05s" in out
    assert "cancelled=2 rescheduled=2" in out
    assert "worker died" in out and "swept=1" in out
    assert count(clean_db, "post_metrics") == 3
    assert count(clean_db, "analytics_requests", "finished_at IS NULL") == 1


def live_env(monkeypatch, url):
    monkeypatch.setenv("RUN_LIVE_X_ANALYTICS_TESTS", "1")
    monkeypatch.setenv("X_BEARER_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", url)


def fake_x(monkeypatch, fake: FakeX) -> None:
    def from_token(cls, token, *, timeout_seconds, transport=None):
        assert token.get_secret_value() == TOKEN
        return cls(XApiClient(SecretStr(TOKEN), timeout_seconds=1.0, transport=fake.transport()))

    monkeypatch.setattr(XAnalyticsProvider, "from_token", classmethod(from_token))


def test_the_live_demo_refuses_without_its_gate(clean_db, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", clean_db)
    assert x_analytics_demo.main(["1908000000000000001"]) == 2
    assert "RUN_LIVE_X_ANALYTICS_TESTS" in capsys.readouterr().err


def test_the_live_demo_refuses_posts_it_did_not_publish(clean_db, monkeypatch, capsys):
    live_env(monkeypatch, clean_db)
    fake = FakeX()
    fake_x(monkeypatch, fake)

    assert x_analytics_demo.main(["1908000000000000001"]) == 2
    assert "refusing to read it" in capsys.readouterr().out
    assert fake.requests == [] and count(clean_db, "analytics_requests") == 0
    assert x_analytics_demo.main(["not-an-id"]) == 2


def test_the_live_demo_probe_is_recorded_and_stores_nothing(
    make_runtime, client_for, run_payload, clean_db, monkeypatch, capsys
):
    _runtime, _client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    the_post = post_id(clean_db, pid)
    before = jobs(clean_db, pid, "id, status, scheduled_for")
    live_env(monkeypatch, clean_db)
    metrics = full_metrics(impressions=None)
    fake = FakeX(
        ok(
            {
                "data": [
                    {
                        "id": the_post,
                        "created_at": "2026-10-04T09:30:00.000Z",
                        "public_metrics": metrics,
                    }
                ]
            }
        )
    )
    fake_x(monkeypatch, fake)

    assert x_analytics_demo.main([the_post]) == 0
    out = capsys.readouterr().out

    assert len(fake.requests) == 1 and fake.requests[0].method == "GET"
    assert "impressions  not returned (NULL)" in out
    assert "estimated cost: 1 post read(s)" in out
    assert count(clean_db, "analytics_requests", "outcome = 'succeeded'") == 1
    assert count(clean_db, "post_metrics") == 0
    assert jobs(clean_db, pid, "id, status, scheduled_for") == before


def test_the_live_demo_collects_only_due_jobs_of_that_publication(
    make_runtime, client_for, run_payload, clean_db, monkeypatch, capsys
):
    _runtime, _client, [pid] = published_runtime(make_runtime, client_for, run_payload)
    live_env(monkeypatch, clean_db)
    fake = FakeX()
    fake_x(monkeypatch, fake)

    assert x_analytics_demo.main([post_id(clean_db, pid), "--collect-due"]) == 0
    assert "nothing was read" in capsys.readouterr().out
    assert fake.requests == [] and count(clean_db, "analytics_requests") == 0


def test_the_worker_cli_dry_run_and_backfill_read_nothing(
    make_runtime, client_for, run_payload, clean_db, monkeypatch, capsys
):
    from social_growth_agent.services import analytics_cli

    _runtime, _client, [pid] = published_runtime(
        make_runtime, client_for, run_payload, analytics_enqueue_on_publish=False
    )
    monkeypatch.setenv("DATABASE_URL", clean_db)

    assert analytics_cli.main(["--dry-run"]) == 0
    assert "0 analytics job(s) due; nothing was read" in capsys.readouterr().out
    assert analytics_cli.main(["backfill", "--all-published", "--dry-run"]) == 0
    assert "would create 3" in capsys.readouterr().out
    assert count(clean_db, "analytics_jobs") == 0
    assert analytics_cli.main(["backfill", "--publication", pid]) == 0
    assert f"{pid}: created 3, already had 0" in capsys.readouterr().out
    assert analytics_cli.main(["backfill", "--publication", pid]) == 0
    assert "created 0, already had 3" in capsys.readouterr().out
    assert analytics_cli.main(["backfill", "--publication", "pub_missing"]) == 1
    # One cycle of the configured (mock) worker: nothing is due yet, nothing is read.
    assert analytics_cli.main(["--once"]) == 0
    assert "claimed 0" in capsys.readouterr().out
    assert count(clean_db, "analytics_requests") == 0
