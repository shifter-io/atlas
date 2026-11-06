"""SNAP-SIZE-3a: a build stored whole before calculated v8 (one payload row) still reads,
publishes and serves like a build of head and parts, and the v7 layer's builds migrate unchanged.
"""

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from migration_cases import catalog
from psycopg.types.json import Jsonb
from test_calculated_parts import POLICY, two_providers, whole_payload_build, without

from proxy_index.calculated import parts, store, store_retention
from proxy_index.calculated.api import RESOURCES, app
from proxy_index.calculated.calculator import calculate
from proxy_index.storage import connect, migrate

pytest_plugins = ["test_storage", "calculated_template"]


@pytest.fixture
def database(calculated_database):
    """Every test here works on the current calculated schema, copied from its template."""
    return calculated_database


@pytest.mark.asyncio
async def test_a_build_stored_whole_before_v8_still_reads_publishes_and_serves(
    database, tmp_path, monkeypatch
):
    # Both builds stay after the second publication (retention has its own tests).
    monkeypatch.setattr(store_retention, "RETAIN_PER_DAY", 100)
    end = await two_providers(database, tmp_path)
    payload = calculate(database, POLICY, as_of=end, publish=True)
    with connect(database) as conn:
        split = parts.previous(conn)
    legacy = whole_payload_build(database, payload)
    expected = payload | {"snapshot_id": str(legacy)}
    assert store.read(database, str(legacy)) == expected
    assert store.read(database, str(legacy), assemble=False) == expected
    assert store.read_head(database, str(legacy)) == {
        k: v for k, v in expected.items() if k not in ("products", "runs")
    }
    assert store.read_parts(database, str(legacy)) == payload["products"]
    assert store.read_parts(database, str(legacy), "runs") == payload["runs"]
    one = payload["products"][0]
    assert store.read_parts(database, str(legacy), product=str(one["product_id"])) == [one]
    assert store.read_parts(database, str(legacy), keys=("id",)) == [
        {"id": p["id"]} for p in payload["products"]
    ]
    store.publish(database, str(legacy))
    with connect(database) as conn:
        whole = parts.previous(conn)
    # The next calculation reads the same projection from either layout: ids and scores only.
    assert whole == split
    window = whole["products"][0]["windows"]["24h"]
    assert set(window) == {"scores", "targets"}
    assert all(set(t) == {"score"} for t in window["targets"].values())
    async with TestClient(TestServer(app(database))) as client:
        for path in ["", *(f"/{r}?window=7d" for r in sorted(RESOURCES))]:
            new = await client.get(f"/v1/snapshots/{payload['snapshot_id']}{path}")
            old = await client.get(f"/v1/snapshots/{legacy}{path}")
            assert (new.status, old.status) == (200, 200)
            assert without(await new.json()) == without(await old.json()), path


CALCULATED_V7 = Path(__file__).parent / "fixtures/calculated_v7.sql"


def v7_layer(database, payloads):
    """A current database (a copy of the history template) given the v7 release's calculated
    layer, holding payloads as v7 stored them (one row each); the last one is current. Returns
    their build ids."""
    assert hashlib.sha256(CALCULATED_V7.read_bytes()).hexdigest() == store.V7_CHECKSUM
    with connect(database) as conn:
        conn.execute(CALCULATED_V7.read_text())
        conn.execute("INSERT INTO calculation_schema VALUES (7,%s)", (store.V7_CHECKSUM,))
        for payload in payloads:
            method = payload["methodology"]
            conn.execute(
                "INSERT INTO methodologies VALUES (%s,%s,now()) ON CONFLICT DO NOTHING",
                (method["id"], Jsonb(method["settings"])),
            )
    builds = [whole_payload_build(database, payload) for payload in payloads]
    with connect(database) as conn:
        if builds:
            conn.execute("INSERT INTO current_calculation VALUES (true,%s)", (builds[-1],))
        conn.execute("UPDATE calculation_builds SET published=true")
    return builds


def snapshots(database):
    with connect(database) as conn:
        return conn.execute(
            "SELECT build_id,content_hash,payload::text FROM calculated_snapshots ORDER BY 1"
        ).fetchall()


@pytest.mark.asyncio
async def test_calculated_v7_builds_migrate_unchanged_and_still_serve(
    calculated_database, databases, database_template, calculated_template, tmp_path
):
    source = calculated_database
    end = await two_providers(source, tmp_path)
    payloads = [calculate(source, POLICY, as_of=end + timedelta(hours=h)) for h in (0, 1)]
    database = databases(template=database_template)
    builds = v7_layer(database, payloads)
    before = snapshots(database)
    result = migrate(database)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        7,
        12,
    )
    assert result["verified"] == [
        *store.V7_CHECKS,
        *store.V8_CHECKS,
        *store.V9_CHECKS,
        *store.V10_CHECKS,
        *store.V11_CHECKS,
    ]
    assert snapshots(database) == before
    for build_id, payload in zip(builds, payloads, strict=True):
        assert store.read(database, str(build_id)) == payload | {"snapshot_id": str(build_id)}
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM calculated_snapshot_parts").fetchone() == (0,)
    async with TestClient(TestServer(app(database))) as client:
        for path in ["", *(f"/{r}?window=24h" for r in sorted(RESOURCES))]:
            assert (await client.get(f"/v1/snapshots/{builds[0]}{path}")).status == 200
    assert migrate(database)["migrated"] is False
    store.initialize(database)
    reference = databases(template=calculated_template)
    assert catalog(database) == catalog(reference)


def test_a_failed_v7_check_rolls_back(databases, database_template, monkeypatch):
    database = databases(template=database_template)
    v7_layer(database, [])
    monkeypatch.setattr(store, "V7_CHECKS", {"always": "SELECT 1"})
    with pytest.raises(ValueError, match="always"):
        migrate(database)
    with connect(database) as conn:
        assert conn.execute("SELECT version FROM calculation_schema").fetchall() == [(7,)]
        assert conn.execute("SELECT to_regclass('calculated_snapshot_parts')").fetchone() == (None,)
