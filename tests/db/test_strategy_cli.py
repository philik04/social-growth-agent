"""``sga-strategy import-runs`` (Phase 7): explicit, dry-run first, conflicts never resolved."""

from social_growth_agent.services.strategy_cli import main
from tests.db.analytics_helpers import count
from tests.db.conftest import sql
from tests.db.strategy_helpers import V1, seed_comparable, strategy_runtime


def test_import_runs_reports_conflicts_and_never_resolves_them(make_runtime, clean_db, capsys):
    runtime = strategy_runtime(make_runtime)
    seed_comparable(runtime, per_hook=1)
    other = V1.model_copy(update={"id": "strat_other"})
    seed_comparable(runtime, per_hook=1, strategy=other)
    seed_comparable(runtime, per_hook=1, strategy=other.model_copy(update={"tone": "loud"}))

    assert main(["--url", clean_db, "import-runs", "--dry-run"]) == 1
    out = capsys.readouterr().out
    assert "would register: strat_p7@v1" in out
    assert "CONFLICT (not registered): strat_other@v1: 2 different bodies in runs" in out
    assert "dry run: nothing written" in out
    assert count(clean_db, "strategy_versions") == 0

    assert main(["--url", clean_db, "import-runs"]) == 1  # conflicts still reported
    out = capsys.readouterr().out
    assert "registered: strat_p7@v1" in out and "CONFLICT" in out
    assert sql(clean_db, "SELECT strategy_id, origin FROM strategy_versions") == [
        ("strat_p7", "imported_from_run")
    ]
    assert count(clean_db, "strategy_activations") == 0  # importing activates nothing

    assert main(["--url", clean_db, "import-runs"]) == 1
    out = capsys.readouterr().out
    assert "already registered: strat_p7@v1" in out
    assert count(clean_db, "strategy_versions") == 1


def test_import_runs_without_conflicts_exits_zero(make_runtime, clean_db, capsys):
    seed_comparable(strategy_runtime(make_runtime), per_hook=1)
    assert main(["--url", clean_db, "import-runs"]) == 0
    assert "1 registered, 0 already registered, 0 conflicts" in capsys.readouterr().out


def test_import_runs_needs_a_database_url(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir("/")  # no .env with a DATABASE_URL
    assert main(["import-runs", "--dry-run"]) == 2
    assert "DATABASE_URL is not set" in capsys.readouterr().err
