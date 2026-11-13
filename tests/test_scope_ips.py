"""The IPs new to a scope (schema v14, SCOPE-IPS-1): counted from the scope_ips membership set,
they equal v13's scan of ip_observations, on a fresh database and on a v13 one back-filled by
db-migrate; re-importing stored runs changes nothing.

The v13 build is this build with v13's schema (fixtures/schema_v13.sql) and its statement
(V13_COUNT) in place of storage_ingest.add_scope_ips.
"""

from contextlib import contextmanager
from pathlib import Path

import ingest_cases
from ingest_cases import daily_test, dump

from atlas import storage, storage_ingest
from atlas.storage import V13_CHECKSUM, connect, ingest_many, initialize, migrate

pytest_plugins = ["test_storage"]

SCHEMA_V13 = Path(__file__).parent / "fixtures/schema_v13.sql"
# v13's count: a batch's pairs with no observation of their scope outside the batch.
V13_COUNT = (
    "SELECT idx,count(*) FROM (SELECT scope_id,ip_id,min(idx) AS idx FROM "
    "incoming_observations GROUP BY scope_id,ip_id) n WHERE NOT EXISTS (SELECT 1 FROM "
    "ip_observations o JOIN test_runs r USING(run_key) WHERE o.ip_id=n.ip_id AND "
    "r.scope_id=n.scope_id AND o.run_key NOT IN (SELECT run_key FROM incoming_runs)) "
    "GROUP BY idx"
)
RECOUNT = (
    "SELECT DISTINCT r.scope_id,o.ip_id FROM ip_observations o JOIN test_runs r USING(run_key) "
    "ORDER BY 1,2"
)
MEMBERS = "SELECT scope_id,ip_id FROM scope_ips ORDER BY 1,2"
# The IPs new to each run's scope per batch (by batch position), from batches() below.
COUNTS = [{0: 300, 1: 210, 2: 300}, {1: 210, 2: 210}, {0: 210}]


@contextmanager
def v13_build(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 13)
        patch.setattr(storage, "_schema", lambda: (SCHEMA_V13.read_text(), V13_CHECKSUM))
        patch.setattr(storage_ingest, "add_scope_ips", lambda conn: conn.execute(V13_COUNT))
        patch.delitem(ingest_cases.DUMP, "scope_ips")
        yield


def batches(tmp_path):
    """Three batches of daily tests of 300 IPs, each sharing 90 with the next index: d0 to d4 in
    one scope, d5 in another of the same IPs as d4. d1 overlaps d0 in the first batch, is stored
    again in the second (skipped), where d2 overlaps it and d3 overlaps d2; d4 overlaps d3."""

    def run(index, provider="daily-same"):
        directory = tmp_path / f"d{index}"
        if not directory.exists():
            daily_test(directory, index, ips=300, repeats=100, provider=provider)
        return directory

    return [[run(0), run(1), run(5, "daily-other")], [run(1), run(2), run(3)], [run(4)]]


def ingest_all(database, runs, monkeypatch):
    """Import each batch in turn; the IPs new to each run's scope that every batch counted."""
    counted, totals = [], storage._scope_totals

    def recorded(found, scopes, new_ips):
        counted.append(dict(new_ips))
        return totals(found, scopes, new_ips)

    with monkeypatch.context() as patch:
        patch.setattr(storage, "_scope_totals", recorded)
        for batch in runs:
            ingest_many(batch, database)
    return counted


def stored(database):
    with connect(database) as conn:
        return dump(conn)


def unique_ips(database):
    with connect(database) as conn:
        query = "SELECT scope_hash,metrics->'unique_ips' FROM scope_stats ORDER BY 1"
        return conn.execute(query).fetchall()


def test_v14_counts_equal_the_v13_scan(databases, tmp_path, monkeypatch):
    runs = batches(tmp_path)
    old, new = databases(), databases()
    with v13_build(monkeypatch):
        initialize(old)
        assert ingest_all(old, runs, monkeypatch) == COUNTS
        before = stored(old)
    initialize(new)
    assert ingest_all(new, runs, monkeypatch) == COUNTS
    after = stored(new)
    assert before == {table: rows for table, rows in after.items() if table != "scope_ips"}
    assert sorted(n for _, n in unique_ips(new)) == [300, 1140]
    assert len(after["scope_ips"]) == 1440
    result = migrate(old)  # the back-fill gives the same memberships
    assert (result["from_schema_version"], result["schema_version"]) == (13, 15)
    assert stored(old) == after


def test_a_backfilled_v13_database_counts_on_as_a_v14_one(databases, tmp_path, monkeypatch):
    runs = batches(tmp_path)
    old, new = databases(), databases()
    with v13_build(monkeypatch):
        initialize(old)
        assert ingest_all(old, runs[:1], monkeypatch) == COUNTS[:1]
    migrate(old)
    with connect(old) as conn:
        assert conn.execute(MEMBERS).fetchall() == conn.execute(RECOUNT).fetchall()
    assert ingest_all(old, runs[1:], monkeypatch) == COUNTS[1:]
    initialize(new)
    assert ingest_all(new, runs, monkeypatch) == COUNTS
    assert stored(old) == stored(new)
    assert unique_ips(old) == unique_ips(new)
    with connect(old) as conn:
        assert conn.execute(MEMBERS).fetchall() == conn.execute(RECOUNT).fetchall()


def test_reimporting_stored_runs_changes_nothing(database, tmp_path, monkeypatch):
    runs = batches(tmp_path)
    ingest_all(database, runs, monkeypatch)
    before = stored(database)
    again = list(dict.fromkeys(directory for batch in runs for directory in batch))
    assert not any(result["imported"] for result in ingest_many(again, database))
    assert stored(database) == before
