"""PULSE-COUNTERS-1: storage.ingest(counters=True) keeps a run's exit IPs only in the lifetime
counters (ip_addresses, product_ips, provider_overlap) with its enrichment, no ip_observations,
pool_ips or scope_ips rows; a run is counted with its first enrichment, and only then."""

import gzip
import json
from datetime import datetime

from test_storage import enrichment, measurement

from atlas.storage import connect, ingest, ingest_many, status

pytest_plugins = ["test_storage"]

TABLES = ("ip_observations", "pool_ips", "scope_ips", "ip_addresses", "run_enrichments")


def counted(database):
    """The rows of TABLES, and each product_ips row's counters and seen times."""
    with connect(database) as conn:
        rows = [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES]
        products = conn.execute(
            "SELECT product,observation_count,run_count,first_seen,last_seen "
            "FROM product_ip_details ORDER BY provider,product"
        ).fetchall()
    return dict(zip(TABLES, rows, strict=True)), products


def seen(directory):
    """The (first, last) seen times of a run's one IP row."""
    with gzip.open(directory / "ips.jsonl.gz", "rt") as f:
        (row,) = [json.loads(line) for line in f]
    return tuple(datetime.fromisoformat(row[k]) for k in ("first_seen", "last_seen"))


async def test_counters_keep_a_run_s_ips_only_in_the_lifetime_counters(database, tmp_path):
    first = await measurement(tmp_path / "a")
    enrichment(first)
    done = ingest(first, database, counters=True)
    assert done["imported"] and done["counted_ip_rows"] == 1 and done["observation_rows"] == 0
    assert not ingest(first, database, counters=True)["imported"]  # a replay counts nothing
    tables, products = counted(database)
    assert tables == dict(zip(TABLES, (0, 0, 0, 1, 1), strict=True))
    assert [p[:3] for p in products] == [("residential", 3, 1)]
    second = await measurement(tmp_path / "b")
    enrichment(second)
    ingest(second, database, counters=True)
    ((_, requests, runs, first_seen, last_seen),) = counted(database)[1]
    assert (requests, runs) == (6, 2)  # each run's requests, and the run once
    assert (first_seen, last_seen) == (seen(first)[0], seen(second)[1])
    other = await measurement(tmp_path / "c", provider="beta")
    enrichment(other)
    ingest(other, database, counters=True)  # the provider overlap still counts the shared IP
    assert status(database)["provider_overlaps"] == [
        {"provider_a": "beta", "provider_b": "fixture", "shared_ips": 1, "window": "lifetime"}
    ]


async def test_a_run_is_counted_with_its_first_enrichment_and_only_then(database, tmp_path):
    directory = await measurement(tmp_path / "a")
    done = ingest(directory, database, counters=True)  # no DB-IP anywhere: stored, not counted
    assert done["imported"] and done["counted_ip_rows"] == 0
    assert counted(database) == (dict(zip(TABLES, (0, 0, 0, 0, 0), strict=True)), [])
    enrichment(directory)  # a retried upload or the backfill brings its enrichment
    done = ingest(directory, database, counters=True)
    assert not done["imported"] and done["counted_ip_rows"] == 1
    assert ingest(directory, database, counters=True)["counted_ip_rows"] == 0
    enrichment(directory, release="c" * 64)  # a second release adds labels, not a second count
    assert ingest(directory, database, counters=True)["enrichment_releases_added"] == 1
    tables, products = counted(database)
    assert tables == dict(zip(TABLES, (0, 0, 0, 1, 2), strict=True))
    assert [p[:3] for p in products] == [("residential", 3, 1)]


async def test_a_batch_counts_its_stored_and_new_runs_together(database, tmp_path):
    stored, new = await measurement(tmp_path / "a"), await measurement(tmp_path / "b")
    ingest(stored, database, counters=True)
    enrichment(stored)
    enrichment(new)
    done = ingest_many([stored, new], database, counters=True)
    assert [d["imported"] for d in done] == [False, True]
    assert [d["counted_ip_rows"] for d in done] == [1, 1]
    assert [p[:3] for p in counted(database)[1]] == [("residential", 6, 2)]
    with connect(database) as conn:  # counters do not touch the scope's lifetime unique IPs
        metrics = conn.execute("SELECT metrics->>'unique_ips' FROM scope_stats").fetchone()
    assert metrics == ("0",)
