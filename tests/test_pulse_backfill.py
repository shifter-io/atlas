"""PULSE-COUNTERS-1: `atlas daily pulse-backfill` counts the exit IPs of the pulse runs stored
without them, from their spooled uploads, as their import now does; it is idempotent, never
counts a run twice, and only counts without --apply."""

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dbip_fakes import DE, US, tiny_mmdb
from test_storage import enrichment, measurement

from atlas.cli import main
from atlas.daily.pulse_backfill import backfill
from atlas.enrichment import SharedEnricher
from atlas.orchestrator.archive import pack
from atlas.orchestrator.control import write_settings
from atlas.storage import IngestBatchError, connect, ingest, seed

pytest_plugins = ["test_storage"]

# What an import stores for a run's IPs, beyond its own rows (times of the update left out).
TABLES = {
    "ip_observations": "SELECT * FROM ip_observations ORDER BY 1,2,3",
    "product_ips": "SELECT * FROM product_ips ORDER BY 1,2",
    "pool_ips": "SELECT * FROM pool_ips ORDER BY 1,2",
    "scope_ips": "SELECT * FROM scope_ips ORDER BY 1,2",
    "overlap": "SELECT provider_a,provider_b,shared_ips FROM provider_overlap ORDER BY 1,2",
    "ip_enrichment": "SELECT * FROM ip_enrichment ORDER BY 1,2",
    "run_enrichments": "SELECT * FROM run_enrichments ORDER BY 1,2",
    "releases": "SELECT sha256 FROM dbip_releases ORDER BY 1",
}


@pytest.fixture
def mmdb(tmp_path):
    path = tmp_path / "dbip-full.mmdb"
    path.write_bytes(tiny_mmdb(DE, US))
    return path


def pulse_batch(conn, country):
    region = conn.execute(
        "SELECT region_id FROM countries WHERE country_code=%s", (country,)
    ).fetchone()[0]
    return conn.execute(
        "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,start_at,"
        "status) VALUES ('pulse',%s,%s,'generic_http',%s,%s,'done') RETURNING batch_id",
        (country, region, datetime.now(UTC), datetime.now(UTC)),
    ).fetchone()[0]


async def measured(tmp_path):
    """A census run of alpha, then pulses of alpha and beta (the same exit IP), each pulse
    enriched by its droplet: [(directory, network, country)]."""
    found = [(await measurement(tmp_path / "census", provider="alpha"), "alpha", None)]
    for network, country in (("alpha", "DE"), ("beta", "FR")):
        directory = await measurement(tmp_path / network, provider=network)
        enrichment(directory, release="b" * 64)  # the droplet's
        found.append((directory, network, country))
    return found


def store(database, runs, root, mmdb=None):
    """The runs stored as before PULSE-ENRICH-1 (pulses without IP rows) and their uploads
    spooled under root; with mmdb, stored as the manager now stores them. The pulses' names
    and batch ids."""
    seed(database)
    names = []
    for directory, network, country in runs:
        if country is None:
            ingest(directory, database)
            continue
        with connect(database) as conn:
            batch = pulse_batch(conn, country)
        names.append((f"pulse-b{batch}-{directory.name}.tar.gz", batch))
        folder = root / f"{datetime.now(UTC):%Y/%m/%d/%H}" / network
        folder.mkdir(parents=True, exist_ok=True)
        pack(directory, folder / names[-1][0])
        if mmdb:
            directory = Path(shutil.copytree(directory, root / "now" / directory.name))
            SharedEnricher(mmdb).reenrich(directory)
        ingest(directory, database, batch_id=batch, ip_rows=False, counters=mmdb is not None)
    return names


def tables(database):
    with connect(database) as conn:
        return {name: conn.execute(sql).fetchall() for name, sql in TABLES.items()}


async def test_the_backfill_stores_what_the_import_now_stores(
    databases, database_template, tmp_path, mmdb
):
    reference, database = (databases(template=database_template) for _ in range(2))
    runs = await measured(tmp_path)
    store(reference, runs, tmp_path / "spool-now", mmdb)
    store(database, runs, tmp_path / "spool")
    before = tables(database)
    assert not before["run_enrichments"] and len(before["product_ips"]) == 1  # the census
    report = backfill(database, spool=tmp_path / "spool", mmdb=mmdb, apply=True, batch=1)
    assert report == {
        "candidates": 2,
        "raw_files": 2,
        "runs": 2,
        "ip_rows": 2,
        "product_ips": 1,
        "refused": [],
        "no_raw_file": [],
        "candidates_after": 0,
        "product_ips_after": 2,
    }
    after = tables(database)
    assert after == tables(reference)
    assert after["overlap"] == [("alpha", "beta", 1)] and len(after["ip_observations"]) == 1
    assert [row[4:] for row in after["product_ips"]] == [(6, 2), (3, 1)]  # census + pulse, beta
    release = hashlib.sha256(mmdb.read_bytes()).hexdigest()
    assert after["releases"] == [(release,)]  # the manager's, not the droplet's
    again = backfill(database, spool=tmp_path / "spool", mmdb=mmdb, apply=True)
    assert again["candidates"] == 0 and again["runs"] == 0 and tables(database) == after


async def test_without_apply_it_only_counts_and_names_runs_without_a_raw_file(
    database, tmp_path, mmdb, capsys
):
    root = tmp_path / "spool"
    names = store(database, await measured(tmp_path), root)
    for upload in root.rglob(names[1][0]):
        upload.unlink()  # beta's raw file is gone
    with connect(database) as conn:
        write_settings(conn, {"raw.spool_dir": str(root)})  # the default spool
    before = tables(database)
    args = ["daily", "pulse-backfill", "--database-file", str(database), "--mmdb", str(mmdb)]
    assert main(args) == 0
    counted = json.loads(capsys.readouterr().out)
    assert (counted["candidates"], counted["raw_files"], counted["runs"]) == (2, 1, 0)
    assert counted["candidates_after"] == 2 and tables(database) == before
    assert main([*args, "--apply"]) == 3  # beta stays without enrichment
    applied = json.loads(capsys.readouterr().out)
    assert (applied["raw_files"], applied["runs"], applied["candidates_after"]) == (1, 1, 1)
    assert applied["no_raw_file"] == [names[1][0]]


async def test_a_run_counted_by_a_retried_upload_is_not_counted_again(database, tmp_path, mmdb):
    root = tmp_path / "spool"
    runs = await measured(tmp_path)
    (_, alpha), _ = store(database, runs, root)
    ingest(runs[1][0], database, batch_id=alpha, counters=True)  # its droplet's enrichment
    counters = tables(database)["product_ips"]
    report = backfill(database, spool=root, mmdb=mmdb, apply=True)
    assert (report["candidates"], report["runs"], report["candidates_after"]) == (1, 1, 0)
    found = tables(database)
    assert found["product_ips"][0] == counters[0]  # alpha: the census and its pulse, once each
    release = hashlib.sha256(mmdb.read_bytes()).hexdigest()
    assert found["releases"] == sorted([(release,), ("b" * 64,)])  # alpha kept the droplet's


async def test_a_run_enrichment_refuses_is_named_and_left(database, tmp_path, mmdb, monkeypatch):
    root = tmp_path / "spool"
    names = store(database, await measured(tmp_path), root)

    def refuse(self, directory):
        raise ValueError("IP artifact checksum does not match summary")

    monkeypatch.setattr(SharedEnricher, "reenrich", refuse)
    report = backfill(database, spool=root, mmdb=mmdb, apply=True)
    assert report["refused"] == [name for name, _ in names] and report["runs"] == 0
    assert report["candidates_after"] == 2


async def test_a_backfill_refuses_a_database_of_another_schema(database, tmp_path, mmdb):
    store(database, await measured(tmp_path), tmp_path / "spool")
    with connect(database) as conn:
        conn.execute("UPDATE schema_versions SET version=version-1")
    with pytest.raises(IngestBatchError):
        backfill(database, spool=tmp_path / "spool", mmdb=mmdb, apply=True)
    with connect(database) as conn:
        conn.execute("UPDATE schema_versions SET version=version+1")
    assert not tables(database)["run_enrichments"]  # rolled back
    assert backfill(database, apply=True)["raw_files"] == 0  # no spool configured: none found
