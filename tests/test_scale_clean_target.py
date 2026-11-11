"""PROD-AS-STAGING-1: clean_target.py empties the measurement and calculated data of the test
database (standing in for production, then staging) and keeps its seed, catalog and schema
versions row for row."""

import importlib
import json
import stat
from datetime import UTC, datetime, timedelta

import pytest
import test_scale_cycle  # noqa: F401  (puts scripts/scale on sys.path)
from orchestrator_cases import prepare, query
from test_storage import measurement

from proxy_index.calculated import store
from proxy_index.partitions import DAILY, children
from proxy_index.storage import connect, ingest

target = importlib.import_module("target")
clean_target = importlib.import_module("clean_target")
replay_baseline = importlib.import_module("replay_baseline")

pytest_plugins = ["test_storage"]

TODAY = datetime.now(UTC).date()
GO = f"owner-go PRODUCTION-AS-STAGING {TODAY}"
ACK = "--i-know-this-is-production"
SITE = "site.providers"
MEASURED = ("test_runs", "ip_observations", "run_buckets", "ip_addresses", "job_runs", "alerts")
MEASURED += ("calculation_builds", "calculated_snapshots", "nodes")


def name_of(config):
    return query(config, "SELECT current_database()")[0][0]


async def filled(database, tmp_path):
    """Seed, the catalog, a measurement, an old job run, an alert and a calculated build."""
    prepare(database, ("US", "DE"))
    store.initialize(database)
    ingest(await measurement(tmp_path), database)
    with connect(database) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS site")
        conn.execute(f"CREATE TABLE {SITE}(slug text PRIMARY KEY, name text NOT NULL)")
        conn.execute(f"INSERT INTO {SITE} VALUES ('fixture','Fixture'),('other','Other')")
        conn.execute("INSERT INTO settings VALUES ('kept','{\"a\":1}')")
        conn.execute("INSERT INTO methodologies VALUES ('baseline','{}') ON CONFLICT DO NOTHING")
        old = "now()-interval '1 hour'"
        conn.execute(f"INSERT INTO job_runs(job,started_at,status) VALUES ('daily',{old},'ok')")
        conn.execute("INSERT INTO alerts(kind,message) VALUES ('fixture','an alert')")
        conn.execute(
            "INSERT INTO nodes(region_id,provider,product,token_hash) "
            "SELECT region_id,'fixture','residential','t' FROM runner_regions LIMIT 1"
        )
        build = conn.execute(
            "INSERT INTO calculation_builds(methodology_id,as_of,input_hash,published) "
            "VALUES ('baseline',now(),'h',true) RETURNING build_id"
        ).fetchone()[0]
        conn.execute("INSERT INTO calculated_snapshots VALUES (%s,'baseline','{}')", (build,))
    return database


@pytest.fixture
async def production(database, tmp_path, monkeypatch):
    monkeypatch.setattr(target, "PRODUCTION", name_of(database))
    monkeypatch.setattr(target, "STAGING", "not-this-one")
    monkeypatch.setenv(target.ALLOW, "1")
    return await filled(database, tmp_path / "measured")


def execute(database, statement):
    with connect(database) as conn:
        conn.execute(statement)


def kept_rows(database) -> dict:
    """Every row of the kept tables and the catalog."""
    names = [*clean_target.KEEP, SITE]
    return {name: sorted(map(repr, query(database, f"SELECT * FROM {name}"))) for name in names}


def counts(database) -> dict:
    with connect(database) as conn:
        return replay_baseline.counts(conn)


def partitions(database) -> list:
    with connect(database) as conn:
        return [name for table in DAILY for name, *_ in children(conn, table)]


def clean(database, out, capsys, *extra, go=GO):
    argv = ["--database", str(database), "--out", str(out), "--owner-go", go, *extra]
    code = clean_target.main(argv)
    found = capsys.readouterr()
    return code, json.loads(found.out) if code == 0 else found.err


def test_the_dry_run_counts_and_apply_deletes_exactly_those(production, tmp_path, capsys):
    before, rows, out = counts(production), kept_rows(production), tmp_path / "out"
    assert all(before[f"public.{name}"] for name in MEASURED) and partitions(production)
    code, dry = clean(production, out, capsys, ACK)
    assert code == 0 and dry["dry_run"] is True
    assert dry["go"]["production"] is True and dry["database"] == target.PRODUCTION
    doomed = {name for name in before if name.startswith("public.")}
    doomed -= {f"public.{name}" for name in clean_target.KEEP}
    assert dry["deleted"] == {name: before[name] for name in sorted(doomed)}
    assert set(dry["kept"]) == set(before) - doomed and dry["kept"][SITE] == 2
    assert dry["keep"] == [*clean_target.KEEP, "site.*"] and dry["partitions"]
    path = out / clean_target.DRY_RUN
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["deleted"] == dry["deleted"]
    assert counts(production) == before and partitions(production)  # rolled back
    code, done = clean(production, out, capsys, ACK, "--apply")
    assert code == 0 and done["dry_run"] is False and done["deleted"] == dry["deleted"]
    after = counts(production)
    assert all(after[name] == 0 for name in doomed) and kept_rows(production) == rows
    assert not partitions(production)
    job = "INSERT INTO job_runs(job,status) VALUES ('daily','ok') RETURNING id"
    assert query(production, job)[0][0] == 1  # the identities restart


def test_a_count_that_differs_from_the_dry_run_rolls_back(production, tmp_path, capsys):
    out = tmp_path / "out"
    assert clean(production, out, capsys, ACK)[0] == 0
    execute(production, "INSERT INTO alerts(kind,message) VALUES ('later','one more')")
    before = counts(production)
    code, err = clean(production, out, capsys, ACK, "--apply")
    assert code == 2 and "rolled back: the counts differ from the dry run's" in err
    assert counts(production) == before and partitions(production)


def test_a_live_job_run_refuses(production, tmp_path, capsys):
    execute(production, "INSERT INTO job_runs(job) VALUES ('hourly')")
    before, out = counts(production), tmp_path / "out"
    code, err = clean(production, out, capsys, ACK)
    assert code == 2 and "a job_runs row started in the last 5 minutes" in err
    assert counts(production) == before and not (out / clean_target.DRY_RUN).exists()


def test_apply_needs_a_fresh_dry_run_of_the_same_database(production, tmp_path, capsys):
    out, before = tmp_path / "out", counts(production)
    code, err = clean(production, out, capsys, ACK, "--apply")
    assert code == 2 and "no dry run (clean-dry-run.json)" in err
    assert clean(production, out, capsys, ACK)[0] == 0
    path = out / clean_target.DRY_RUN
    found = json.loads(path.read_text())
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    path.write_text(json.dumps(found | {"at": stale}))
    assert "more than an hour ago" in clean(production, out, capsys, ACK, "--apply")[1]
    path.write_text(json.dumps(found | {"database": "another"}))
    assert "the dry run is of 'another'" in clean(production, out, capsys, ACK, "--apply")[1]
    assert counts(production) == before


@pytest.mark.parametrize("missing", ["acknowledgement", "environment", "go"])
def test_production_needs_the_three(production, tmp_path, capsys, monkeypatch, missing):
    extra, go = [ACK], GO
    if missing == "acknowledgement":
        extra = []
    elif missing == "environment":
        monkeypatch.delenv(target.ALLOW)
    else:
        go = f"owner-go CLEAN-STAGING {TODAY}"
    before = counts(production)
    code, err = clean(production, tmp_path / "out", capsys, *extra, go=go)
    assert code == 2 and "is production's: a target only with --owner-go" in err
    assert counts(production) == before


async def test_staging_takes_its_own_go_and_another_database_none(
    database, tmp_path, capsys, monkeypatch
):
    staging = await filled(database, tmp_path / "measured")
    out = tmp_path / "out"
    code, err = clean(staging, out, capsys, ACK, go=f"owner-go CLEAN-STAGING {TODAY}")
    assert code == 2 and f"cleans only {target.STAGING!r} or" in err
    monkeypatch.setattr(target, "STAGING", name_of(staging))
    code, err = clean(staging, out, capsys, ACK)
    assert code == 2 and "is not 'owner-go CLEAN-STAGING <YYYY-MM-DD>'" in err
    code, dry = clean(staging, out, capsys, go=f"owner-go CLEAN-STAGING {TODAY}")
    assert code == 0 and dry["deleted"]["public.test_runs"] == 1 and "production" not in dry["go"]
