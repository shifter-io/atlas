"""SCALE-TARGET-2/3: the owner's go lets the replay (REPLAY-RUN-1) write to the staging database.

The scratch database stands in for staging: `staging` points target.STAGING at its name, and
`production` points target.PRODUCTION at it to check that production stays refused.
"""

import importlib
from datetime import UTC, datetime, timedelta

import pytest
import test_scale_cycle  # noqa: F401  (puts scripts/scale on sys.path)
from test_storage import measurement

from atlas.storage import connect, ingest

target = importlib.import_module("target")

pytest_plugins = ["test_storage"]

TODAY = datetime.now(UTC).date()
GO = f"owner-go REPLAY-RUN-1 {TODAY}"


def name_of(config):
    with connect(config) as conn:
        return conn.execute("SELECT current_database()").fetchone()[0]


@pytest.fixture
def staging(database, monkeypatch):
    """The test database, named staging for the guard."""
    monkeypatch.setattr(target, "STAGING", name_of(database))
    return database


@pytest.fixture
def production(database, monkeypatch):
    """The test database, named production for the guard."""
    monkeypatch.setattr(target, "PRODUCTION", name_of(database))
    return database


def check(config, owner_go=None):
    with connect(config) as conn:
        return target.refusal(conn, owner_go=owner_go)


def test_without_the_go_staging_and_production_are_refused_as_before(staging, capsys):
    assert check(staging).startswith("the database is")
    assert target.main(["check", "--database", str(staging)]) == 2
    assert "write only to 'proxy_scale'" in capsys.readouterr().out


def test_production_is_refused_with_a_valid_go(production, capsys):
    assert check(production).startswith("the database is")
    found = check(production, GO)
    assert "is production's: a target only with --owner-go 'owner-go PRODUCTION-AS" in found
    assert target.main(["check", "--database", str(production), "--owner-go", GO]) == 2
    assert "refused: " in capsys.readouterr().out


def test_production_is_refused_even_without_test_runs(production, monkeypatch):
    with connect(production) as conn:
        assert conn.execute(target.FOREIGN_RUNS).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM test_runs").fetchone()[0] == 0
    # Even were staging's name production's, the production check wins.
    monkeypatch.setattr(target, "STAGING", target.PRODUCTION)
    assert "is production's" in check(production, GO)


def test_staging_and_production_are_distinct():
    assert target.STAGING == "proxy_reviews_staging" and target.PRODUCTION == "proxy_reviews"


@pytest.mark.parametrize(
    ("go", "reason"),
    [
        ("owner-go REPLAY-RUN-1", "is not 'owner-go REPLAY-RUN-1 <YYYY-MM-DD>'"),
        ("owner-go REPLAY-RUN-2 2026-11-07", "is not"),
        (f"owner-go REPLAY-RUN-1 {TODAY} extra", "is not"),
        ("owner-go REPLAY-RUN-1 2026-13-01", "is not"),
        (f"owner-go REPLAY-RUN-1 {TODAY + timedelta(days=1)}", "after today"),
    ],
)
def test_a_malformed_or_future_go_is_refused(staging, go, reason):
    assert reason in check(staging, go)


def test_another_database_is_refused(database):
    assert "the owner's go is for 'proxy_reviews_staging', not the database" in check(database, GO)


async def test_a_run_without_the_replay_marker_is_refused(staging, tmp_path, capsys):
    ingest(await measurement(tmp_path), staging)
    assert "holds 1 test runs without the 'scale_replay' marker" in check(staging, GO)
    assert target.main(["check", "--database", str(staging), "--owner-go", GO]) == 2
    assert "refused: " in capsys.readouterr().out
    with connect(staging) as conn:
        conn.execute("""UPDATE test_runs SET summary=summary||'{"scale_replay":true}'""")
    assert check(staging, GO) is None


def test_the_go_is_allowed_logged_and_returned(staging, capsys):
    yesterday = f"owner-go REPLAY-RUN-1 {TODAY - timedelta(days=1)}"  # a resumed run
    assert check(staging, yesterday) is None
    with connect(staging) as conn:
        found, record = target.owner_go_refusal(conn, GO)
        role = conn.execute("SELECT current_user").fetchone()[0]
    assert found is None
    assert record["go"] == GO and record["database"] == target.STAGING
    assert record["role"] == role
    assert datetime.fromisoformat(record["at"]).tzinfo is not None
    assert f"owner go accepted: {record}" in capsys.readouterr().err
    assert target.main(["check", "--database", str(staging), "--owner-go", GO]) == 0
    assert capsys.readouterr().out.endswith("allowed\n")


def test_the_go_does_not_open_the_scratch_targets(databases):
    scratch = databases()
    with connect(scratch) as conn:
        name = conn.execute("SELECT current_database()").fetchone()[0]
        assert target.refusal(conn, expected=name) is None
        assert target.refusal(conn, owner_go=GO).startswith("the owner's go is for")
