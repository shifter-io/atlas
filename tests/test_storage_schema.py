"""Storage schema versions: refusing an incompatible schema, the v2 to v3 migration and the
controlled rebuild from v3."""

import hashlib
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb
from storage_cases import ALL_ATTRIBUTES

from atlas.storage import connect, initialize, migrate

pytest_plugins = ["storage_cases"]


def test_initialize_refuses_incompatible_schema(database, databases):
    assert migrate(database)["migrated"] is False
    with pytest.raises(ValueError, match="db-init"):
        migrate(databases())
    with connect(database) as conn:
        conn.execute("UPDATE schema_versions SET version=1")
    with pytest.raises(ValueError, match="rebuild"):
        initialize(database)
    with connect(database) as conn:
        assert conn.execute("SELECT version FROM schema_versions").fetchone()[0] == 1


@pytest.mark.parametrize("invalid", [False, True])
def test_v2_migration_preserves_every_field_and_is_atomic(database, invalid):
    old = Path(__file__).parent / "fixtures/schema_v2.sql"
    attributes = ALL_ATTRIBUTES | ({"matched": "invalid-boolean"} if invalid else {})
    with connect(database) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        conn.execute(old.read_text())
        conn.execute(
            "INSERT INTO schema_versions(version,checksum) VALUES (2,%s)",
            (hashlib.sha256(old.read_bytes()).hexdigest(),),
        )
        ip_id = conn.execute(
            "INSERT INTO ip_addresses(ip) VALUES ('8.8.8.8') RETURNING ip_id"
        ).fetchone()[0]
        release_id = conn.execute(
            "INSERT INTO dbip_releases(sha256,metadata) VALUES (%s,'{}') RETURNING release_id",
            ("a" * 64,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO ip_enrichment(ip_id,release_id,attributes) VALUES (%s,%s,%s)",
            (ip_id, release_id, Jsonb(attributes)),
        )
    if invalid:
        with pytest.raises(psycopg.errors.InvalidTextRepresentation):
            with connect(database) as conn:
                conn.execute(
                    (Path(__file__).parents[1] / "src/atlas/migrate_v2_v3.sql").read_text()
                )
        with connect(database) as conn:
            assert conn.execute("SELECT version FROM schema_versions").fetchone()[0] == 2
            assert conn.execute("SELECT attributes FROM ip_enrichment").fetchone()[0] == attributes
    else:
        with connect(database) as conn:
            conn.execute(
                (Path(__file__).parents[1] / "src/atlas/migrate_v2_v3.sql").read_text()
            )
        with connect(database) as conn:
            row = conn.execute(
                "SELECT to_jsonb(e) - 'ip_id' - 'release_id' FROM ip_enrichment e"
            ).fetchone()[0]
            extra = row.pop("extra_attributes")
            assert row | extra == attributes
            assert conn.execute(
                "SELECT connection_type,usage_type FROM ip_enrichment_details"
            ).fetchone() == ("Cable/DSL", "residential")


def test_v3_requires_controlled_rebuild(database):
    old = Path(__file__).parent / "fixtures/schema_v3.sql"
    with connect(database) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        conn.execute(old.read_text())
        conn.execute(
            "INSERT INTO schema_versions(version,checksum) VALUES (3,%s)",
            (hashlib.sha256(old.read_bytes()).hexdigest(),),
        )
    with pytest.raises(ValueError, match="rebuild"):
        initialize(database)
