"""storage.ingest_many (INGEST-RTT-1): a batch stores exactly what importing its runs one at a
time stores, and a batch that fails stores nothing and names its runs."""

import gzip
import json
from datetime import timedelta

import psycopg
import pytest
from ingest_cases import RELEASE, START, daily_test, dump, pulse, run_id

from atlas.storage import IngestBatchError, connect, ingest, ingest_many, seed, status

pytest_plugins = ["test_storage"]


def mix(tmp_path):
    """Daily tests and pulses in batch order: d0 and d3 share a scope (and d0, d1 IPs at other
    providers), p0 and p2 share a scope (and p0, p1 IPs at other providers)."""
    small = {"ips": 300, "repeats": 100}
    return [
        daily_test(tmp_path / "d0", 0, **small),
        pulse(tmp_path / "p0", 0),
        daily_test(tmp_path / "d1", 1, **small),
        pulse(tmp_path / "p1", 1),
        daily_test(tmp_path / "d3", 3, **small),
        pulse(tmp_path / "p2", 2),
    ]


def rewrite(path, change):
    with gzip.open(path, "rt") as stream:
        rows = [json.loads(line) for line in stream]
    with gzip.open(path, "wt") as stream:
        stream.writelines(json.dumps(change(n, row)) + "\n" for n, row in enumerate(rows))


def test_a_batch_stores_what_one_at_a_time_stores(databases, database_template, tmp_path):
    directories = mix(tmp_path)
    alone, together = (databases(template=database_template) for _ in range(2))
    for database in (alone, together):
        ingest(directories[2], database)  # stored before the batch: skipped
    expected = [ingest(directory, alone) for directory in directories]
    results = ingest_many(directories, together)
    assert [result["imported"] for result in results] == [True, True, False, True, True, True]
    keys = ("run_id", "imported", "observation_rows", "enrichment_releases_added", "scope_hash")
    assert [{k: r[k] for k in keys} for r in results] == [{k: r[k] for k in keys} for r in expected]
    with connect(alone) as one, connect(together) as other:
        stored = dump(other)
        assert stored == dump(one)
    assert len(stored["provider_overlap"]) == 2 and len(stored["run_geo_buckets"]) == 3 * 70


def test_pulses_without_ip_rows_store_what_one_at_a_time_stores(
    databases, database_template, tmp_path
):
    pulses = [pulse(tmp_path / f"p{n}", n) for n in range(4)]
    alone, together = (databases(template=database_template) for _ in range(2))
    for directory in pulses:
        ingest(directory, alone, ip_rows=False)
    results = ingest_many(pulses, together, ip_rows=False)
    assert {result["observation_rows"] for result in results} == {0}
    with connect(alone) as one, connect(together) as other:
        stored = dump(other)
        assert stored == dump(one)
    assert not stored["ip_addresses"] and len(stored["scope_stats"]) == 2


def test_a_database_error_rolls_the_batch_back(database, tmp_path):
    good, bad = daily_test(tmp_path / "d0", 0, ips=50, repeats=10), pulse(tmp_path / "p0", 0)
    path = bad / "summary.json"
    path.write_text(json.dumps(json.loads(path.read_text()) | {"status": "lost"}))
    with pytest.raises(IngestBatchError) as raised:
        ingest_many([good, bad], database)
    assert raised.value.run_ids == [run_id("daily", 0), run_id("pulse", 0)]
    assert isinstance(raised.value.__cause__, psycopg.errors.CheckViolation)
    assert status(database)["counts"]["test_runs"] == 0
    # One at a time, the good run is stored and the bad one raises its own error.
    assert ingest(good, database)["imported"]
    with pytest.raises(psycopg.errors.CheckViolation):
        ingest(bad, database)
    assert status(database)["counts"]["test_runs"] == 1


def test_a_failed_check_in_the_transaction_rolls_the_batch_back(database, tmp_path):
    stored, fresh = pulse(tmp_path / "p0", 0), pulse(tmp_path / "p1", 1)
    ingest(stored, database)
    path = stored / "summary.json"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(IngestBatchError, match=run_id("pulse", 1)) as raised:
        ingest_many([fresh, stored], database)
    assert str(raised.value.__cause__) == "Run ID already exists with different content"
    assert status(database)["counts"]["test_runs"] == 1


def test_every_directory_is_checked_before_the_transaction(tmp_path):
    good, bad = pulse(tmp_path / "p0", 0), pulse(tmp_path / "p1", 1)
    (bad / "ips.jsonl.gz").write_bytes(b"tampered")
    nowhere = tmp_path / "missing.toml"  # no database is reached
    with pytest.raises(ValueError, match="IP artifact checksum mismatch"):
        ingest_many([good, bad], nowhere)
    with pytest.raises(ValueError, match="one batch id"):
        ingest_many([good], nowhere, batch_ids=[])


def test_a_run_twice_in_a_batch_fails_it(database, tmp_path):
    """One at a time the second copy is a replay; in one batch both are new: test_runs refuses."""
    directory = pulse(tmp_path / "p0", 0)
    with pytest.raises(IngestBatchError) as raised:
        ingest_many([directory, directory], database)
    assert isinstance(raised.value.__cause__, psycopg.errors.UniqueViolation)
    assert [ingest(directory, database)["imported"] for _ in range(2)] == [True, False]


def test_batch_ids_link_new_and_stored_runs(database, tmp_path):
    seed(database)
    with connect(database) as conn:
        first, second, third = (
            conn.execute(
                "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
                "start_at) SELECT 'pulse','DE',region_id,'generic_http',%s,%s FROM countries "
                "WHERE country_code='DE' RETURNING batch_id",
                (START + timedelta(hours=h), START + timedelta(hours=h)),
            ).fetchone()[0]
            for h in range(3)
        )
    stored, fresh = pulse(tmp_path / "p0", 0), pulse(tmp_path / "p1", 1)
    ingest(stored, database, ip_rows=False)  # stored without a batch: it joins its batch
    ingest_many([stored, fresh], database, batch_ids=[first, second], ip_rows=False)
    with connect(database) as conn:
        linked = dict(conn.execute("SELECT run_id::text,batch_id FROM test_runs"))
    assert linked == {run_id("pulse", 0): first, run_id("pulse", 1): second}
    with pytest.raises(IngestBatchError) as raised:
        ingest_many([stored], database, batch_ids=[third], ip_rows=False)
    assert str(raised.value.__cause__) == "Run already belongs to another batch"


def test_reports_of_one_release_agree_across_the_batch(database, tmp_path):
    first, second = (daily_test(tmp_path / f"d{n}", n, ips=50, repeats=10) for n in range(2))
    rewrite(
        second / f"enriched-{RELEASE}.jsonl.gz",
        lambda n, row: row | {"enrichment": row["enrichment"] | {"city": "Elsewhere"}},
    )
    with pytest.raises(IngestBatchError) as raised:
        ingest_many([first, second], database)
    assert str(raised.value.__cause__) == "Enrichment release conflicts with prior data"
    assert status(database)["counts"]["test_runs"] == 0
    ingest(first, database)
    with pytest.raises(ValueError, match="conflicts with prior data"):
        ingest(second, database)


def test_one_report_gives_an_ip_one_enrichment(database, tmp_path):
    """An IP seen in two buckets of a run must have the same attributes in both."""
    directory = daily_test(tmp_path / "d0", 0, ips=50, repeats=10)
    seen = {}

    def change(n, row):
        if seen.setdefault(row["ip"], n) != n:
            return row | {"enrichment": row["enrichment"] | {"city": "Elsewhere"}}
        return row

    rewrite(directory / f"enriched-{RELEASE}.jsonl.gz", change)
    with pytest.raises(ValueError, match="Conflicting enrichment for the same IP"):
        ingest(directory, database)
    assert status(database)["counts"]["test_runs"] == 0
