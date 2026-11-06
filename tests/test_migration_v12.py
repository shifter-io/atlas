"""In-place v11 → v12 migration (ip_observations indexed by time, then IP), on monthly and daily
partitions; a slice's distinct-IP count reads the new index; a v11 build refuses v12.

fixtures/schema_v11.sql is the exact v11 schema (storage.V11_CHECKSUM); load_v11 builds the v11
release's database from test_migration_v11's v10 database the way the v11 release migrated it.
The v4 dump's observations sit in the monthly partition ip_observations_2026_10; load_v11 copies
them into the daily partitions of 2026-11-01 and 2026-11-02 (created as ARCHIVE-1 does).
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from migration_cases import SOURCE, catalog
from test_migration_v11 import load_v10
from test_storage import measurement

from proxy_index import storage
from proxy_index.calculated import store
from proxy_index.partitions import partition
from proxy_index.storage import SCHEMA_VERSION, V11_CHECKSUM, connect, ingest, initialize, migrate

pytest_plugins = ["test_storage"]

SCHEMA_V11 = Path(__file__).parent / "fixtures/schema_v11.sql"
DAYS = (31, 32)  # the October observations, shifted into two November days
# The secondary (non-primary-key) indexes of every ip_observations partition, with the
# partitioned index each is attached to.
PARTITION_INDEXES = (
    "SELECT c.relname,pg_get_indexdef(i.indexrelid),p.relname,i.indisvalid "
    "FROM pg_inherits h JOIN pg_class c ON c.oid=h.inhrelid "
    "JOIN pg_index i ON i.indrelid=c.oid AND NOT i.indisprimary "
    "LEFT JOIN pg_inherits a ON a.inhrelid=i.indexrelid LEFT JOIN pg_class p ON p.oid=a.inhparent "
    "WHERE h.inhparent='ip_observations'::regclass ORDER BY 1,2"
)
SLICE = (
    "SELECT count(DISTINCT ip_id) FROM ip_observations WHERE bucket_start>=%s AND bucket_start<%s"
)


def load_v11(database):
    """The live shape: the v11 release's database (its exact schema), calculated v7, with
    observations in one monthly and two daily partitions."""
    load_v10(database)
    schema = SCHEMA_V11.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V11_CHECKSUM
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v10_v11.sql").read_text())
        conn.execute(schema)
        conn.execute("UPDATE schema_versions SET version=11,checksum=%s", (V11_CHECKSUM,))
        for days in DAYS:
            partition(conn, "ip_observations", datetime(2026, 10, 1, tzinfo=UTC) + timedelta(days))
            conn.execute(
                "INSERT INTO ip_observations SELECT bucket_start+%(d)s,run_key,ip_id,count,"
                "first_seen+%(d)s,last_seen+%(d)s FROM ip_observations_2026_10",
                {"d": timedelta(days)},
            )


def partition_indexes(database):
    """{partition: [(indexed columns, parent index, valid)]}."""
    found = {}
    with connect(database) as conn:
        for name, definition, parent, valid in conn.execute(PARTITION_INDEXES).fetchall():
            columns = definition.rsplit("(", 1)[1].rstrip(")")
            found.setdefault(name, []).append((columns, parent, valid))
    return found


def observations(database):
    with connect(database) as conn:
        return conn.execute("SELECT * FROM ip_observations ORDER BY 1,2,3").fetchall()


def test_v11_database_indexes_observations_by_time_then_ip(databases):
    database = databases()
    load_v11(database)
    with pytest.raises(ValueError, match="db-migrate"):
        initialize(database)
    partitions = [f"ip_observations_2026_{day}" for day in ("10", "11_01", "11_02")]
    old = [("ip_id, bucket_start", "ip_observations_ip_time", True)]
    assert partition_indexes(database) == dict.fromkeys(partitions, old)
    before = observations(database)
    assert len(before) == 3 * len([row for row in before if row[0].month == 10]) > 0
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (11, SCHEMA_VERSION)
    assert result["verified"] == [] and migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    assert observations(database) == before
    new = [("bucket_start, ip_id", "ip_observations_time_ip", True)]
    assert partition_indexes(database) == dict.fromkeys(partitions, new)
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)
    with connect(database) as conn:  # a partition created later gets the same index
        partition(conn, "ip_observations", datetime(2026, 11, 3, tzinfo=UTC))
        november = datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 3, tzinfo=UTC)
        distinct = conn.execute(SLICE, november).fetchone()[0]
    later = [*partitions, "ip_observations_2026_11_03"]
    assert partition_indexes(database) == dict.fromkeys(later, new)
    assert distinct == len({row[2] for row in before if row[0].month == 10})


def test_v12_database_is_refused_by_a_v11_build(databases, monkeypatch):
    """The v11 release's guards (its version, schema and migratable list) refuse a v12 database."""
    database = databases()
    load_v11(database)
    migrate(database)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 11)
    monkeypatch.setattr(storage, "_schema", lambda: (SCHEMA_V11.read_text(), V11_CHECKSUM))
    monkeypatch.delitem(storage.MIGRATABLE, (11, V11_CHECKSUM))
    with pytest.raises(ValueError, match="rebuild"):
        initialize(database)
    with pytest.raises(ValueError, match="unmodified"):
        migrate(database)
    current = hashlib.sha256((SOURCE / "schema.sql").read_text().encode()).hexdigest()
    with connect(database) as conn:
        assert conn.execute("SELECT version,checksum FROM schema_versions").fetchall() == [
            (SCHEMA_VERSION, current)
        ]


def scans(node):
    """(relation, scan type, index) of every scan node of an ip_observations partition."""
    relation = node.get("Relation Name", "")
    kind = node["Node Type"]
    if relation.startswith("ip_observations") and "Scan" in kind and kind != "Bitmap Heap Scan":
        yield relation, node["Node Type"], node.get("Index Name")
    if kind == "Bitmap Index Scan" and node["Index Name"].startswith("ip_observations"):
        yield None, node["Node Type"], node["Index Name"]
    for child in node.get("Plans", []):
        yield from scans(child)


@pytest.mark.asyncio
async def test_a_slice_counts_distinct_ips_on_the_time_ip_index(database, tmp_path):
    """Two daily partitions of 288 five-minute buckets x 100 IPs; a two-hour slice across
    midnight reads only (bucket_start, ip_id) of both, as the planner chooses it unforced."""
    ingest(await measurement(tmp_path / "run"), database)
    day = datetime(2026, 11, 1, tzinfo=UTC)
    with connect(database) as conn:
        run_key = conn.execute("SELECT run_key FROM test_runs").fetchone()[0]
        conn.execute(
            "INSERT INTO ip_addresses(ip) SELECT ('198.51.100.0'::inet + n) "
            "FROM generate_series(1,100) n ON CONFLICT DO NOTHING"
        )
        for offset in (0, 1):
            partition(conn, "ip_observations", day + timedelta(days=offset))
        conn.execute(
            "INSERT INTO ip_observations SELECT %(day)s+b*interval '5 minutes',%(run)s,a.ip_id,1,"
            "%(day)s+b*interval '5 minutes',%(day)s+b*interval '5 minutes' "
            "FROM generate_series(0,575) b CROSS JOIN ip_addresses a "
            "WHERE a.ip<<='198.51.100.0/24' AND (b+a.ip_id)%%3<>0",
            {"day": day, "run": run_key},
        )
    with connect(database) as conn:
        conn.autocommit = True
        conn.execute("VACUUM (ANALYZE) ip_observations_2026_11_01, ip_observations_2026_11_02")
    bounds = day + timedelta(hours=23), day + timedelta(hours=25)
    with connect(database) as conn:
        plan = conn.execute("EXPLAIN (FORMAT JSON) " + SLICE, bounds).fetchone()[0]
        distinct = conn.execute(SLICE, bounds).fetchone()[0]
    found = list(scans(plan[0]["Plan"]))
    indexes = {index for _, _, index in found}
    assert indexes == {
        "ip_observations_2026_11_01_bucket_start_ip_id_idx",
        "ip_observations_2026_11_02_bucket_start_ip_id_idx",
    }, json.dumps(plan)
    assert distinct == 100
