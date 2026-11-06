import asyncio
import gzip
import hashlib
import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import FakeTransport, Gateway, server

from proxy_index import JsonSink, ProxyConfig, RunConfig, RunOptions, run
from proxy_index.browser.contracts import RUN_TARGETS
from proxy_index.cli import main
from proxy_index.enrichment import LocalEnricher, enrich_run, find_database
from proxy_index.storage import ENRICHMENT_FIELDS


@pytest.fixture
def fake_database(tmp_path, monkeypatch):
    database = tmp_path / "fixture.mmdb"
    database.write_bytes(b"test database")

    class Reader:
        closed = False
        calls = 0

        def metadata(self):
            return SimpleNamespace(database_type="DBIP-Full", build_epoch=1000)

        def close(self):
            self.closed = True

        def get(self, address):
            self.calls += 1
            if address == "127.0.0.1":
                return None
            return {
                "country": {"iso_code": "US", "names": {"en": "United States"}},
                "subdivisions": [{"iso_code": "CA", "names": {"en": "California"}}],
                "city": {"names": {"en": "Mountain View"}},
                "traits": {
                    "autonomous_system_number": 15169,
                    "isp": "Google",
                    "user_type": "hosting",
                    "connection_type": "Corporate",
                },
            }

    reader = Reader()
    monkeypatch.setattr("proxy_index.enrichment.maxminddb.open_database", lambda _: reader)
    return database, reader


def test_mmdb_mapping_cache_and_unknown(fake_database):
    database, reader = fake_database
    with LocalEnricher(database, cache_size=1) as enricher:
        result = enricher.lookup("8.8.8.8")
        assert set(result) == set(ENRICHMENT_FIELDS)
        assert result["asn"] == 15169 and result["country_code"] == "US"
        assert result["region_code"] == "CA" and result["usage_type"] == "hosting"
        result["asn"] = 999
        assert enricher.lookup("8.8.8.8")["asn"] == 15169
        assert reader.calls == 1
        assert not enricher.lookup("127.0.0.1")["matched"]
        assert len(enricher.cache) == 1
        assert len(enricher.release["sha256"]) == 64
    assert reader.closed


async def test_enrichment_preserves_original_and_versions_output(tmp_path, fake_database):
    database, _ = fake_database
    sink = JsonSink(tmp_path / "runs")
    await run(
        RunConfig("generic_http", ProxyConfig("http", "localhost", 8080), requests=2),
        sink,
        RunOptions(transport_factory=FakeTransport),
    )
    summary_before = (sink.directory / "summary.json").read_bytes()
    report = enrich_run(sink.directory, database)
    assert report["rows"] == 1 and report["unmatched_rows"] == 0
    assert (sink.directory / "summary.json").read_bytes() == summary_before
    with gzip.open(sink.directory / report["file"], "rt") as stream:
        row = json.loads(stream.readline())
    assert row["enrichment"]["isp"] == "Google" and row["count"] == 2
    with pytest.raises(FileExistsError):
        enrich_run(sink.directory, database)
    with (sink.directory / "ips.jsonl.gz").open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        enrich_run(sink.directory, database)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([{"run_id": "other", "ip": "192.0.2.1"}], "different run ID"),
        ([], "row count"),
    ],
)
async def test_enrichment_rejects_a_mismatched_ip_artifact(tmp_path, fake_database, rows, message):
    database, _ = fake_database
    sink = JsonSink(tmp_path / "runs")
    await run(
        RunConfig("generic_http", ProxyConfig("http", "localhost", 8080), requests=2),
        sink,
        RunOptions(transport_factory=FakeTransport),
    )
    source = sink.directory / "ips.jsonl.gz"
    with gzip.open(source, "wt", encoding="utf-8") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in rows)
    summary = json.loads((sink.directory / "summary.json").read_text())
    summary["artifacts"]["ips"]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    (sink.directory / "summary.json").write_text(json.dumps(summary))
    with pytest.raises(ValueError, match=message):
        enrich_run(sink.directory, database)
    assert not list(sink.directory.glob("enrichment-*.json"))


def test_database_discovery_is_explicit_when_ambiguous(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DBIP_MMDB", raising=False)
    (tmp_path / "db-ip").mkdir()
    for name in ("one.mmdb", "two.mmdb"):
        (tmp_path / "db-ip" / name).touch()
    with pytest.raises(ValueError, match="exactly one"):
        find_database()
    monkeypatch.setenv("DBIP_MMDB", str(tmp_path / "db-ip" / "one.mmdb"))
    assert find_database().name == "one.mmdb"
    with pytest.raises(FileNotFoundError):
        find_database(tmp_path / "missing.mmdb")


def test_cli_targets_and_missing_secret(monkeypatch, capsys):
    assert main(["targets"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [t["id"] for t in listed if t["engine"] == "http"] == ["generic_http", "generic_https"]
    assert [t["id"] for t in listed if t["engine"] == "browser"] == list(RUN_TARGETS)
    monkeypatch.delenv("PROXY_URL", raising=False)
    assert main(["run", "--target", "generic_http"]) == 2
    assert "ValueError" in capsys.readouterr().err


def test_real_local_mmdb_when_present():
    paths = list((Path(__file__).parents[1] / "db-ip").glob("*.mmdb"))
    if not paths:
        pytest.skip("Local licensed MMDB is not included in public builds")
    with LocalEnricher(paths[0]) as enricher:
        assert enricher.lookup("8.8.8.8")["asn"] == 15169
        assert enricher.lookup("2001:4860:4860::8888")["matched"]
        assert enricher.release["database_type"].lower().startswith("dbip")


@pytest.mark.parametrize("interrupt", [False, True])
async def test_cli_process_and_signal(tmp_path, interrupt):
    gateway = Gateway(auth=True, delay=0.2 if interrupt else 0)
    async with server(gateway) as port:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "proxy_index.cli",
            "run",
            "--target",
            "generic_http",
            "--requests",
            "10",
            "--concurrency",
            "2",
            "--output",
            str(tmp_path),
            env=os.environ
            | {
                "PROXY_URL": f"http://test:secret@127.0.0.1:{port}",
                "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if interrupt:
            async with asyncio.timeout(5):
                await gateway.accepted.wait()
            process.send_signal(signal.SIGTERM)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
    assert b"secret" not in stdout + stderr
    assert process.returncode == (130 if interrupt else 0)
    summary = json.loads(stdout)
    assert summary["status"] == ("cancelled" if interrupt else "completed")
    assert summary["successes"] == (0 if interrupt else 10)
