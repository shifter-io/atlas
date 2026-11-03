"""ARCHIVE-1: one re-importable file per UTC day (export, restore, CLI) and daily partitions."""

import io
import json
import tarfile
from datetime import UTC, date, datetime, timedelta

import pytest
from test_storage import enrichment, measurement
from test_storage_v6 import census

from proxy_index.archive import ArchiveError
from proxy_index.archive.export import MANIFEST, export_day, file_digests
from proxy_index.archive.restore import restore_file
from proxy_index.archive.tables import TABLES
from proxy_index.calculated import store
from proxy_index.cli import main
from proxy_index.partitions import children, partition
from proxy_index.storage import connect, ingest, initialize, seed

pytest_plugins = ["test_storage"]

DAY = date(2026, 10, 1)
SLOT = datetime(2026, 10, 1, 10, tzinfo=UTC)
FACTS = {
    "test_batches": "batch_id",
    "test_runs": "run_key",
    "batch_runs": "batch_id,product_id",
    "run_buckets": "run_key,bucket_start",
    "run_geo_buckets": "run_key,bucket_start,location_id,network_id",
    "ip_observations": "run_key,bucket_start,ip_id",
    "run_enrichments": "run_key,release_id",
    "estimated_hours": "product_id,country_code,day,hour",
    "calculation_roles": "run_key",
}


def batch(conn, at, *, retry_of=None):
    return conn.execute(
        "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,start_at,"
        "started_at,finished_at,status,retry_of) SELECT 'census','DE',region_id,'generic_http',"
        "%s,%s,%s,%s,'done',%s FROM countries WHERE country_code='DE' RETURNING batch_id",
        (at, at, at, at + timedelta(minutes=30), retry_of),
    ).fetchone()[0]


def event(conn, at, batch_id):
    partition(conn, "test_events", at)
    conn.execute(
        "INSERT INTO test_events(at,kind,batch_id,country_code,target_id) "
        "VALUES (%s,'census_started',%s,'DE','generic_http')",
        (at, batch_id),
    )


async def day_of_data(database, tmp_path):
    """A census batch on DAY with its run (geo, enrichment, node, estimate, role, tape), a batch
    of the day before that it retries, and a daily-test run of today (not part of DAY)."""
    seed(database)
    store.initialize(database)
    with connect(database) as conn:
        before = batch(conn, SLOT - timedelta(days=1))
        own = batch(conn, SLOT, retry_of=before)
    directory, _ = await census(tmp_path / "census")
    enrichment(directory)
    key = ingest(directory, database, batch_id=own)["run_key"]
    ingest(await measurement(tmp_path / "daily"), database)
    with connect(database) as conn:
        product_id = conn.execute(
            "SELECT product_id FROM test_runs JOIN scope_stats USING(scope_id) WHERE run_key=%s",
            (key,),
        ).fetchone()[0]
        node = conn.execute(
            "INSERT INTO nodes(region_id,provider,product,token_hash) SELECT region_id,"
            "'fixture','residential','secret-token-hash' FROM countries WHERE country_code='DE' "
            "RETURNING node_id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,node_id,status,started_at,finished_at,"
            "run_key) VALUES (%s,%s,%s,'done',%s,%s,%s)",
            (own, product_id, node, SLOT, SLOT + timedelta(minutes=20), key),
        )
        conn.execute(
            "INSERT INTO estimated_hours(product_id,country_code,day,hour,run_key,measured_at,"
            "measured_ips,measured_requests,hour_factor,unique_ips,requests,day_unique_ips,curve,"
            "formula_version) VALUES (%s,'DE',%s,10,%s,%s,3,3,1.0,3.0,3.0,30.5,'country',1)",
            (product_id, DAY, key, SLOT),
        )
        conn.execute("INSERT INTO calculation_roles VALUES (%s,'benchmark')", (key,))
        event(conn, SLOT + timedelta(minutes=1), own)
        event(conn, SLOT + timedelta(minutes=2), before)
    return key


def empty_database(databases, *, calculated=True):
    target = databases()
    initialize(target)
    if calculated:
        store.initialize(target)
    return target


def fact_rows(database, run_key):
    """The day's fact rows: the run's rows and the day's batch rows."""
    found = {}
    with connect(database) as conn:
        batch_id = conn.execute(
            "SELECT batch_id FROM test_runs WHERE run_key=%s", (run_key,)
        ).fetchone()[0]
        for table, order in FACTS.items():
            column, value = ("batch_id", batch_id) if "batch" in table else ("run_key", run_key)
            found[table] = conn.execute(
                f"SELECT * FROM {table} WHERE {column}=%s ORDER BY {order}", (value,)
            ).fetchall()
    return found


def members(path):
    with tarfile.open(path, "r:gz") as archive:
        return {m.name: archive.extractfile(m).read() for m in archive.getmembers()}


def rewrite(path, change):
    """Write the archive again with change(members) applied (a dict of name -> bytes)."""
    files = change(members(path))
    with tarfile.open(path, "w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


async def test_a_day_exports_deterministically_and_restores_into_an_empty_database(
    database, databases, tmp_path
):
    key = await day_of_data(database, tmp_path)
    path, manifest = export_day(database, DAY, tmp_path / "out")
    assert path.name == "2026-10-01.tar.gz"
    assert not list((tmp_path / "out").glob("*.parts"))
    files = members(path)
    assert list(files) == [MANIFEST] + [t["file"] for t in manifest["tables"]]
    assert [t["name"] for t in manifest["tables"]] == [t.name for t in TABLES]
    assert json.loads(files[MANIFEST]) == manifest
    counts = {t["name"]: t["rows"] for t in manifest["tables"]}
    assert {name: counts[name] for name in ("test_batches", "test_runs", "batch_runs")} == {
        "test_batches": 1,
        "test_runs": 1,
        "batch_runs": 1,
    }
    assert counts["run_buckets"] and counts["ip_observations"] and counts["run_geo_buckets"] == 2
    assert (counts["test_events"], counts["estimated_hours"], counts["nodes"]) == (2, 1, 1)
    assert b"secret-token-hash" not in files["nodes.csv"] and b"archived-" in files["nodes.csv"]
    assert manifest["day"] == "2026-10-01" and manifest["storage_schema"] == 10
    first = file_digests(path)
    assert first["bytes"] == path.stat().st_size
    assert file_digests(export_day(database, DAY, tmp_path / "again")[0]) == first

    target = empty_database(databases)
    report = restore_file(path, target)
    assert report["day"] == "2026-10-01"
    assert {name: report["inserted"][name] for name in FACTS} == {
        name: counts[name] for name in FACTS
    }
    source, restored = fact_rows(database, key), fact_rows(target, key)
    # The batch it retries (test_batches.retry_of, the last column) is not in the file, so the
    # restored link is NULL; everything else is equal.
    (batch_row,) = source["test_batches"]
    assert batch_row[-1] is not None
    assert restored == source | {"test_batches": [(*batch_row[:-1], None)]}
    with connect(target) as conn:
        # The second event's batch (the retried one) is not in the file either.
        assert conn.execute("SELECT batch_id IS NULL FROM test_events ORDER BY at").fetchall() == [
            (False,),
            (True,),
        ]
        assert conn.execute("SELECT token_hash FROM nodes").fetchone()[0].startswith("archived-")
        # Identity sequences moved past the restored ids.
        conn.execute("INSERT INTO providers VALUES ('later')")
        later = conn.execute(
            "INSERT INTO products(provider,name) VALUES ('later','x') RETURNING product_id"
        ).fetchone()[0]
        assert (
            later
            > conn.execute(
                "SELECT max(product_id) FROM test_runs JOIN scope_stats USING(scope_id)"
            ).fetchone()[0]
        )
    again = restore_file(path, target)
    assert not any(again["inserted"].values())
    # Into the database the file came from: everything is already there.
    assert not any(restore_file(path, database)["inserted"].values())
    assert fact_rows(database, key) == source


async def test_restore_refuses_files_and_databases_that_do_not_fit(database, databases, tmp_path):
    await day_of_data(database, tmp_path)
    path, _ = export_day(database, DAY, tmp_path / "out")
    original = path.read_bytes()

    def refused(target, message, file=path):
        with pytest.raises(ArchiveError, match=message):
            restore_file(file, target)

    refused(databases(), "no proxy-index schema")
    refused(empty_database(databases, calculated=False), "no table calculation_roles")
    stale = empty_database(databases)
    with connect(stale) as conn:
        conn.execute("UPDATE schema_versions SET version=9")
    refused(stale, "storage schema v10, the database v9")
    other = empty_database(databases)
    with connect(other) as conn:
        conn.execute("INSERT INTO providers VALUES ('other')")
        conn.execute("INSERT INTO products(provider,name) VALUES ('other','first')")
    refused(other, "ids differ from the archive's")
    with connect(other) as conn:
        assert conn.execute("SELECT count(*) FROM test_runs").fetchone()[0] == 0

    target = empty_database(databases)
    rewrite(
        path, lambda f: f | {"test_runs.csv": f["test_runs.csv"].replace(b"completed", b"failed")}
    )
    refused(target, "test_runs.csv does not match its manifest entry")
    path.write_bytes(original)
    rewrite(path, lambda f: f | {"extra.csv": b"x"})
    refused(target, "files its manifest does not list")
    path.write_bytes(original)
    rewrite(path, lambda f: {k: v for k, v in f.items() if k != "test_events.csv"})
    refused(target, "test_events.csv is missing or out of order")
    path.write_bytes(original)
    rewrite(path, lambda f: {k: f[k] for k in [*list(f)[1:], MANIFEST]})
    refused(target, "does not start with manifest.json")

    def manifest(change):
        def apply(f):
            data = json.loads(f[MANIFEST])
            change(data)
            return f | {MANIFEST: json.dumps(data).encode()}

        path.write_bytes(original)
        rewrite(path, apply)

    manifest(lambda m: m.update(version=99))
    refused(target, "not a proxy-index raw day archive")
    manifest(lambda m: m["tables"][0].update(name="pg_authid"))
    refused(target, "unknown table entry")
    manifest(lambda m: m["tables"][0].update(columns=["id; DROP TABLE x"]))
    refused(target, "unknown table entry")
    with connect(target) as conn:
        assert conn.execute("SELECT count(*) FROM providers").fetchone()[0] == 0


async def test_archive_cli_exports_and_restores(database, databases, tmp_path, capsys):
    await day_of_data(database, tmp_path)
    out = tmp_path / "out"
    assert (
        main(
            [
                "archive",
                "export",
                "--date",
                "2026-10-01",
                "--database-file",
                str(database),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["file"] == str(out / "2026-10-01.tar.gz")
    assert printed["rows"]["test_runs"] == 1 and len(printed["sha256"]) == 64
    target = empty_database(databases)
    args = ["archive", "restore", "--file", printed["file"], "--database-file", str(target)]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["inserted"]["test_runs"] == 1
    with connect(target) as conn:
        conn.execute("UPDATE schema_versions SET version=9")
    assert main(args) == 2
    assert "Restore refused" in capsys.readouterr().err


def test_raw_tables_get_daily_partitions_unless_the_month_has_one(database):
    with connect(database) as conn:
        partition(conn, "run_buckets", datetime(2026, 12, 3, 23, 59, tzinfo=UTC))
        partition(conn, "run_buckets", datetime(2026, 12, 3, 0, 0, tzinfo=UTC))
        conn.execute(
            "CREATE TABLE ip_observations_2026_12 PARTITION OF ip_observations "
            "FOR VALUES FROM ('2026-12-01') TO ('2027-01-01')"
        )
        conn.execute(
            "CREATE TABLE ip_observations_misc PARTITION OF ip_observations "
            "FOR VALUES FROM ('2020-01-01') TO ('2020-01-02')"
        )
        partition(conn, "ip_observations", datetime(2026, 12, 9, tzinfo=UTC))
        partition(conn, "ip_observations", datetime(2027, 1, 2, 5, tzinfo=UTC))
        partition(conn, "test_events", datetime(2026, 12, 9, tzinfo=UTC))
        assert children(conn, "run_buckets") == [
            ("run_buckets_2026_12_03", date(2026, 12, 3), date(2026, 12, 4))
        ]
        assert children(conn, "ip_observations") == [
            ("ip_observations_2026_12", date(2026, 12, 1), date(2027, 1, 1)),
            ("ip_observations_2027_01_02", date(2027, 1, 2), date(2027, 1, 3)),
        ]
        assert [name for name, _, _ in children(conn, "test_events")] == ["test_events_2026_12"]
