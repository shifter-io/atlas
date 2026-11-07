"""SCALE-TARGET-2: the owner's go lets the replay (REPLAY-RUN-1) write to production's database.

The scratch database stands in for production: `production` points target.PRODUCTION at its name.
"""

import importlib
from datetime import UTC, datetime, timedelta

import pytest
import test_scale_cycle  # noqa: F401  (puts scripts/scale on sys.path)
from test_storage import measurement

from proxy_index.storage import connect, ingest

target = importlib.import_module("target")

pytest_plugins = ["test_storage"]

TODAY = datetime.now(UTC).date()
GO = f"owner-go REPLAY-RUN-1 {TODAY}"


@pytest.fixture
def production(database, monkeypatch):
    """The test database, named production for the guard."""
    with connect(database) as conn:
        name = conn.execute("SELECT current_database()").fetchone()[0]
    monkeypatch.setattr(target, "PRODUCTION", name)
    return database


def check(config, owner_go=None):
    with connect(config) as conn:
        return target.refusal(conn, owner_go=owner_go)


def test_without_the_go_production_is_refused_as_before(production, capsys):
    assert check(production).startswith("the database is")
    assert target.main(["check", "--database", str(production)]) == 2
    assert "write only to 'proxy_scale'" in capsys.readouterr().out


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
def test_a_malformed_or_future_go_is_refused(production, go, reason):
    assert reason in check(production, go)


def test_another_database_is_refused(database):
    assert "the owner's go is for 'proxy_reviews', not the database" in check(database, GO)


async def test_a_run_without_the_replay_marker_is_refused(production, tmp_path, capsys):
    ingest(await measurement(tmp_path), production)
    assert "holds 1 test runs without the 'scale_replay' marker" in check(production, GO)
    assert target.main(["check", "--database", str(production), "--owner-go", GO]) == 2
    assert "refused: " in capsys.readouterr().out
    with connect(production) as conn:
        conn.execute("""UPDATE test_runs SET summary=summary||'{"scale_replay":true}'""")
    assert check(production, GO) is None


def test_the_go_is_allowed_logged_and_returned(production, capsys):
    yesterday = f"owner-go REPLAY-RUN-1 {TODAY - timedelta(days=1)}"  # a resumed run
    assert check(production, yesterday) is None
    with connect(production) as conn:
        found, record = target.owner_go_refusal(conn, GO)
        role = conn.execute("SELECT current_user").fetchone()[0]
    assert found is None
    assert record["go"] == GO and record["database"] == target.PRODUCTION
    assert record["role"] == role
    assert datetime.fromisoformat(record["at"]).tzinfo is not None
    assert f"owner go accepted: {record}" in capsys.readouterr().err
    assert target.main(["check", "--database", str(production), "--owner-go", GO]) == 0
    assert capsys.readouterr().out.endswith("allowed\n")


def test_the_go_does_not_open_the_scratch_targets(databases):
    scratch = databases()
    with connect(scratch) as conn:
        name = conn.execute("SELECT current_database()").fetchone()[0]
        assert target.refusal(conn, expected=name) is None
        assert target.refusal(conn, owner_go=GO).startswith("the owner's go is for")
