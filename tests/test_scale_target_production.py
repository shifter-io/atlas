"""PROD-AS-STAGING-1: production's database is a target only with the owner's dated production
go, the command line's acknowledgement and PROXY_INDEX_ALLOW_PRODUCTION_TARGET=1.

The test database stands in for production: `production` points target.PRODUCTION at its name
(and staging elsewhere); `staging` points target.STAGING at it to check staging is unchanged.
"""

import importlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import test_scale_cycle  # noqa: F401  (puts scripts/scale on sys.path)
from test_storage import measurement

from proxy_index.storage import connect, ingest

target = importlib.import_module("target")
replay = importlib.import_module("replay")
wipe_replay = importlib.import_module("wipe_replay")

pytest_plugins = ["test_storage"]

TODAY = datetime.now(UTC).date()
GO = f"owner-go PRODUCTION-AS-STAGING {TODAY}"
STAGING_GO = f"owner-go REPLAY-RUN-1 {TODAY}"
ACK = "--i-know-this-is-production"


def name_of(config):
    with connect(config) as conn:
        return conn.execute("SELECT current_database()").fetchone()[0]


@pytest.fixture
def production(database, monkeypatch):
    """The test database, named production for the guard; the environment allows it."""
    monkeypatch.setattr(target, "PRODUCTION", name_of(database))
    monkeypatch.setattr(target, "STAGING", "not-this-one")
    monkeypatch.setenv(target.ALLOW, "1")
    return database


@pytest.fixture
def staging(database, monkeypatch):
    monkeypatch.setattr(target, "STAGING", name_of(database))
    monkeypatch.setenv(target.ALLOW, "1")
    return database


def check(config, owner_go=GO, acknowledged=True):
    with connect(config) as conn:
        return target.refusal(conn, owner_go=owner_go, acknowledged=acknowledged)


def needs(found):
    """The refusal names all three requirements."""
    assert f"--owner-go {target.PRODUCTION_GO_FORM}" in found
    assert ACK in found and f"{target.ALLOW}=1" in found
    return found


def test_the_three_together_accept_production_with_a_banner_and_record(production, capsys):
    with connect(production) as conn:
        found, record = target.owner_go_refusal(conn, GO, True)
        role = conn.execute("SELECT current_user").fetchone()[0]
    assert found is None
    assert record["go"] == GO and record["database"] == target.PRODUCTION
    assert record["role"] == role and record["production"] is True
    assert datetime.fromisoformat(record["at"]).utcoffset() == timedelta(0)
    err = capsys.readouterr().err
    assert f"*** PRODUCTION DATABASE {target.PRODUCTION!r} AS STAGING" in err
    assert f"owner go accepted: {record}" in err
    yesterday = f"owner-go PRODUCTION-AS-STAGING {TODAY - timedelta(days=1)}"
    assert check(production, yesterday) is None
    argv = ["check", "--database", str(production), "--owner-go", GO, ACK]
    assert target.main(argv) == 0 and capsys.readouterr().out.endswith("allowed\n")


def test_without_the_acknowledgement_production_is_refused(production, capsys):
    assert needs(check(production, acknowledged=False)).endswith(f"{ACK} is missing")
    assert target.main(["check", "--database", str(production), "--owner-go", GO]) == 2
    assert "refused: " in capsys.readouterr().out


@pytest.mark.parametrize("value", [None, "0", "yes", ""])
def test_without_the_environment_production_is_refused(production, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(target.ALLOW)
    else:
        monkeypatch.setenv(target.ALLOW, value)
    assert needs(check(production)).endswith(f"{target.ALLOW} is not 1")


@pytest.mark.parametrize(
    ("go", "reason"),
    [
        (None, "is not 'owner-go PRODUCTION-AS-STAGING <YYYY-MM-DD>'"),
        (STAGING_GO, "is not"),
        ("owner-go PRODUCTION-AS-STAGING", "is not"),
        (f"{GO} extra", "is not"),
        ("owner-go PRODUCTION-AS-STAGING 2026-13-01", "is not"),
        (f"owner-go PRODUCTION-AS-STAGING {TODAY + timedelta(days=1)}", "after today"),
    ],
)
def test_a_missing_malformed_or_future_go_is_refused(production, go, reason):
    with connect(production) as conn:
        found, record = target.owner_go_refusal(conn, go, True)
    assert reason in needs(found) and record is None


async def test_a_load_into_production_needs_it_empty_of_others_runs(production, tmp_path):
    ingest(await measurement(tmp_path), production)
    assert "holds 1 test runs without the 'scale_replay' marker" in check(production)
    with connect(production) as conn:  # the cleaner and the wipe do not count runs
        assert target.owner_go_refusal(conn, GO, True, runs=False)[0] is None


def test_staging_and_the_scratch_targets_are_as_before(staging, databases):
    assert check(staging, STAGING_GO, acknowledged=False) is None
    assert check(staging, STAGING_GO) is None  # the acknowledgement changes nothing there
    assert "is not 'owner-go REPLAY-RUN-1 <YYYY-MM-DD>'" in check(staging, GO)
    assert check(staging, None).startswith("the database is")
    scratch = databases()
    with connect(scratch) as conn:
        name = conn.execute("SELECT current_database()").fetchone()[0]
        assert target.refusal(conn, expected=name) is None
        assert target.refusal(conn, owner_go=GO, acknowledged=True).startswith("the owner's go")


def test_the_generator_and_cycle_keep_refusing_production(production):
    """They never pass a go: production is refused as any database but the scratch one."""
    with connect(production) as conn:
        assert target.refusal(conn).startswith("the database is")


def guarded(**given):
    args = {"owner_go": GO, "first_day": str(TODAY - timedelta(days=3)), "days": 1}
    return SimpleNamespace(**args | {"hours_per_day": 1} | given)


def test_the_load_records_the_go_and_the_replay_keeps_refusing(production):
    with connect(production) as conn:
        go = replay.guard(conn, guarded(i_know_this_is_production=True))
        assert go["database"] == target.PRODUCTION and go["go"] == GO
        assert go["production"]["role"] and go["production"]["production"] is True
        with pytest.raises(replay.Refused, match="is missing"):
            replay.guard(conn, guarded())  # replay.py has no acknowledgement


def test_the_wipe_needs_the_three_in_production_even_for_a_dry_run(production, tmp_path, capsys):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"database": target.PRODUCTION}))
    argv = ["--database", str(production), "--state", str(state), "--owner-go", GO]
    assert wipe_replay.main(argv) == 2
    assert f"{ACK} is missing" in capsys.readouterr().err
    with connect(production) as conn:
        args = SimpleNamespace(owner_go=GO, i_know_this_is_production=True, apply=False)
        found, record = wipe_replay.refusal(conn, args, {"database": target.PRODUCTION})
    assert found is None and record["production"] is True
