"""The offline discovery demo end to end, the live demo's gate, and ``sga-discovery``."""

import json

import pytest

from social_growth_agent.services import discovery_cli, discovery_demo, discovery_live_demo
from tests.db.conftest import sql

TOKEN = "cli-test-review-token-0123"


def test_the_offline_discovery_demo_runs_end_to_end(clean_db, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", clean_db)
    assert discovery_demo.main() == 0
    out = capsys.readouterr().out

    assert "research via discovery_replay (0 requests, 0 reads)" in out
    assert "@grok-eligible topics: 1" in out
    assert "API publish refused: contains an @mention: manual publication only" in out
    assert "text exact, author verified" in out
    assert "already_registered=True, same record=True, verifier reads=1" in out
    assert "not stored: purged earlier (tombstone)" in out
    assert sql(clean_db, "SELECT provider, status FROM publications") == [("manual", "published")]
    assert sql(clean_db, "SELECT count(*) FROM manual_publications") == [(1,)]
    assert TOKEN not in out


def test_the_live_demo_refuses_without_its_gate(clean_db, monkeypatch, capsys):
    monkeypatch.delenv("RUN_LIVE_DISCOVERY_DEMO", raising=False)
    monkeypatch.setenv("DATABASE_URL", clean_db)
    assert discovery_live_demo.main([]) == 2
    assert "RUN_LIVE_DISCOVERY_DEMO" in capsys.readouterr().err
    assert sql(clean_db, "SELECT count(*) FROM discovery_sessions") == [(0,)]


@pytest.fixture
def cli_env(clean_db, monkeypatch) -> str:
    monkeypatch.setenv("DATABASE_URL", clean_db)
    monkeypatch.setenv("STRATEGY_REVIEW_TOKEN", TOKEN)
    monkeypatch.delenv("SGA_REVIEW_TOKEN", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    for name in ("RESEARCH_PROVIDER", "DISCOVERY_SOURCES", "PUBLISHER_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    return str(clean_db)


def test_the_cli_reads_without_a_token_and_mutates_only_with_it(cli_env, monkeypatch, capsys):
    assert discovery_cli.main(["sources"]) == 0
    assert "x-ai-engineering" in capsys.readouterr().out

    assert discovery_cli.main(["plan", "--account", "acct_cli"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["max_x_post_reads"] == 10
    assert sql(cli_env, "SELECT count(*) FROM discovery_sessions") == [(0,)]

    assert discovery_cli.main(["run", "--account", "acct_cli", "--by", "philipp"]) == 2
    assert "SGA_REVIEW_TOKEN" in capsys.readouterr().err
    monkeypatch.setenv("SGA_REVIEW_TOKEN", "wrong")
    assert discovery_cli.main(["run", "--account", "acct_cli", "--by", "philipp"]) == 2
    assert sql(cli_env, "SELECT count(*) FROM discovery_sessions") == [(0,)]

    monkeypatch.setenv("SGA_REVIEW_TOKEN", TOKEN)
    assert discovery_cli.main(["run", "--account", "acct_cli", "--by", "philipp"]) == 0
    out = capsys.readouterr().out
    session = json.loads(out)
    assert session["status"] == "succeeded" and session["auth_method"] == "review_token"
    assert TOKEN not in out

    assert discovery_cli.main(["show", session["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == session["id"]
    assert discovery_cli.main(["opportunities", session["id"]]) == 0
    assert "#1 " in capsys.readouterr().out

    item = "1900000000000000101"
    assert discovery_cli.main(["purge", item, "--by", "philipp"]) == 0
    assert json.loads(capsys.readouterr().out)["tombstones"] == 1
    assert discovery_cli.main(["purge-expired"]) == 0
    assert json.loads(capsys.readouterr().out)["provider_item_ids"] == []
    assert discovery_cli.main(["show", "disc_missing"]) == 1
    assert discovery_cli.main(["register", "run_x", "cand_x", "nope", "--by", "p"]) == 1
