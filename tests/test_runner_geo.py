"""The runner's geo artifact: rows per bucket, location and network.

The shared run configuration is in `runner_cases.py`.
"""

import asyncio
import gzip
import hashlib
import json

import pytest
from helpers import FakeTransport
from runner_cases import config

from proxy_index import JsonSink, RunOptions, run
from proxy_index.models import Outcome
from proxy_index.stats import LOCATION_FIELDS, NETWORK_FIELDS


class Enricher:
    release = {
        "file": "fixture.mmdb",
        "sha256": "ab" * 32,
        "database_type": "DBIP-Full",
        "build_epoch": 1000,
        "built_at": "1970-01-01T00:16:40+00:00",
    }

    def __init__(self):
        self.lookups = 0

    def lookup(self, address):
        self.lookups += 1
        last = int(address.rsplit(".", 1)[1])
        if last == 99:
            return {"matched": False, **dict.fromkeys((*LOCATION_FIELDS, *NETWORK_FIELDS))}
        german = last % 2 == 0
        return {
            "matched": True,
            "country_code": "DE" if german else "FR",
            "country": "Germany" if german else "France",
            "region_code": None,
            "region": None,
            "city": "Berlin" if german else "Paris",
            "latitude": 52.5 if german else 48.9,
            "longitude": 13.4 if german else 2.35,
            "isp": "Telekom" if german else "Orange",
            "organization": None,
            "asn": 3320 if german else 3215,
            "asn_organization": "DTAG" if german else "Orange",
            "connection_type": "Cable/DSL",
            "usage_type": "residential",
        }


async def test_geo_rows_per_bucket_location_and_network(tmp_path, monkeypatch):
    import proxy_index.runner as runner

    clock = [0.0]
    monkeypatch.setattr(runner.time, "time", lambda: 290.0)
    monkeypatch.setattr(runner.time, "perf_counter", lambda: clock[0])

    class Script(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            # Requests 1-6 land in the 0:00 bucket, 7-12 in 5:00.
            clock[0] = 0 if self.count <= 6 else 20
            ip = ["192.0.2.2", "192.0.2.4", "192.0.2.3", "192.0.2.99", None, None][
                (self.count - 1) % 6
            ]
            if ip is None:
                return Outcome(9, error="timeout" if self.count % 2 else "challenge", bytes_up=5)
            return Outcome(self.count, ip=ip, status=200, bytes_up=10, bytes_down=30)

    enricher = Enricher()
    sink = JsonSink(tmp_path)
    result = await run(
        config(requests=12, concurrency=1),
        sink,
        RunOptions(transport_factory=Script, enricher=enricher),
    )
    artifact = result.artifacts["geo"]
    path = sink.directory / artifact["file"]
    assert artifact["file"] == "geo.jsonl.gz" and artifact["release"] == Enricher.release
    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    with gzip.open(path, "rt") as stream:
        rows = [json.loads(line) for line in stream]
    # (DE, FR, unknown) x 2 buckets: an unmatched IP shares the no-IP "unknown" row.
    assert artifact["rows"] == len(rows) == 6
    assert enricher.lookups == 8
    assert sum(r["completed"] for r in rows) == result.completed == 12
    assert sum(r["successes"] for r in rows) == result.successes == 8
    assert sum(r["bytes_up"] for r in rows) == result.bytes_up
    assert sum(r["challenges"] for r in rows) == result.challenges
    assert sum(r["timeouts"] for r in rows) == result.timeouts
    assert {r["bucket_start"] for r in rows} == {b["bucket_start"] for b in result.buckets}
    german = [r for r in rows if r["location"] and r["location"]["country_code"] == "DE"]
    assert len(german) == 2 and all(r["completed"] == 2 for r in german)
    assert german[0]["location"] == {
        "country_code": "DE",
        "country": "Germany",
        "region_code": None,
        "region": None,
        "city": "Berlin",
        "latitude": 52.5,
        "longitude": 13.4,
    }
    assert german[0]["network"] == {
        "isp": "Telekom",
        "organization": None,
        "asn": 3320,
        "asn_organization": "DTAG",
        "connection_type": "Cable/DSL",
        "usage_type": "residential",
    }
    assert german[0]["latency_histogram"] == {"format": "ceil_ms_v1", "bins": [[1, 1], [2, 1]]}
    unknown = [r for r in rows if r["location"] is None]
    assert all(r["network"] is None for r in unknown) and len(unknown) == 2
    assert all((r["completed"], r["successes"]) == (3, 1) for r in unknown)
    assert sum(r["challenges"] + r["timeouts"] for r in unknown) == 4
    assert [len(r["latency_histogram"]["bins"]) for r in unknown] == [1, 1]
    assert set(rows[0]) == {
        "bucket_start",
        "location",
        "network",
        "completed",
        "successes",
        "challenges",
        "blocks",
        "timeouts",
        "bytes_up",
        "bytes_down",
        "latency_histogram",
    }


async def test_geo_artifact_exists_without_traffic(tmp_path):
    stop = asyncio.Event()
    stop.set()
    sink = JsonSink(tmp_path)
    result = await run(
        config(), sink, RunOptions(stop=stop, transport_factory=FakeTransport, enricher=Enricher())
    )
    assert result.artifacts["geo"]["rows"] == 0
    assert (sink.directory / "geo.jsonl.gz").exists()
    assert not list(sink.directory.glob("*.partial"))


async def test_geo_with_the_local_dbip_database(tmp_path):
    from proxy_index.enrichment import LocalEnricher, find_database

    try:
        find_database()
    except (ValueError, FileNotFoundError):
        pytest.skip("No local DB-IP database")
    with LocalEnricher() as enricher:
        result = await run(
            config(requests=3),
            JsonSink(tmp_path),
            RunOptions(transport_factory=FakeTransport, enricher=enricher),
        )
    assert result.artifacts["geo"]["rows"] == 1
    assert result.artifacts["geo"]["release"]["sha256"] == enricher.release["sha256"]
