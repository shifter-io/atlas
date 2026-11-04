"""In-place v10 → v11 migration (the batch kind 'browser'), and a v10 build refusing v11.

fixtures/schema_v10.sql is the exact v10 schema (storage.V10_CHECKSUM); load_v10 builds the v10
release's database from test_migration's v9 database the way the v10 release migrated it.
"""

import hashlib
from pathlib import Path

import pytest
from test_migration import SOURCE, catalog, load_v9

from proxy_index import storage
from proxy_index.calculated import store
from proxy_index.storage import SCHEMA_VERSION, V10_CHECKSUM, connect, initialize, migrate

pytest_plugins = ["test_storage"]

SCHEMA_V10 = Path(__file__).parent / "fixtures/schema_v10.sql"
BROWSER_BATCH = (
    "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,start_at) "
    "VALUES ('browser','DE',1,'generic_http',now(),now()) RETURNING kind"
)


def load_v10(database):
    """The live shape: the v10 release's database (its exact schema), calculated v7, two batches."""
    load_v9(database)
    schema = SCHEMA_V10.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V10_CHECKSUM
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v9_v10.sql").read_text())
        conn.execute(schema)
        conn.execute("UPDATE schema_versions SET version=10,checksum=%s", (V10_CHECKSUM,))


def stored_rows(database):
    with connect(database) as conn:
        return [
            conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("test_batches", "test_runs", "run_buckets", "estimated_hours")
        ]


def test_v10_database_adds_the_browser_batch_kind(databases):
    database = databases()
    load_v10(database)
    with pytest.raises(ValueError, match="db-migrate"):
        initialize(database)
    with connect(database) as conn, pytest.raises(Exception, match="test_batches_kind_check"):
        conn.execute(BROWSER_BATCH)
    before = stored_rows(database)
    assert before[0] and before[1]
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (10, SCHEMA_VERSION)
    assert result["verified"] == [] and migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    assert stored_rows(database) == before
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)
    with connect(database) as conn:
        assert conn.execute(BROWSER_BATCH).fetchone() == ("browser",)
        with pytest.raises(Exception, match="test_batches_kind_check"):
            conn.execute(BROWSER_BATCH.replace("'browser'", "'other'"))


def test_v11_database_is_refused_by_a_v10_build(databases, monkeypatch):
    """The v10 release's guards (its version, schema and migratable list) refuse a v11 database."""
    database = databases()
    load_v10(database)
    migrate(database)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 10)
    monkeypatch.setattr(storage, "_schema", lambda: (SCHEMA_V10.read_text(), V10_CHECKSUM))
    monkeypatch.delitem(storage.MIGRATABLE, (10, V10_CHECKSUM))
    with pytest.raises(ValueError, match="rebuild"):
        initialize(database)
    with pytest.raises(ValueError, match="unmodified"):
        migrate(database)
    current = hashlib.sha256((SOURCE / "schema.sql").read_text().encode()).hexdigest()
    with connect(database) as conn:
        assert conn.execute("SELECT version,checksum FROM schema_versions").fetchall() == [
            (SCHEMA_VERSION, current)
        ]
