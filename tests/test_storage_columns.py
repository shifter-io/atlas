"""Storage typed columns: JSON forms of column values, bucket counters and profiles split into
columns and `extra`, runs that cannot be stored losslessly, replacement counters and the cached
scope metrics."""

import json

import psycopg
import pytest
from helpers import FakeTransport
from storage_cases import measurement

from atlas import JsonSink, ProxyConfig, RunConfig, RunOptions, run, storage
from atlas.stats import Histogram
from atlas.storage import (
    _add_run_metrics,
    _bucket,
    _profile,
    _steps,
    column,
    connect,
    ingest,
    read_summary,
    status,
)

pytest_plugins = ["storage_cases"]


SHIFTER = {
    "adapter": "shifter",
    "version": "1",
    "gateway": {"host": "p.example", "port": 443},
    "pool": None,
    "parameters": {"country": "us", "sid": "[redacted]"},
    "session_policy": "fixed",
    "effective_ttl_seconds": 120,
    "extra_flags": {"x": "[redacted]"},
}


def edit_summary(directory, change):
    path = directory / "summary.json"
    summary = json.loads(path.read_text())
    change(summary)
    path.write_text(json.dumps(summary))
    return summary


def test_typed_columns_keep_json_forms():
    assert column("number", 15) == 15 and str(column("number", 15.0)) == "15.0"
    assert str(column("number", 1e-05)) == "0.00001"
    for kind, value in [
        ("number", 1e16),
        ("number", float("inf")),
        ("number", True),
        ("integer", 2**31),
        ("integer", "4"),
        ("text", 3),
    ]:
        with pytest.raises(TypeError):
            column(kind, value)
    assert column("text", None) is None and column("json", None) is None
    assert _steps([[10, 2], [12, 1], [40, 5]]) == ([10, 2, 28], [2, 1, 5])


def test_storage_keeps_psycopg_importable_for_callers():
    # storage re-exports psycopg so callers can catch storage.psycopg errors.
    assert storage.psycopg.OperationalError is psycopg.OperationalError


def test_bucket_counters_split_into_columns_and_extra():
    from datetime import UTC, datetime

    start = datetime(2026, 9, 25, 15, 20, tzinfo=UTC)
    counters = {
        "bucket_start": "2026-09-25T15:20:00+00:00",
        "completed": 3,
        "successes": 2,
        "response_body_bytes": 16,
        "errors": {"timeout": 1},
        "status_codes": {"200": 2},
        "latency_histogram": {"format": "ceil_ms_v1", "bins": [[11, 1], [15, 1]]},
        "future": 1,
    }
    row = _bucket(start, counters)
    assert row[:3] == (3, 2, 16) and row[5:7] == ([11, 4], [1, 1])
    assert row[7].obj == {"future": 1, "bucket_start": counters["bucket_start"]}
    canonical = counters | {"bucket_start": "2026-09-25T15:20:00.000000+00:00"}
    del canonical["future"]
    assert _bucket(start, canonical)[7] is None
    with pytest.raises(ValueError, match="bucket counters"):
        _bucket(start, counters | {"completed": 3.0})


def test_profile_types_known_fields_and_keeps_the_rest():
    target = {"id": "generic_http", "url": "http://x/", "engine": "http", "version": "1"}
    config = {
        "target": "generic_http",
        "provider": "p",
        "product": "r",
        "requested_country": "us",
        "runner_location": None,
        "proxy_protocol": "http",
        "provider_config": SHIFTER,
        "future": [1],
    }
    row = _profile({"target": target, "config": config}, "p", "r")
    assert (row["adapter"], row["gateway_port"], row["ttl_seconds"]) == ("shifter", 443, 120)
    assert row["extra"].obj == {"config": {"future": [1]}}
    odd = config | {
        "proxy_protocol": None,
        "provider_config": SHIFTER | {"effective_ttl_seconds": "300"},
    }
    row = _profile({"target": target | {"label": 2}, "config": odd}, "p", "r")
    assert row["adapter"] is None and row["proxy_protocol"] is None
    assert row["extra"].obj == {
        "config": {
            "proxy_protocol": None,
            "provider_config": odd["provider_config"],
            "future": [1],
        },
        "target": {"label": 2},
    }


async def test_profiles_round_trip_through_typed_columns(database, tmp_path):
    variants = [
        lambda c: c.update(provider_config=SHIFTER, timeout_seconds=7.5, requests_per_second=2.5),
        lambda c: c.update(
            provider_config=SHIFTER | {"gateway": "p.example"}, future={"a": [1, 2.0, None]}
        ),
        lambda c: c.update(proxy_protocol=None, requested_country="de"),
    ]
    for variant in variants:
        directory = await measurement(tmp_path)
        summary = edit_summary(directory, lambda s, v=variant: v(s["config"]))
        ingest(directory, database)
        assert read_summary(summary["run_id"], database) == summary
    with connect(database) as conn:
        rows = conn.execute(
            "SELECT adapter,gateway_host,ttl_seconds,proxy_protocol,extra FROM pool_stats "
            "ORDER BY pool_id"
        ).fetchall()
        timeouts = conn.execute("SELECT timeout_seconds::text FROM scope_stats").fetchall()
    assert rows[0][:4] == ("shifter", "p.example", 120, "http") and rows[0][4] is None
    assert rows[1][0] is None and rows[1][4]["config"]["future"] == {"a": [1, 2.0, None]}
    assert rows[2][3] is None and rows[2][4] == {"config": {"proxy_protocol": None}}
    assert ("7.5",) in timeouts and ("15",) in timeouts


async def test_unrepresentable_runs_are_rejected(database, tmp_path):
    for change, message in [
        (lambda s: s["config"].pop("runner_location"), "losslessly"),
        (lambda s: s["config"].update(concurrency="4"), "concurrency"),
        (lambda s: s["buckets"][0].pop("status_codes"), "bucket counters"),
    ]:
        directory = await measurement(tmp_path)
        edit_summary(directory, change)
        with pytest.raises(ValueError, match=message):
            ingest(directory, database)
    assert all(value == 0 for value in status(database)["counts"].values())


async def test_ingest_reconciles_replacements_and_other_country(database, tmp_path):
    from atlas.models import AnswerClasses, Outcome

    class Answers(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            if self.count % 3 == 0:
                return Outcome(1, error="http_status", status=429)
            # Every fourth exit is in another country than the requested one.
            return Outcome(
                1,
                ip=f"192.0.2.{self.count}" if self.count % 4 else f"198.51.100.{self.count}",
                status=200,
            )

    class Countries:
        def lookup(self, address):
            return {"country_code": "US" if address.startswith("192.0.2.") else "DE"}

    sink = JsonSink(tmp_path / "results")
    summary = await run(
        RunConfig(
            "generic_http",
            ProxyConfig("http", "unused.invalid", 8080),
            requests=20,
            concurrency=4,
            requested_country="US",
            provider="fixture",
            product="residential",
            answer_classes=AnswerClasses(("429",), ()),
        ),
        sink,
        RunOptions(transport_factory=Answers, enricher=Countries()),
    )
    assert summary.requested == 20 and summary.started == 29 and summary.not_started == 0
    assert summary.rate_limited == {"429": 9} and summary.other_country > 0
    assert summary.unique_ips == summary.successes - summary.other_country
    assert ingest(sink.directory, database)["imported"]
    metrics = status(database)["cached_scopes"][0]["metrics"]
    # The attempted total holds each replacement; the scope is the one without answer classes.
    assert metrics["requested"] == metrics["started"] == metrics["completed"] == 29
    assert metrics["unique_ips"] == summary.unique_ips


async def test_ingest_counts_replacements_before_any_database_access(tmp_path):
    """Runs without PostgreSQL: the counter checks precede every connection."""
    from atlas.models import AnswerClasses, Outcome

    class Answers(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            if self.count % 3 == 0:
                return Outcome(1, error="http_status", status=429)
            return Outcome(
                1,
                ip=f"192.0.2.{self.count}" if self.count % 4 else f"198.51.100.{self.count}",
                status=200,
            )

    class Countries:
        def lookup(self, address):
            return {"country_code": "US" if address.startswith("192.0.2.") else "DE"}

    sink = JsonSink(tmp_path / "results")
    summary = await run(
        RunConfig(
            "generic_http",
            ProxyConfig("http", "unused.invalid", 8080),
            requests=20,
            concurrency=1,
            requested_country="US",
            provider="fixture",
            product="residential",
            answer_classes=AnswerClasses(("429",), ()),
        ),
        sink,
        RunOptions(transport_factory=Answers, enricher=Countries()),
    )
    path = sink.directory / "summary.json"
    saved = json.loads(path.read_text())
    assert saved["requested"] == 20 and saved["started"] == 29 and saved["not_started"] == 0
    # The counters reconcile with the 9 replacements: ingest gets as far as the IP artifact.
    (sink.directory / "ips.jsonl.gz").write_bytes(b"changed")
    with pytest.raises(ValueError, match="IP artifact checksum"):
        ingest(sink.directory, tmp_path / "unused.toml")
    # Without the replacements the bounded run does not reconcile.
    del saved["rate_limited"]
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="Measurement counters do not reconcile"):
        ingest(sink.directory, tmp_path / "unused.toml")
    assert summary.other_country > 0


def test_scope_metrics_add_runs_and_mean_latency():
    """Cached scope metrics without Postgres: counters add up, latency comes from the merge."""
    names = (
        "started",
        "completed",
        "successes",
        "total_errors",
        "cancelled",
        "not_started",
        "response_body_bytes",
        "duration_seconds",
    )
    run = dict.fromkeys(names, 2) | {
        "errors": {"timeout": 1},
        "status_codes": {"200": 2},
        "pool_eligible": True,
    }
    histogram = Histogram()
    for value in (10, 11, 30):
        histogram.add(value)
    # Cached metrics from before latency_mean_ms existed gain it on the next run.
    metrics = {
        "runs": 1,
        "requested": 3,
        "completed": 2,
        "successes": 1,
        "unique_ips": 4,
        "errors": {"timeout": 2},
        "latency_ms": {"p50": 1},
    }
    _add_run_metrics(metrics, run, 3, 5, histogram)
    assert metrics["runs"] == 2 and metrics["requested"] == 6 and metrics["unique_ips"] == 9
    assert metrics["completed"] == 4 and metrics["success_rate"] == 0.75
    assert metrics["errors"] == {"timeout": 3} and metrics["status_codes"] == {"200": 2}
    assert set(metrics["latency_ms"]) == {"p50", "p90", "p95", "p99"}
    assert metrics["latency_ms"]["p50"] == 11 and metrics["latency_mean_ms"] == 17
    empty = {}
    _add_run_metrics(
        empty,
        dict.fromkeys(names, 0) | {"errors": {}, "status_codes": {}, "pool_eligible": False},
        0,
        0,
        Histogram(),
    )
    assert empty["unique_ips"] is None and empty["success_rate"] is None
    assert empty["latency_mean_ms"] is None
