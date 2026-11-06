"""scripts/report_live.py: the latency table of a saved live measurement, with and without the
mean (latency_mean_ms), and the full report against its golden."""

from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("report_live", ROOT / "scripts" / "report_live.py")
report_live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report_live)


def summary(**extra):
    return {
        "config": {"provider": "fixture", "concurrency": 4, "timeout_seconds": 10},
        "run_id": "run-1",
        "status": "completed",
        "framework_version": "0.0.0",
        "target": {"id": "generic_http", "url": "http://example.test/"},
        "requested": 10,
        "started": 10,
        "completed": 10,
        "successes": 9,
        "success_rate": 0.9,
        "unique_ips": 9,
        "duration_seconds": 2.0,
        "requests_per_second": 5.0,
        "response_body_bytes": 90,
        "started_at": "2026-11-06T10:00:00+00:00",
        "finished_at": "2026-11-06T10:00:02+00:00",
        "errors": {"timeout": 1},
        "status_codes": {"200": 9},
        "latency_ms": {"p50": 110, "p90": 1200, "p95": 1300, "p99": 1500},
    } | extra


def test_latency_rows_end_with_the_mean():
    rows = report_live.latency_rows(summary(latency_mean_ms=1234.5))
    assert rows == [
        ("P50", "110"),
        ("P90", "1,200"),
        ("P95", "1,300"),
        ("P99", "1,500"),
        ("Mean", "1,234.5"),
    ]
    # A run without successes has a null mean, a summary from before it none at all.
    assert report_live.latency_rows(summary(latency_mean_ms=None))[-1] == ("Mean", "n/a")
    assert report_live.latency_rows(summary())[-1] == ("Mean", "n/a")


def test_report_prints_the_mean_or_na(tmp_path):
    cases = (("with", {"latency_mean_ms": 207.3}, "207.3"), ("without", {}, "n/a"))
    for name, extra, shown in cases:
        directory = tmp_path / name
        directory.mkdir()
        (directory / "summary.json").write_text(json.dumps(summary(**extra)))
        page = report_live.report(directory).read_text()
        assert f"<tr><td>Mean</td><td>{shown}</td></tr>" in page
        assert "<tr><td>P50</td><td>110</td></tr>" in page


GOLDEN = ROOT / "tests" / "fixtures" / "report_live" / "report.html"


def saved_run(directory):
    """A saved shifter run without the strict flag and one enrichment release: every section of
    the report, the offline geography and its fallback caption included."""
    provider = {
        "adapter": "shifter",
        "version": "1.2",
        "gateway": {"host": "gateway.example.test", "port": 8080},
        "parameters": {"country": "us"},
        "session_policy": "rotate",
    }
    run = summary(latency_mean_ms=207.3, unique_ips=4)
    run["config"] = run["config"] | {"provider_config": provider}
    run["errors"] = {"timeout": 1, "invalid_body": 2}
    (directory / "summary.json").write_text(json.dumps(run))
    rows = [
        ("192.0.2.1", 4, "US", 64500, "Example <ISP>"),
        ("192.0.2.2", 3, "US", 64500, "Example <ISP>"),
        ("198.51.100.7", 1, "DE", 64501, "Beispiel & Co"),
        ("203.0.113.9", 1, None, None, ""),
    ]
    with gzip.open(directory / "enrichment.jsonl.gz", "wt") as stream:
        for ip, count, country, asn, isp in rows:
            data = {"country_code": country, "asn": asn, "isp": isp}
            stream.write(json.dumps({"ip": ip, "count": count, "enrichment": data}) + "\n")
    release = {"file": "enrichment.jsonl.gz", "release": {"file": "geo-2026-11.mmdb"}}
    (directory / "enrichment-geo.json").write_text(json.dumps(release))


def test_report_full_output_is_unchanged(tmp_path):
    saved_run(tmp_path)
    assert report_live.report(tmp_path) == tmp_path / "report.html"
    assert (tmp_path / "report.html").read_text() == GOLDEN.read_text()


def test_report_rejects_two_enrichment_releases(tmp_path):
    saved_run(tmp_path)
    (tmp_path / "enrichment-other.json").write_text("{}")
    with pytest.raises(ValueError, match="single enrichment release"):
        report_live.report(tmp_path)
