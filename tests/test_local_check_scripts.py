"""The local geonode check and the onboarding probe watcher, run against fakes."""

import argparse
import asyncio
import gzip
import hashlib
import importlib.util
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Enricher:
    release = "2026-11"

    def __init__(self, mmdb):
        self.mmdb = mmdb

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def lookup(self, ip):
        return {"country_code": "DE" if ip.startswith("1.") else None}


class Sink:
    def __init__(self, output):
        self.directory = output / f"run-{len(list(output.iterdir()))}"
        self.directory.mkdir()


def fake_run(calls, corrupt=False):
    async def run(config, sink, options):
        calls.append(config)
        for index, ip in enumerate(("1.1.1.1", "1.1.1.2", "9.9.9.9")):
            await options.observation_consumer(ip, 1_800_000_000 + index)
        raw = gzip.compress(b"a\nb\n")
        (sink.directory / "ips.jsonl.gz").write_bytes(raw)
        digest = "0" * 64 if corrupt else hashlib.sha256(raw).hexdigest()
        artifacts = {"ips": {"file": "ips.jsonl.gz", "sha256": digest, "rows": 2}}
        return SimpleNamespace(
            run_id=f"run{len(calls)}",
            successes=3,
            unique_ips=3,
            errors={},
            status_codes={"200": 3},
            to_dict=lambda: {"completed": 3, "artifacts": artifacts},
        )

    return run


@pytest.fixture
def geonode(monkeypatch):
    module = script("geonode-local-check.py")
    calls = []
    monkeypatch.setattr(module, "LocalEnricher", Enricher)
    monkeypatch.setattr(module, "JsonSink", Sink)
    monkeypatch.setattr(module, "RunConfig", lambda *a, **kw: (a, kw))
    monkeypatch.setattr(
        module,
        "load_provider",
        lambda config, credentials_file, parameters: SimpleNamespace(port=8000, **parameters),
    )
    monkeypatch.setattr(module, "run", fake_run(calls))
    monkeypatch.setattr(module.os, "umask", lambda mask: 0)
    return module, calls


def check_args(tmp_path):
    return argparse.Namespace(
        config="geonode.toml",
        credentials="private.toml",
        output=str(tmp_path / "out"),
        mmdb=None,
        country=[["de", "3"], ["fr", "3"]],
    )


def test_geonode_check_writes_rows_per_target_and_country(geonode, tmp_path, capsys):
    module, calls = geonode
    args = check_args(tmp_path)
    asyncio.run(module.check(args))

    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(row["target"], row["requested_country"]) for row in printed] == [
        ("generic_http", "de"),
        ("generic_http", "fr"),
        ("generic_https", "de"),
        ("generic_https", "fr"),
    ]
    assert [config[0][0] for config in calls] == ["generic_http"] * 2 + ["generic_https"] * 2
    assert calls[0][1] == {
        "requests": 3,
        "concurrency": 10,
        "processes": 4,
        "timeout_seconds": 15,
    }
    first = printed[0]
    assert first["geo_countries"] == {"DE": 2, "unknown": 1}
    assert first["geo_matches"] == 2
    assert printed[1]["geo_matches"] == 0
    assert (first["port"], first["requests"], first["successes"]) == (8000, 3, 3)
    path = Path(first["directory"]) / "observations.jsonl.gz"
    assert first["observations_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    rows = [json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
    assert rows[0] == {
        "ip": "1.1.1.1",
        "requested_country": "de",
        "observed_at": "2027-01-15T08:00:00+00:00",
        "geo": {"country_code": "DE"},
    }
    validation = json.loads((tmp_path / "out" / "validation.json").read_text())
    assert validation["results"] == printed
    assert validation["local_only"] is True
    assert (validation["config"], validation["geolocation_release"]) == ("geonode.toml", "2026-11")


def test_geonode_check_rejects_a_mismatched_artifact(geonode, tmp_path, monkeypatch):
    module, calls = geonode
    monkeypatch.setattr(module, "run", fake_run(calls, corrupt=True))
    with pytest.raises(AssertionError):
        asyncio.run(module.check(check_args(tmp_path)))
    assert not (tmp_path / "out" / "validation.json").exists()


def node(unit, status, requests):
    return {
        "region": "tor1",
        "health": "ok",
        "requests": requests,
        "assignment": {"unit": unit},
        "state": {"status": status},
    }


@pytest.mark.parametrize(("final", "code"), [("completed", 0), ("failed", 1)])
def test_watch_probe_saves_each_new_snapshot_until_the_run_ends(
    tmp_path, monkeypatch, capsys, final, code
):
    module = script("onboarding-watch-probe.py")
    launch = tmp_path / "launch.json"
    launch.write_text(json.dumps({"unit": "probe-1", "region": "tor1"}))
    other = {"region": "nyc1", "health": "ok"}
    frames = [
        {"collected_at": "t1", "servers": [other, node("probe-1.service", "running", 5)]},
        {"collected_at": "t1", "servers": [other, node("probe-1.service", "running", 5)]},
        {"collected_at": "t2", "servers": [other, node("other.service", "running", 0)]},
        {"collected_at": "t3", "servers": [other, node("probe-1.service", final, 9)]},
    ]
    for index, frame in enumerate(frames):
        frame["collected_epoch"] = index
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(frames.pop(0)))
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        snapshot.write_text(json.dumps(frames.pop(0)))

    monkeypatch.setattr(module.time, "sleep", sleep)
    argv = ["onboarding-watch-probe.py", "--launch", str(launch), "--monitor", str(tmp_path)]
    monkeypatch.setattr(sys, "argv", argv)

    assert module.main() == code
    assert sleeps == [2, 2, 2]
    output = tmp_path / "probe-1-samples.jsonl"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    samples = [json.loads(line) for line in output.read_text().splitlines()]
    assert [(s["collected_at"], s["collected_epoch"]) for s in samples] == [("t1", 0), ("t3", 3)]
    assert samples[1]["node"]["state"]["status"] == final
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(p["at"], p["requests"], p["health"]) for p in printed] == [
        ("t1", 5, "ok"),
        ("t3", 9, "ok"),
    ]
    assert printed[0]["workers"] is None
