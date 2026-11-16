"""In-place v19 → v20 migration (an outage may end 'not_measured', OUTAGE-NOT-MEASURED-1): every
stored outage kept, the new reason refused by v19 and accepted by v20, the catalog compared with a
fresh v20, and a v19 build refusing v20.

fixtures/schema_v19.sql is the exact v19 schema (storage.V19_CHECKSUM); load_v19 builds the v19
release's database from test_storage_migrate_v19's v18 database the way the v19 release migrated
it.
"""

import hashlib
from pathlib import Path

import psycopg
import pytest
from migration_cases import SOURCE, catalog
from test_storage_migrate_v13 import rows
from test_storage_migrate_v19 import load_v18

from atlas import storage
from atlas.calculated import store
from atlas.storage import SCHEMA_VERSION, V19_CHECKSUM, connect, initialize, migrate

pytest_plugins = ["test_storage"]

SCHEMA_V19 = Path(__file__).parent / "fixtures/schema_v19.sql"
OUTAGES = "SELECT * FROM outages ORDER BY outage_id"
NOT_MEASURED = "UPDATE outages SET end_reason='not_measured' WHERE end_reason IS NULL"


def load_v19(database):
    """The v19 release's database (its exact schema), with an open and a too_few_ips outage."""
    load_v18(database)
    schema = SCHEMA_V19.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V19_CHECKSUM
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v18_v19.sql").read_text())
        conn.execute(schema)
        conn.execute("UPDATE schema_versions SET version=19,checksum=%s", (V19_CHECKSUM,))
        conn.execute("INSERT INTO providers VALUES ('unmeasured') ON CONFLICT DO NOTHING")
        product = conn.execute(
            "INSERT INTO products(provider,name) VALUES ('unmeasured','residential') "
            "RETURNING product_id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO outages(product_id,country_code,source,started_at,ended_at,end_reason) "
            "VALUES (%s,'DE','pulse','2026-11-16 17:50Z',NULL,NULL),"
            "(%s,'DE','pulse','2026-11-16 16:00Z','2026-11-16 16:05Z','too_few_ips')",
            (product, product),
        )


def test_v19_database_lets_an_outage_end_not_measured(databases):
    database = databases()
    load_v19(database)
    with connect(database) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(NOT_MEASURED)
    outages = rows(database, OUTAGES)
    with pytest.raises(ValueError, match="db-migrate"):
        initialize(database)
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (19, SCHEMA_VERSION)
    assert result["verified"] == [] and migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    assert rows(database, OUTAGES) == outages
    with connect(database) as conn:
        assert conn.execute(NOT_MEASURED).rowcount == 1
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)


def test_v20_database_is_refused_by_a_v19_build(databases, monkeypatch):
    """The v19 release's guards (its version, schema and migratable list) refuse a v20 database."""
    database = databases()
    load_v19(database)
    migrate(database)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 19)
    monkeypatch.setattr(storage, "_schema", lambda: (SCHEMA_V19.read_text(), V19_CHECKSUM))
    monkeypatch.delitem(storage.MIGRATABLE, (19, V19_CHECKSUM))
    with pytest.raises(ValueError, match="rebuild"):
        initialize(database)
    with pytest.raises(ValueError, match="unmodified"):
        migrate(database)
