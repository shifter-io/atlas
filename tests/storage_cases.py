"""Shared fixtures and helpers of the storage tests: the isolated PostgreSQL databases (each a
copy of this worker's initialised template), a measured run (`measurement`) and its DB-IP
enrichment (`enrichment`). test_storage.py holds ingest and history, test_storage_schema.py
schema versions and migrations, test_storage_columns.py typed columns and cached metrics.

Most test modules load the fixtures with `pytest_plugins = ["test_storage"]`, which loads this
module in turn; test_storage keeps `measurement`, `enrichment` and `ALL_ATTRIBUTES` importable.
The test modules import its names before pytest loads it, so pytest cannot rewrite it
(PYTEST_DONT_REWRITE).
"""

import gzip
import json
import os

import pytest
from helpers import FakeTransport, drop_databases, scratch_database

from proxy_index import JsonSink, ProxyConfig, RunConfig, RunOptions, run
from proxy_index.providers.credentials import read_credentials
from proxy_index.storage import initialize


def admin_credentials():
    path = os.environ.get("PG_TEST_ADMIN_FILE")
    if not path:
        pytest.skip("Set PG_TEST_ADMIN_FILE for isolated real PostgreSQL integration tests")
    return read_credentials(path, "postgres")


@pytest.fixture(scope="session")
def database_template(tmp_path_factory):
    """This worker's initialised schema; each `database` is a copy of it."""
    admin = admin_credentials()
    name = scratch_database(admin)
    try:
        initialize(credentials_file(admin, name, tmp_path_factory.mktemp("template")))
        yield name
    finally:
        drop_databases(admin, [name])


def credentials_file(admin, name, directory):
    config = directory / f"{name}.toml"
    config.write_text(
        "[postgres]\n"
        + "\n".join(
            f"{key} = {json.dumps(value)}" for key, value in (admin | {"dbname": name}).items()
        )
    )
    config.chmod(0o600)
    return config


@pytest.fixture
def databases(tmp_path):
    admin = admin_credentials()
    names = []

    def create(template=None):
        names.append(scratch_database(admin, template=template))
        return credentials_file(admin, names[-1], tmp_path)

    try:
        yield create
    finally:
        drop_databases(admin, names)


@pytest.fixture
def database(databases, database_template):
    return databases(template=database_template)


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
    transport_factory=FakeTransport,
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
        RunOptions(transport_factory=transport_factory),
    )
    return sink.directory


def enrichment(directory, *, invalid_ip=False, attributes=None, release="a" * 64):
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
