"""In-place v14 → v15 migration (three partial test_events indexes, built on every monthly
partition and attached), the catalog compared with a fresh v15, the partitions' indexes named as
a later partition's, and a v14 build refusing v15.

fixtures/schema_v14.sql is the exact v14 schema (storage.V14_CHECKSUM); load_v14 builds the v14
release's database from test_storage_migrate_v14's v13 database the way the v14 release migrated
it, with events in a second monthly partition.
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from migration_cases import SOURCE, catalog
from test_storage_migrate_v13 import EVENTS, product, rows
from test_storage_migrate_v14 import load_v13

from atlas import storage
from atlas.calculated import store
from atlas.partitions import partition
from atlas.storage import SCHEMA_VERSION, V14_CHECKSUM, connect, initialize, migrate

pytest_plugins = ["test_storage"]

SCHEMA_V14 = Path(__file__).parent / "fixtures/schema_v14.sql"
DECEMBER = datetime(2026, 12, 2, 8, 0, tzinfo=UTC)
JANUARY = datetime(2027, 1, 5, tzinfo=UTC)
INDEXES = {
    "test_events_notable": "at_event_id_idx",
    "test_events_country": "country_code_at_idx",
    "test_events_target": "target_id_at_idx",
}
# The partition indexes attached to the three, and whether each of the three is valid.
ATTACHED = (
    "SELECT inhparent::regclass::text,inhrelid::regclass::text FROM pg_inherits "
    "WHERE inhparent::regclass::text = ANY(%s) ORDER BY 1,2"
)
VALID = "SELECT bool_and(indisvalid) FROM pg_index WHERE indexrelid::regclass::text = ANY(%s)"


def load_v14(database):
    """The live shape: the v14 release's database (its exact schema), its events in November's
    partition and 20 more in December's."""
    load_v13(database)
    schema = SCHEMA_V14.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V14_CHECKSUM
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v13_v14.sql").read_text())
        conn.execute(schema)
        conn.execute("UPDATE schema_versions SET version=14,checksum=%s", (V14_CHECKSUM,))
        partition(conn, "test_events", DECEMBER)
        conn.execute(EVENTS, {"at": DECEMBER, "product": product(conn), "n": 20})


def indexes(database, query):
    """query's rows for the three indexes' names."""
    with connect(database) as conn:
        return conn.execute(query, (list(INDEXES),)).fetchall()


def attached(months):
    """The expected attachments: each index's partition index in each month's partition."""
    pairs = [(n, f"test_events_{m}_{suffix}") for n, suffix in INDEXES.items() for m in months]
    return sorted(pairs)


def test_v14_database_indexes_every_event_partition(databases):
    database = databases()
    load_v14(database)
    events = rows(database, "SELECT * FROM test_events ORDER BY event_id")
    with pytest.raises(ValueError, match="db-migrate"):
        initialize(database)
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (14, SCHEMA_VERSION)
    assert result["verified"] == [] and migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    assert indexes(database, ATTACHED) == attached(["2026_11", "2026_12"])
    assert indexes(database, VALID) == [(True,)]
    assert rows(database, "SELECT * FROM test_events ORDER BY event_id") == events
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)
    # A partition made after the migration gets the same indexes, named the same way.
    with connect(database) as conn:
        partition(conn, "test_events", JANUARY)
    assert indexes(database, ATTACHED) == attached(["2026_11", "2026_12", "2027_01"])


def test_v15_database_is_refused_by_a_v14_build(databases, monkeypatch):
    """The v14 release's guards (its version, schema and migratable list) refuse a v15 database."""
    database = databases()
    load_v14(database)
    migrate(database)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 14)
    monkeypatch.setattr(storage, "_schema", lambda: (SCHEMA_V14.read_text(), V14_CHECKSUM))
    monkeypatch.delitem(storage.MIGRATABLE, (14, V14_CHECKSUM))
    with pytest.raises(ValueError, match="rebuild"):
        initialize(database)
    with pytest.raises(ValueError, match="unmodified"):
        migrate(database)
