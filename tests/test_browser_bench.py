"""scripts/browser_bench.py over fake browsers (no traffic) and scripts/browser_bench_report.py
on small fixture benches."""

from __future__ import annotations

import importlib
import json
import os
import sys
from collections import Counter
from functools import partial
from pathlib import Path

import pytest
from browser_node_cases import Fleet, make_state
from browser_transport_cases import Script

from atlas.browser import driver, release
from atlas.browser.chrome import ChromeRefused
from atlas.browser.driver import UserAgentRefused
from atlas.browser.procstats import Process

sys.path.append(str(Path(__file__).resolve().parents[1] / "scripts"))
bench = importlib.import_module("browser_bench")
report = importlib.import_module("browser_bench_report")

GIB = 1 << 30


class Ps:
    """A fake `ps`: one browser below this process, its CPU time growing a second per read."""

    def __init__(self, rss: int = GIB) -> None:
        self.rss, self.reads = rss, 0

    def __call__(self) -> dict[int, Process]:
        self.reads += 1
        return {os.getpid() + 1: Process(os.getpid(), self.rss, float(self.reads))}


def run(tmp_path, *extra, fleet=None, tries=12, engine="camoufox", launch=None):
    state = make_state(tmp_path)
    fleet = fleet or Fleet(delay=0.02)
    argv = ["--settings", str(state / "settings.toml"), "--state", str(state)]
    argv += ["--host", "browser-1", "--network", "shifter/residential"]
    argv += ["--target", "amazon_pdp", "--tries", str(tries), "--engine", engine]
    argv += ["--country", "us", "--out", str(tmp_path / "out"), *extra]
    return bench.main(argv, launch=launch or fleet.launch, read=Ps()), fleet


def written(tmp_path, engine="camoufox"):
    directory = tmp_path / "out" / engine / "amazon_pdp"
    tries = [json.loads(line) for line in (directory / "tries.jsonl").read_text().splitlines()]
    samples = [json.loads(line) for line in (directory / "samples.jsonl").read_text().splitlines()]
    return directory, tries, samples, json.loads((directory / "summary.json").read_text())


def test_at_most_browsers_tries_at_once_and_every_try_recorded(tmp_path):
    status, fleet = run(tmp_path, "--browsers", "3")
    directory, tries, samples, summary = written(tmp_path)
    assert status == 0 and fleet.peak == 3 and len(fleet.browsers) == 3
    assert sorted(record["i"] for record in tries) == list(range(12))
    for record in tries:
        assert record["engine"] == "camoufox"
        assert record["started_at"] <= record["ended_at"]
        assert record["duration_ms"] == record["timings_ms"]["total"] >= 20
        assert "fingerprint" not in record and record["bytes_down_est"] == 4321
        # The relay's count: nothing crossed it, since the fake browser opens no connection.
        assert (record["wire_up"], record["wire_down"], record["wire_exact"]) == (0, 0, True)
    # The settings' blocking, a new context per try.
    contexts = [block for browser in fleet.browsers for _, block in browser.contexts]
    assert contexts == [("image",)] * 12
    assert samples[0]["cpu_percent"] is None and samples[-1]["rss_bytes"] == GIB
    verdicts = Counter(record["verdict"] for record in tries)
    assert {k: n for k, n in summary["outcomes"].items() if n} == dict(verdicts)
    assert summary["tries"] == 12 and summary["browsers"] == 3 and summary["recycle_after"] == 25
    assert summary["bytes"] == {"total": 12 * 4321, "per_try": 4321}
    assert summary["wire"]["down_total"] == 0 and summary["wire"]["up_per_try"] == 0
    assert summary["samples"] == len(samples) >= 2 and summary["rss_bytes"]["peak"] == GIB
    assert summary["cpu_seconds"] == samples[-1]["cpu_seconds"]
    assert summary["identity"] == {} and summary["wall_s"] >= 0
    assert {p.stat().st_mode & 0o777 for p in directory.iterdir()} == {0o600}
    for folder in (directory, directory.parent):
        assert folder.stat().st_mode & 0o777 == 0o700


def test_the_next_try_starts_as_soon_as_one_ends(tmp_path):
    status, fleet = run(tmp_path, "--browsers", "8", "--recycle-after", "2", tries=20)
    _, tries, _, summary = written(tmp_path)
    assert status == 0 and fleet.peak == 8 and summary["recycle_after"] == 2
    # 20 tries on 8 browsers recycled every 2 tries: at least 10 launches.
    assert len(fleet.browsers) >= 10 and len(tries) == 20
    starts = sorted(record["started_at"] for record in tries)
    ends = sorted(record["ended_at"] for record in tries)
    assert starts[8] >= ends[0]  # the ninth try waited for a slot


class Identified(Fleet):
    async def launch(self):
        browser = await Fleet.launch(self)
        browser.identity = {"engine": "chrome", "version": "151.0.1", "user_agent": "UA/151"}
        return browser


def test_the_engine_names_the_folder_and_its_identity_is_kept(tmp_path):
    status, _ = run(tmp_path, fleet=Identified(), engine="chrome", tries=2)
    directory, tries, _, summary = written(tmp_path, "chrome")
    assert status == 0 and summary["engine"] == "chrome"
    assert summary["identity"]["version"] == "151.0.1"
    assert {record["engine"] for record in tries} == {"chrome"}
    assert summary["network"] == "shifter/residential" and summary["host"] == "browser-1"


def test_no_credential_or_shown_address_is_written(tmp_path):
    fleet = Fleet()
    fleet.script = Script(url="http://203.0.113.7:8080/a?token=TOKENPROBE", found=0)
    run(tmp_path, fleet=fleet, tries=2)
    out = tmp_path / "out"
    data = b"".join(p.read_bytes() for p in out.rglob("*") if p.is_file())
    for secret in (b"secret", b"acct", b"token-shifter", b"203.0.113.7", b"TOKENPROBE"):
        assert secret not in data
    assert not list(out.rglob("*.jpg"))


@pytest.mark.parametrize(
    "extra",
    [("--browsers", "0"), ("--browsers", "17"), ("--recycle-after", "0"), ("--country", "gb")],
)
def test_refused_requests_run_no_try(tmp_path, capsys, extra):
    status, fleet = run(tmp_path, *extra)
    assert status == 2 and fleet.browsers == [] and "browser_bench:" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_over_the_tries_cap_or_into_an_existing_bench_is_refused(tmp_path):
    assert run(tmp_path / "cap", tries=bench.MAX_TRIES + 1)[0] == 2
    (tmp_path / "again/out/camoufox/amazon_pdp").mkdir(parents=True)
    status, fleet = run(tmp_path / "again", tries=1)
    assert status == 2 and fleet.browsers == []


def test_an_engine_refused_during_the_run_stops_it(tmp_path, capsys):
    async def refused():
        raise UserAgentRefused("the user agent cannot claim Chrome 151")

    status, _ = run(tmp_path, launch=refused, engine="chrome", tries=4)
    assert status == 1 and "cannot claim Chrome" in capsys.readouterr().err


def test_the_engines_launch_with_the_hosts_settings(tmp_path, monkeypatch):
    state = make_state(tmp_path)
    args = bench.parse(
        ["--settings", str(state / "settings.toml"), "--state", str(state)]
        + [
            *("--host", "browser-1", "--network", "shifter/residential", "--target", "amazon_pdp"),
            *("--tries", "1", "--engine", "chrome", "--country", "us", "--out", str(tmp_path)),
        ]
    )
    plan = bench.prepare(args, bench.MAX_TRIES)
    checked = []
    monkeypatch.setattr(bench.chrome, "detect", checked.append)
    launch = bench.engine_launch(args, plan)
    assert isinstance(launch, partial) and launch.func is driver.launch_chrome
    assert launch.args == (checked[0],) and checked[0].startswith("/")
    assert launch.keywords == {"country": "US", "headless": True}
    args.engine = "camoufox"
    with pytest.raises(bench.UserError, match="not installed"):
        bench.engine_launch(args, plan)
    monkeypatch.setattr(release, "executable", lambda dest: dest / "firefox")
    launch = bench.engine_launch(args, plan)
    assert launch.func is driver.launch and launch.args == (state / "camoufox/firefox",)
    assert launch.keywords == {"locale": "en-US", "headless": True}


def test_a_missing_chrome_is_refused_before_any_try(tmp_path, monkeypatch, capsys):
    def missing(executable):
        raise ChromeRefused("Chrome is missing")

    monkeypatch.setattr(bench.chrome, "detect", missing)
    state = make_state(tmp_path)
    argv = ["--settings", str(state / "settings.toml"), "--state", str(state)]
    argv += ["--host", "browser-1", "--network", "shifter/residential", "--target", "amazon_pdp"]
    argv += ["--tries", "1", "--engine", "chrome", "--country", "us", "--out", str(tmp_path / "o")]
    assert bench.main(argv) == 2 and "Chrome is missing" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def record(i: int, verdict: str, duration: int, size: int | None = 1000) -> dict:
    errors = {"ok": None, "block": "blocked", "timeout": "timeout", "error": "browser_error"}
    return {
        "i": i,
        "verdict": verdict,
        "error": errors[verdict],
        "duration_ms": duration,
        "bytes_down_est": size,
    }


def sample(rss: int, percent: float | None, seconds: float) -> dict:
    return {"rss_bytes": rss, "cpu_percent": percent, "cpu_seconds": seconds}


def test_the_summary_statistics_on_known_durations():
    verdicts = ["ok"] * 7 + ["block", "timeout", "error"]
    records = [record(i, v, 100 * (i + 1)) for i, v in enumerate(verdicts)]
    records[0]["bytes_down_est"] = None
    for index, down in enumerate((100, 300, 200)):
        records[index] |= {"wire_down": down, "wire_up": 10}
    samples = [sample(GIB, None, 0), sample(2 * GIB, 100.0, 1), sample(3 * GIB, 300.0, 4)]
    summary = bench.summarize(records, samples, {"engine": "camoufox"})
    assert summary["engine"] == "camoufox"
    assert summary["outcomes"] == {
        "ok": 7,
        "challenge": 0,
        "block": 1,
        "timeout": 1,
        "proxy_error": 0,
        "other": 1,
    }
    assert summary["other_errors"] == {"browser_error": 1}
    assert summary["request_ms"] == {"average": 550.0, "median": 550.0, "p95": 1000, "max": 1000}
    assert summary["bytes"] == {"total": 9000, "per_try": 900}
    assert summary["wire"] == {
        "down_total": 600,
        "down": {"average": 200.0, "median": 200, "p95": 300, "max": 300},
        "up_per_try": 10,
    }
    assert summary["cpu_percent"] == {"average": 200.0, "p95": 300.0, "peak": 300.0}
    assert summary["rss_bytes"] == {"average": 2 * GIB, "peak": 3 * GIB}
    assert summary["cpu_seconds"] == 4 and summary["samples"] == 3
    empty = bench.summarize([], [], {})
    assert empty["request_ms"]["median"] is None and empty["bytes"]["per_try"] is None
    assert empty["wire"] == {"down_total": 0, "down": bench._stats([]), "up_per_try": None}
    assert empty["cpu_seconds"] is None


def fixture_bench(out: Path, host: str, engine: str, rss: int, percent: float, seconds: float):
    directory = out / engine / "amazon_pdp"
    directory.mkdir(parents=True)
    durations = (1000, 2000) if engine == "chrome" else (3000, 5000)
    records = [record(0, "ok", durations[0]), record(1, "block", durations[1])]
    lines = [sample(rss, None, 0), sample(rss, percent, seconds / 2), sample(rss, percent, seconds)]
    for name, rows in (("tries.jsonl", records), ("samples.jsonl", lines)):
        (directory / name).write_text("".join(json.dumps(row) + "\n" for row in rows))
    identity = {"version": "151.0.1"} if engine == "chrome" else {"release": "135.0.1-beta.24"}
    summary = {"host": host, "engine": engine, "target": "amazon_pdp", "identity": identity}
    summary |= {"network": "shifter/residential", "browsers": 8, "tries": 2}
    summary["cpu_seconds"] = seconds
    (directory / "summary.json").write_text(json.dumps(summary))


def make_minis(tmp_path) -> list[Path]:
    minis = []
    for host in ("browser-1", "browser-2"):
        out = tmp_path / host
        fixture_bench(out, host, "camoufox", 3 * GIB, 200.0, 8.0)
        fixture_bench(out, host, "chrome", 2 * GIB, 150.0, 4.0)
        minis.append(out)
    return minis


def test_the_report_merges_two_minis(tmp_path):
    destination = tmp_path / "report.md"
    assert report.main([*map(str, make_minis(tmp_path)), "--out", str(destination)]) == 0
    text = destination.read_text()
    assert "Minis: browser-1, browser-2; engines: Camoufox 135.0.1-beta.24, Chrome 151.0.1" in text
    assert "| amazon_pdp | Chrome | 4 | 2 (50.0%) | 0 (0.0%) | 2 (50.0%) |" in text
    assert "| 1,500 | 1,500 | 2,000 | 1000 B |" in text
    assert (
        "| amazon_pdp | Camoufox | 4 | 2 (50.0%) |" in text and "| 4,000 | 4,000 | 5,000 |" in text
    )
    assert "| browser-2 | Chrome | 2 | 150% | 150% | 2.00 GiB | 2.00 GiB | 2.00 |" in text
    assert "| browser-1 | Camoufox | 2 | 200% | 200% | 3.00 GiB | 3.00 GiB | 4.00 |" in text
    assert "Chrome used 2.00 CPU seconds against Camoufox's 4.00 (50% less)" in text
    assert "Chrome held 2.00 GiB on average (peak 2.00 GiB)" in text and "33% less" in text
    assert "Success over all targets: Camoufox 50.0%, Chrome 50.0%" in text
    assert "**Verdict:** Chrome is better for the minis' CPU and RAM." in text
    assert str(tmp_path) not in text and "/" not in text.replace("shifter/residential", "")


def test_a_bad_host_name_becomes_a_mini_number_and_nothing_found_is_refused(tmp_path):
    out = tmp_path / "one"
    fixture_bench(out, "mini.example.org", "chrome", GIB, 100.0, 2.0)
    benches = report.load([out])
    assert [b["mini"] for b in benches] == ["mini 1"]
    assert "Only Chrome was benched: no verdict." in report.report(benches)
    assert "mini.example.org" not in report.report(benches)
    assert report.main([str(tmp_path / "empty"), "--out", str(tmp_path / "r.md")]) == 2


def test_the_verdict_when_mixed_or_not_sampled():
    def totals(per_try, rss):
        return {
            "cpu_per_try": per_try,
            "cpu_percent": {"average": 100.0},
            "rss_bytes": {"average": rss, "peak": rss},
            "success": 0.9,
        }

    mixed = report.verdict({"camoufox": totals(1.0, 3 * GIB), "chrome": totals(2.0, GIB)})
    assert mixed[-1] == "\n**Verdict:** mixed: Camoufox needs less CPU, Chrome less RAM."
    unsampled = report.verdict({"camoufox": totals(None, GIB), "chrome": totals(1.0, GIB)})
    assert unsampled == ["CPU or RAM was not sampled for an engine: no verdict."]
    assert report._size(None) == "-" and report._size(512) == "512 B"
    assert report._share(0, 0) == "0" and report._less(1.0, 0) == "the same"
