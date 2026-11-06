"""The hour engine on the worker: the start script and the worker sampler.

Every tool the start script calls is a local stub; the sampler reads a fake /proc and cgroup.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from campaign_hour_worker_cases import member, tar_bytes

from proxy_index.campaign import fanout, sampler
from proxy_index.campaign.contracts import WORKER_ROOT, WORKER_SECRETS

# --- start script ----------------------------------------------------------------------------


def test_the_start_script_installs_arms_and_keeps_secrets_off_argv(tmp_path):
    """9: secrets only from stdin to a 0600 file; wheel installed only when its SHA differs."""
    bin_dir, log = tmp_path / "bin", tmp_path / "calls.log"
    bin_dir.mkdir()
    for tool, out in (("uv", ""), ("systemd-run", ""), ("timedatectl", "yes")):
        (bin_dir / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> {log}\necho {out}\n')
        (bin_dir / tool).chmod(0o755)
    payload = tar_bytes(
        member("run/m-net00/campaign.json", b"{}"),
        member("run/m-net00/provider.toml", b""),
        member("run/m-net00/assignment.json", b"{}"),
        member("wheel/proxy_index-1.0-py3-none-any.whl", b"wheel"),
        member("secrets/credentials.toml", b"password = 'secret-x'\n"),
    )
    worker, secrets = tmp_path / "worker", tmp_path / "secrets"
    command = fanout.start_command("m-net00", "lon1", 3)
    assert command[3:5] == [WORKER_ROOT, WORKER_SECRETS]
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    for _ in range(2):
        subprocess.run(
            [sys.executable, "-c", command[2], str(worker), str(secrets), *command[5:]],
            input=payload,
            env=env,
            check=True,
        )
    calls = log.read_text().splitlines()
    assert [c.split()[0] for c in calls] == [
        "uv",
        "timedatectl",
        "systemd-run",
        "systemd-run",
        "timedatectl",
        "systemd-run",
        "systemd-run",
    ]
    assert "--property=LimitNOFILE=65536" in calls[2] and "--round 3" in calls[2]
    assert "proxy_index.campaign.sampler" in calls[3] and "secret-x" not in log.read_text()
    credentials = secrets / "credentials.toml"
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert not list(worker.rglob("credentials.toml"))
    marker = json.loads((worker / "active-run.json").read_text())
    assert marker == {
        "kind": "round",
        "region": "lon1",
        "unit": "pi-camp-round",
        "state_path": "run/m-net00/regions/lon1/rounds/03/round.json",
    }
    start = json.loads(
        (worker / "run/m-net00/regions/lon1/rounds/03/worker-start.json").read_text()
    )
    assert start["ntp_synchronized"] is True and start["wheel_installed"] is False
    assert "--mmdb" not in calls[2]  # no database in this image: no country check


@pytest.mark.parametrize("count", [1, 2])
def test_the_round_gets_the_country_check_from_one_image_database(tmp_path, count):
    """CTRY-2: the round runs with `--mmdb` only when the image holds exactly one MMDB."""
    bin_dir, log = tmp_path / "bin", tmp_path / "calls.log"
    bin_dir.mkdir()
    for tool, out in (("uv", ""), ("systemd-run", ""), ("timedatectl", "yes")):
        (bin_dir / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> {log}\necho {out}\n')
        (bin_dir / tool).chmod(0o755)
    worker = tmp_path / "worker"
    (worker / "db-ip").mkdir(parents=True)
    for index in range(count):
        (worker / "db-ip" / f"dbip-{index}.mmdb").write_bytes(b"x")
    payload = tar_bytes(
        member("run/m-net00/campaign.json", b"{}"),
        member("wheel/proxy_index-1.0-py3-none-any.whl", b"wheel"),
        member("secrets/credentials.toml", b""),
    )
    command = fanout.start_command("m-net00", "lon1", 0)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    subprocess.run(
        [sys.executable, "-c", command[2], str(worker), str(tmp_path / "secrets"), *command[5:]],
        input=payload,
        env=env,
        check=True,
    )
    [round_call] = [c for c in log.read_text().splitlines() if "onboarding round" in c]
    database = f"--mmdb {worker / 'db-ip' / 'dbip-0.mmdb'}"
    assert round_call.endswith(database) is (count == 1)
    assert fanout.MMDB_DIR == f"{WORKER_ROOT}/db-ip"


# --- worker sampler --------------------------------------------------------------------------

STAT_1 = "cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 50 0 50 350 50 0 0 0\ncpu1 50 0 50 350 50\n"
STAT_2 = "cpu  200 0 200 800 100 0 0 100 0 0\ncpu0 150 0 150 350 50 0 0 0\ncpu1 50 0 50 400 50\n"


def proc_tree(tmp_path: Path, stat_text: str, pids=("41",)) -> tuple[Path, Path]:
    proc, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    proc.mkdir(exist_ok=True)
    cgroup.mkdir(exist_ok=True)
    (proc / "stat").write_text(stat_text)
    (proc / "loadavg").write_text("1.50 1.00 0.50 2/100 41\n")
    (proc / "meminfo").write_text("MemTotal: 4000 kB\nMemAvailable: 1000 kB\nShmem:\n")
    (cgroup / "cgroup.procs").write_text("\n".join(pids) + "\n")
    for pid in pids:
        (proc / pid).mkdir(exist_ok=True)
        (proc / pid / "statm").write_text("1000 256 0 0 0 0 0\n")
    return proc, cgroup


def test_sampler_parses_proc_text(tmp_path):
    ticks = sampler.cpu_ticks(STAT_1)
    assert ticks["cpu"] == (1000, 800, 0) and ticks["cpu1"] == (500, 400, 0)
    assert sampler.busy(sampler.cpu_ticks(STAT_2)["cpu"], ticks["cpu"]) == (75.0, 25.0)
    assert sampler.busy(ticks["cpu"], ticks["cpu"]) == (0.0, 0.0)
    assert sampler.memory_used_pct("MemTotal: 4000 kB\nMemAvailable: 1000 kB\n") == 75.0
    proc, cgroup = proc_tree(tmp_path, STAT_1, pids=("41", "42"))
    (proc / "42" / "statm").unlink()
    first, ticks = sampler.sample(proc, cgroup, {})
    assert "cpu_pct" not in first and first["load1"] == 1.5 and first["mem_used_pct"] == 75.0
    assert first["rss_mb"] == round(256 * os.sysconf("SC_PAGE_SIZE") / 1024**2, 1)
    (proc / "stat").write_text(STAT_2)
    second, _ = sampler.sample(proc, cgroup, ticks)
    assert (second["cpu_pct"], second["steal_pct"], second["core_max_pct"]) == (75.0, 25.0, 100.0)
    (cgroup / "cgroup.procs").write_text("")
    assert sampler.sample(proc, cgroup, ticks)[0] is None
    assert sampler.round_rss_mb(proc, tmp_path / "gone") is None


def test_sampler_runs_until_the_round_unit_is_empty(tmp_path):
    proc, cgroup = proc_tree(tmp_path, STAT_1)
    output = tmp_path / "host-samples.jsonl"
    texts = iter([STAT_2, STAT_2])

    def sleep(_seconds):
        text = next(texts, None)
        if text is None:
            (cgroup / "cgroup.procs").write_text("")
        else:
            (proc / "stat").write_text(text)

    assert (
        sampler.main(
            ["--output", str(output), "--proc", str(proc), "--cgroup", str(cgroup)], sleep=sleep
        )
        == 0
    )
    lines = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(lines) == 3 and lines[1]["cpu_pct"] == 75.0 and lines[2]["cpu_pct"] == 0.0


def test_sampler_summary_percentiles(tmp_path):
    path = tmp_path / "host-samples.jsonl"
    rows = [
        {
            "cpu_pct": float(v),
            "core_max_pct": v + 1.0,
            "steal_pct": v / 10,
            "load1": v / 50,
            "mem_used_pct": 40.0 + v / 10,
            "rss_mb": 100.0 + v,
        }
        for v in range(1, 101)
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n{}\n")
    summary = sampler.summarize(path)
    assert summary == {
        "cpu_p50_pct": 50.0,
        "cpu_p95_pct": 95.0,
        "cpu_max_pct": 100.0,
        "cpu_peak_pct": 100.0,
        "cpu_core_max_pct": 101.0,
        "steal_p95_pct": 9.5,
        "load1_p95": 1.9,
        "mem_used_max_pct": 50.0,
        "rss_peak_mb": 200.0,
    }
    assert set(sampler.summarize(tmp_path / "missing.jsonl").values()) == {None}
    timed = tmp_path / "timed.jsonl"
    timed.write_text(
        "\n".join(
            json.dumps({"time": t, "cpu_pct": c})
            for t, c in ((10.0, 1.0), (20.0, 2.0), (30.0, 80.0), (40.0, 90.0))
        )
        + "\n"
    )
    assert sampler.summarize(timed, since=30.0)["cpu_p50_pct"] == 80.0  # m6: no idle lead-in
    assert sampler.summarize(timed)["cpu_p50_pct"] == 2.0
    assert sampler.percentile([], 0.5) is None and sampler.percentile([3.0], 0.95) == 3.0
