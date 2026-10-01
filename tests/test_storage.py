import gzip
import hashlib
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from helpers import FakeTransport
from psycopg import sql
from psycopg.types.json import Jsonb

from milf_framework import JsonSink, ProxyConfig, RunConfig, run
from milf_framework.cli import main
from milf_framework.providers.credentials import read_credentials
from milf_framework.storage import (
    ENRICHMENT_FIELDS,
    connect,
    ingest,
    initialize,
    read_summary,
    status,
)


@pytest.fixture
def database(tmp_path):
    path = os.environ.get("PG_TEST_ADMIN_FILE")
    if not path:
        pytest.skip("Set PG_TEST_ADMIN_FILE for isolated real PostgreSQL integration tests")
    admin = read_credentials(path, "postgres")
    name = "proxy_test_" + uuid.uuid4().hex
    with psycopg.connect(**admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    config = tmp_path / "database.toml"
    config.write_text(
        "[postgres]\n"
        + "\n".join(
            f"{key} = {json.dumps(value)}" for key, value in (admin | {"dbname": name}).items()
        )
    )
    config.chmod(0o600)
    initialize(config)
    try:
        yield config
    finally:
        with psycopg.connect(**admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def measurement(
    tmp_path,
    *,
    requests=3,
    target="generic_http",
    provider="fixture",
    product="residential",
    concurrency=100,
    timeout_seconds=15,
    requested_country=None,
):
    sink = JsonSink(tmp_path / "results")
    await run(
        RunConfig(
            target,
            ProxyConfig("http", "unused.invalid", 8080),
            requests=requests,
            concurrency=concurrency,
            timeout_seconds=timeout_seconds,
            requested_country=requested_country,
            provider=provider,
            product=product,
        ),
        sink,
        transport_factory=FakeTransport,
    )
    return sink.directory


def enrichment(directory, *, invalid_ip=False, attributes=None):
    release = "a" * 64
    with gzip.open(directory / "ips.jsonl.gz", "rt") as stream:
        rows = [json.loads(line) for line in stream]
    output = directory / f"enriched-{release}.jsonl.gz"
    with gzip.open(output, "wt") as stream:
        for row in rows:
            if invalid_ip:
                row["ip"] = "1.1.1.1"
            stream.write(
                json.dumps(
                    row
                    | {
                        "dbip_release": release,
                        "enrichment": attributes
                        if attributes is not None
                        else {"country_code": "US", "asn": 15169, "isp": "Fixture"},
                    }
                )
                + "\n"
            )
    manifest = {
        "run_id": rows[0]["run_id"],
        "release": {"sha256": release},
        "rows": len(rows),
        "file": output.name,
    }
    (directory / f"enrichment-{release}.json").write_text(json.dumps(manifest))


async def test_import_history_cache_enrichment_and_replay(database, tmp_path):
    first = await measurement(tmp_path)
    enrichment(first)
    assert ingest(first, database)["imported"]
    assert not ingest(first, database)["imported"]
    second = await measurement(tmp_path, requests=2)
    assert ingest(second, database)["imported"]
    saved = status(database)
    assert saved["counts"]["test_runs"] == 2
    assert saved["counts"]["ip_addresses"] == saved["counts"]["ip_enrichment"] == 1
    metrics = saved["cached_scopes"][0]["metrics"]
    assert metrics["runs"] == 2 and metrics["completed"] == metrics["successes"] == 5
    assert metrics["unique_ips"] == 1 and metrics["latency_ms"]["p99"] == 11
    with connect(database) as conn:
        assert conn.execute("SELECT country_counts FROM run_enrichments").fetchone()[0] == {
            "US": {"requests": 3, "unique_ips": 1}
        }
        assert (
            conn.execute(
                "SELECT count(*) FROM pg_inherits WHERE inhparent='ip_observations'::regclass"
            ).fetchone()[0]
            == 1
        )


async def test_enrichment_can_be_added_after_measurement(database, tmp_path):
    directory = await measurement(tmp_path)
    ingest(directory, database)
    enrichment(directory)
    result = ingest(directory, database)
    assert not result["imported"] and result["enrichment_releases_added"] == 1
    assert status(database)["cached_scopes"][0]["metrics"]["runs"] == 1


async def test_concurrent_duplicate_imports_are_atomic(database, tmp_path):
    directory = await measurement(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ingest(directory, database), range(2)))
    assert sum(result["imported"] for result in results) == 1
    assert status(database)["cached_scopes"][0]["metrics"]["completed"] == 3


async def test_bad_enrichment_rolls_back_history_and_cache(database, tmp_path):
    directory = await measurement(tmp_path)
    enrichment(directory, invalid_ip=True)
    with pytest.raises(ValueError, match="match IP"):
        ingest(directory, database)
    saved = status(database)
    assert all(value == 0 for value in saved["counts"].values())


async def test_changed_run_and_corrupt_artifact_are_rejected(database, tmp_path):
    directory = await measurement(tmp_path)
    ingest(directory, database)
    path = directory / "summary.json"
    original = path.read_text()
    path.write_text(original + "\n")
    with pytest.raises(ValueError, match="different content"):
        ingest(directory, database)
    path.write_text(original)
    with (directory / "ips.jsonl.gz").open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        ingest(directory, database)
    assert status(database)["counts"]["test_runs"] == 1


async def test_https_has_no_ip_rows(database, tmp_path):
    directory = await measurement(tmp_path, target="generic_https")
    ingest(directory, database)
    saved = status(database)
    assert saved["counts"]["ip_addresses"] == saved["counts"]["ip_observations"] == 0
    assert saved["cached_scopes"][0]["metrics"]["unique_ips"] is None


async def test_unreconciled_ip_counts_roll_back(database, tmp_path):
    directory = await measurement(tmp_path)
    source = directory / "ips.jsonl.gz"
    with gzip.open(source, "rt") as stream:
        row = json.loads(stream.readline())
    row["count"] += 1
    with gzip.open(source, "wt") as stream:
        stream.write(json.dumps(row) + "\n")
    summary_path = directory / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary["artifacts"]["ips"]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    summary_path.write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="reconcile"):
        ingest(directory, database)
    assert status(database)["counts"]["test_runs"] == 0


@pytest.mark.parametrize("available", [True, False])
def test_cli_automatic_ingest_and_durable_fallback(
    database,
    tmp_path,
    monkeypatch,
    capsys,
    available,
):
    async def simulated_run(config, sink, **kwargs):
        return await run(config, sink, transport_factory=FakeTransport, **kwargs)

    monkeypatch.setattr("milf_framework.cli.run", simulated_run)
    monkeypatch.setenv("PROXY_URL", "http://unused.invalid:8080")
    credentials = database if available else tmp_path / "missing.toml"
    output = tmp_path / "saved"
    code = main(
        [
            "run",
            "--target",
            "generic_http",
            "--provider",
            "fixture",
            "--product",
            "residential",
            "--requests",
            "3",
            "--output",
            str(output),
            "--database-file",
            str(credentials),
        ]
    )
    assert code == (0 if available else 2)
    summaries = list(output.glob("*/summary.json"))
    assert len(summaries) == 1 and json.loads(summaries[0].read_text())["successes"] == 3
    assert status(database)["counts"]["test_runs"] == int(available)
    assert "Results:" in capsys.readouterr().err


async def test_shared_ip_memberships_and_overlap_are_deduplicated(database, tmp_path):
    for provider, product in [
        ("alpha", "residential"),
        ("beta", "residential"),
        ("alpha", "mobile"),
        ("beta", "mobile"),
        ("gamma", "residential"),
        ("alpha", "residential"),
    ]:
        directory = await measurement(tmp_path, provider=provider, product=product)
        enrichment(directory)
        ingest(directory, database)
        assert not ingest(directory, database)["imported"]
    saved = status(database)
    assert saved["counts"]["ip_addresses"] == saved["counts"]["ip_enrichment"] == 1
    assert saved["counts"]["product_ips"] == 5
    assert saved["counts"]["ip_observations"] == 6
    assert saved["provider_overlaps"] == [
        {"provider_a": a, "provider_b": b, "shared_ips": 1, "window": "lifetime"}
        for a, b in [("alpha", "beta"), ("alpha", "gamma"), ("beta", "gamma")]
    ]
    with connect(database) as conn:
        row = conn.execute(
            "SELECT ip,observation_count,run_count,first_seen<=last_seen "
            "FROM product_ip_details WHERE provider='alpha' "
            "AND product='residential'"
        ).fetchone()
        assert str(row[0]) == "8.8.8.8" and row[1:] == (6, 2, True)
        assert conn.execute("SELECT isp,asn FROM ip_enrichment_details").fetchone() == (
            "Fixture",
            15169,
        )
        assert conn.execute("SELECT count(*) FROM test_run_details").fetchone()[0] == 6


def test_initialize_refuses_incompatible_schema(database):
    with connect(database) as conn:
        conn.execute("UPDATE schema_versions SET version=1")
    with pytest.raises(ValueError, match="rebuild"):
        initialize(database)
    with connect(database) as conn:
        assert conn.execute("SELECT version FROM schema_versions").fetchone()[0] == 1


ALL_ATTRIBUTES = {
    "matched": True,
    "country_code": "US",
    "country": "United States",
    "region_code": "CA",
    "region": "California",
    "city": "Los Angeles",
    "latitude": 34.05,
    "longitude": -118.24,
    "isp": "Fixture",
    "organization": "Customer",
    "asn": 15169,
    "asn_organization": "Network",
    "connection_type": "Cable/DSL",
    "usage_type": "residential",
    "future_field": {"nested": [True, 3, None]},
    "future_null": None,
}


async def test_all_enrichment_fields_and_unknowns_round_trip(database, tmp_path):
    for _ in range(2):
        directory = await measurement(tmp_path)
        enrichment(directory, attributes=ALL_ATTRIBUTES)
        ingest(directory, database)
        assert not ingest(directory, database)["imported"]
    with connect(database) as conn:
        row = conn.execute(
            "SELECT to_jsonb(e) - 'ip_id' - 'release_id' FROM ip_enrichment_fields e"
        ).fetchone()[0]
        extra = row.pop("extra_attributes")
        assert row | extra == ALL_ATTRIBUTES
        assert not set(ENRICHMENT_FIELDS) & extra.keys()
    directory = await measurement(tmp_path)
    enrichment(directory, attributes=ALL_ATTRIBUTES | {"connection_type": "Cellular"})
    with pytest.raises(ValueError, match="conflicts"):
        ingest(directory, database)
    assert status(database)["counts"]["test_runs"] == 2


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
                    (
                        Path(__file__).parents[1] / "src/milf_framework/migrate_v2_v3.sql"
                    ).read_text()
                )
        with connect(database) as conn:
            assert conn.execute("SELECT version FROM schema_versions").fetchone()[0] == 2
            assert conn.execute("SELECT attributes FROM ip_enrichment").fetchone()[0] == attributes
    else:
        with connect(database) as conn:
            conn.execute(
                (Path(__file__).parents[1] / "src/milf_framework/migrate_v2_v3.sql").read_text()
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


async def test_complete_summary_reconstructed_and_numeric_keys(database, tmp_path):
    directory = await measurement(tmp_path)
    original = json.loads((directory / "summary.json").read_text())
    ingest(directory, database)
    assert read_summary(original["run_id"], database) == original
    with connect(database) as conn:
        assert conn.execute("SELECT summary FROM test_run_details").fetchone()[0] == original
        compact = conn.execute("SELECT summary FROM test_runs").fetchone()[0]
        assert not {"buckets", "latency_histogram", "config", "target"} & compact.keys()
        assert (
            conn.execute("SELECT pg_typeof(run_key)::text FROM ip_observations").fetchone()[0]
            == "bigint"
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM ip_observations o JOIN test_runs r USING(run_key)"
            ).fetchone()[0]
            == 1
        )
    with pytest.raises(KeyError):
        read_summary(str(uuid.uuid4()), database)


async def test_pool_membership_shared_but_scopes_and_geography_distinct(database, tmp_path):
    for concurrency, timeout, country in [
        (2, 10, "us"),
        (4, 20, "us"),
        (2, 10, "us"),
        (2, 10, "ca"),
    ]:
        directory = await measurement(
            tmp_path, concurrency=concurrency, timeout_seconds=timeout, requested_country=country
        )
        ingest(directory, database)
    saved = status(database)
    assert len(saved["cached_scopes"]) == 3
    assert sorted(s["metrics"]["runs"] for s in saved["cached_scopes"]) == [1, 1, 2]
    assert all(s["metrics"]["unique_ips"] == 1 for s in saved["cached_scopes"])
    assert len(saved["cached_pools"]) == 2
    assert all(s["unique_ips"] == 1 for s in saved["cached_pools"])
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM pool_ips").fetchone()[0] == 2
        assert conn.execute("SELECT to_regclass('scope_ips')").fetchone()[0] is None


async def test_dimension_nulls_and_distinct_tuples(database, tmp_path):
    first = await measurement(tmp_path)
    enrichment(first, attributes={"matched": False})
    ingest(first, database)
    second = await measurement(tmp_path)
    enrichment(second, attributes={"matched": False})
    ingest(second, database)
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM enrichment_locations").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM enrichment_networks").fetchone()[0] == 1
        assert conn.execute("SELECT extra_attributes FROM ip_enrichment").fetchone()[0] is None


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


async def test_bucket_histogram_mismatch_rejected(database, tmp_path):
    directory = await measurement(tmp_path)
    path = directory / "summary.json"
    summary = json.loads(path.read_text())
    summary["buckets"][0]["latency_histogram"]["bins"][0][0] += 1
    path.write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="histograms differ"):
        ingest(directory, database)
    assert status(database)["counts"]["test_runs"] == 0


async def test_dimensions_keep_same_city_and_asn_with_different_details(database, tmp_path):
    from milf_framework.models import Outcome

    class SecondIP(FakeTransport):
        async def execute(self, target, *, context=None):
            return Outcome(10.1, ip="2001:4860:4860::8888", status=200, response_body_bytes=20)

    first = await measurement(tmp_path)
    enrichment(first, attributes=ALL_ATTRIBUTES)
    ingest(first, database)
    sink = JsonSink(tmp_path / "results")
    await run(
        RunConfig(
            "generic_http",
            ProxyConfig("http", "unused.invalid", 8080),
            requests=2,
            concurrency=100,
            timeout_seconds=15,
            provider="fixture",
            product="residential",
        ),
        sink,
        transport_factory=SecondIP,
    )
    enrichment(
        sink.directory,
        attributes=ALL_ATTRIBUTES
        | {"latitude": 34.15, "isp": "Other ISP", "connection_type": "Cellular"},
    )
    ingest(sink.directory, database)
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM enrichment_locations").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM enrichment_networks").fetchone()[0] == 2
        assert conn.execute(
            "SELECT count(DISTINCT city),count(DISTINCT asn) FROM ip_enrichment_fields"
        ).fetchone() == (1, 1)
        assert (
            conn.execute(
                "SELECT count(*) FROM ip_enrichment_details WHERE connection_type='Cellular'"
            ).fetchone()[0]
            == 1
        )
    assert status(database)["cached_scopes"][0]["metrics"]["unique_ips"] == 2


async def test_multiple_buckets_reconstruct_and_count_one_product_run(database, tmp_path):
    from copy import deepcopy
    from datetime import datetime, timedelta

    directory = await measurement(tmp_path, requests=2)
    path = directory / "summary.json"
    summary = json.loads(path.read_text())
    source = directory / "ips.jsonl.gz"
    with gzip.open(source, "rt") as stream:
        row = json.loads(stream.readline())
    row["count"] = 1
    later = deepcopy(row)
    for key in ("bucket_start", "first_seen", "last_seen"):
        later[key] = (datetime.fromisoformat(row[key]) + timedelta(minutes=5)).isoformat()
    with gzip.open(source, "wt") as stream:
        stream.write(json.dumps(row) + "\n" + json.dumps(later) + "\n")
    bucket = summary["buckets"][0]
    bucket["completed"] = bucket["successes"] = 1
    bucket["status_codes"] = {"200": 1}
    bucket["response_body_bytes"] //= 2
    bucket["latency_histogram"]["bins"][0][1] = 1
    later_bucket = deepcopy(bucket)
    later_bucket["bucket_start"] = later["bucket_start"]
    summary["buckets"].append(later_bucket)
    summary["finished_at"] = (
        datetime.fromisoformat(summary["finished_at"]) + timedelta(minutes=5)
    ).isoformat()
    summary["duration_seconds"] += 300
    summary["artifacts"]["ips"]["rows"] = 2
    summary["artifacts"]["ips"]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    path.write_text(json.dumps(summary))
    ingest(directory, database)
    assert read_summary(summary["run_id"], database) == summary
    with connect(database) as conn:
        assert conn.execute("SELECT observation_count,run_count FROM product_ips").fetchone() == (
            2,
            1,
        )
        assert conn.execute("SELECT count(*) FROM ip_observations").fetchone()[0] == 2
    assert status(database)["cached_scopes"][0]["metrics"]["unique_ips"] == 1
