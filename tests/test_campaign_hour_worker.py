"""The hour engine on the worker: OpenSSH fan-out, the start script and the worker sampler.

No SSH connection leaves the machine: a fake runner answers. Addresses are RFC 5737
documentation addresses.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from campaign_fakes import CID
from campaign_hour_cases import DROPLET, FakeClock, FakeRunner, stub_systemctl

from proxy_index.campaign import fanout, sampler
from proxy_index.campaign.contracts import WORKER_ROOT, WORKER_SECRETS, Droplet

# --- OpenSSH fan-out -------------------------------------------------------------------------


def test_the_openssh_options(tmp_path):
    """11: batch mode, per-hour known hosts, accept-new, control master, no secret in argv."""
    runtime = tmp_path / ".runtime"
    argv = fanout.OpenSSHRemote(tmp_path / "key", runtime, 7).argv(DROPLET, ["true"])
    options = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-o")
    assert options == {
        "BatchMode": "yes",
        "IdentitiesOnly": "yes",
        "User": "root",
        "UserKnownHostsFile": str(runtime / "known_hosts-07"),
        "StrictHostKeyChecking": "accept-new",
        "ConnectTimeout": "5",
        "ServerAliveInterval": "10",
        "ServerAliveCountMax": "3",
        "ControlMaster": "auto",
        "ControlPath": str(fanout.control_dir(runtime) / "cm-%C"),
        "ControlPersist": "4h",
    }
    assert argv[:3] == ["ssh", "-i", str(tmp_path / "key")] and argv[-3:] == [
        "203.0.113.7",
        "--",
        "true",
    ]
    assert fanout.control_dir(Path("/r")) == Path("/r")
    long = Path("/opt/proxy-index-campaign/campaigns") / CID / ".runtime"
    assert fanout.control_dir(long) == Path("/tmp") / f"pi-camp-cm-{os.getuid()}"
    with pytest.raises(fanout.RemoteError):
        fanout.OpenSSHRemote(tmp_path, runtime, 0).argv(
            Droplet("1", "n", "lon1", "c-2", "new", DROPLET.created_at, ()), ["true"]
        )


async def test_the_remote_steps_send_fixed_commands(tmp_path):
    runtime = tmp_path / "rt"
    inspect = json.dumps({"assignment": {"unit": "pi-camp-round"}}).encode()
    runner = FakeRunner((0, b""), (0, inspect), (0, b""), (0, b"journal"))
    remote = fanout.OpenSSHRemote(tmp_path / "key", runtime, 0, runner=runner)
    await remote.push_start(DROPLET, b"payload", fanout.start_command("m", "lon1", 0))
    assert (await remote.inspect(DROPLET))["assignment"]["unit"] == "pi-camp-round"
    await remote.stop(DROPLET)
    await remote.journal(DROPLET, tmp_path / "logs" / "net00-lon1.journal.txt")
    commands = [call[0][-1] for call in runner.calls]
    assert runner.calls[0][1] == b"payload" and WORKER_ROOT in commands[0]
    assert commands[2] == "systemctl kill --signal=SIGTERM pi-camp-round"
    assert "tail -c 5000000" in commands[3]
    journal = tmp_path / "logs" / "net00-lon1.journal.txt"
    assert journal.read_bytes() == b"journal" and stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
    with pytest.raises(fanout.RemoteError, match=r"^inspect failed \(exit 255\)$"):
        await fanout.OpenSSHRemote(
            tmp_path, runtime, 0, runner=FakeRunner((255, b"203.0.113.7 refused"))
        ).inspect(DROPLET)


async def test_wait_ready_retries_until_its_limit(tmp_path):
    clock = FakeClock(0.0)
    runner = FakeRunner((255, b""), (255, b""), (0, b""))
    remote = fanout.OpenSSHRemote(
        tmp_path, tmp_path / "rt", 0, runner=runner, sleep=clock.sleep, clock=clock.now
    )
    assert await remote.wait_ready(DROPLET, 30) and clock.t == 4.0
    never = fanout.OpenSSHRemote(
        tmp_path,
        tmp_path / "rt",
        0,
        runner=FakeRunner(*[(255, b"")] * 100),
        sleep=clock.sleep,
        clock=clock.now,
    )
    assert not await never.wait_ready(DROPLET, 10)


def tar_bytes(*members) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def member(name, data=b"x", kind=tarfile.REGTYPE, link=""):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, link
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    return info, data if kind == tarfile.REGTYPE else None


async def test_pull_replaces_the_local_round_with_the_remote_one(tmp_path):
    local = tmp_path / "regions" / "lon1" / "rounds" / "00"
    local.mkdir(parents=True)
    (local / "stale.json").write_text("{}")
    archive = tar_bytes(
        member("00/round.json", b"{}"),
        member("00/us/summary.json", b"{}"),
        member("00/host-samples.jsonl", b""),
    )
    runner = FakeRunner((0, archive))
    remote = fanout.OpenSSHRemote(tmp_path, tmp_path / "rt", 0, runner=runner)
    await remote.pull(DROPLET, f"{WORKER_ROOT}/run/m/regions/lon1/rounds/00", local)
    assert sorted(p.name for p in local.rglob("*")) == [
        "host-samples.jsonl",
        "round.json",
        "summary.json",
        "us",
    ]
    assert (
        "mkdir -p /opt/proxy-index-worker/run/m/regions/lon1/rounds/00 && tar -C "
        "/opt/proxy-index-worker/run/m/regions/lon1/rounds -cf - 00" in runner.calls[0][0][-1]
    )
    assert sorted(p.name for p in local.parent.iterdir()) == ["00"]


@pytest.mark.parametrize(
    "bad",
    [
        member("/etc/passwd"),
        member("../escape"),
        member("00/../../escape"),
        member("00/link", kind=tarfile.SYMTYPE, link="/etc/passwd"),
        member("00/inner", kind=tarfile.SYMTYPE, link="round.json"),
        member("00/hard", kind=tarfile.LNKTYPE, link="00/round.json"),
        member("00/device", kind=tarfile.CHRTYPE),
    ],
)
async def test_unsafe_tar_entries_are_rejected(tmp_path, bad):
    """12: absolute paths, `..`, links and devices never land on the manager."""
    local = tmp_path / "rounds" / "00"
    runner = FakeRunner((0, tar_bytes(member("00/round.json", b"{}"), bad)))
    remote = fanout.OpenSSHRemote(tmp_path, tmp_path / "rt", 0, runner=runner)
    with pytest.raises(tarfile.TarError):
        await remote.pull(DROPLET, f"{WORKER_ROOT}/run/m/regions/lon1/rounds/00", local)
    assert not local.exists() and not (tmp_path / "escape").exists()
    assert list(local.parent.iterdir()) == []


async def test_run_process_streams_caps_and_times_out():
    """m8: a worker can never send more than the cap; the process is killed."""
    sink = io.BytesIO()
    echo = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
    assert await fanout.run_process(echo, b"hello", 30, sink, 10) == 0
    assert sink.getvalue() == b"hello"
    flood = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1_000_000)"]
    assert await fanout.run_process(flood, None, 30, io.BytesIO(), 1000) == 254
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    assert await fanout.run_process(sleeper, None, 0.2, io.BytesIO(), 10) == 255
    gone = [sys.executable, "-c", "pass"]
    assert await fanout.run_process(gone, b"x" * 5_000_000, 30, io.BytesIO(), 10) == 0


async def test_run_process_ends_when_killed_with_unread_output():
    """A child killed with more unread stdout than the reader buffers (which pauses the pipe)
    is still reaped: wait() returns only once stdout reaches EOF. The child writes before it
    reads, so the stdin write never drains, stdout fills and the timeout always fires."""
    flood_first = "import sys; sys.stdout.write('x' * 1_000_000); sys.stdin.read()"
    stuck = [sys.executable, "-c", flood_first]
    run = fanout.run_process(stuck, b"y" * 1_000_000, 0.5, io.BytesIO(), 10)
    assert await asyncio.wait_for(run, 10) == 255


async def test_oversized_worker_output_is_refused(tmp_path):
    runner = FakeRunner((0, b"x" * (fanout.OUTPUT_BYTES + 1)))
    remote = fanout.OpenSSHRemote(tmp_path, tmp_path / "rt", 0, runner=runner)
    with pytest.raises(fanout.RemoteError, match="exit 254"):
        await remote.inspect(DROPLET)


def test_a_control_directory_of_another_user_is_refused(tmp_path, monkeypatch):
    """m4: the multiplexed channel never uses a directory another user planted."""
    planted = tmp_path / "planted"
    planted.mkdir()
    fanout.owned_dir(planted)
    monkeypatch.setattr(fanout.os, "getuid", lambda: planted.stat().st_uid + 1)
    with pytest.raises(fanout.RemoteError, match="another user"):
        fanout.owned_dir(planted)


@pytest.mark.parametrize(
    "checkpoint,unit,allowed",
    [
        ("running", "ActiveState=active\nSubState=running", False),
        ("armed", "ActiveState=activating\nSubState=start", False),
        ("running", None, False),  # systemctl itself failed: keep refusing (data first)
        ("running", "ActiveState=failed\nSubState=failed", True),  # killed round (B2)
        ("running", "ActiveState=inactive\nSubState=dead", True),  # unit gone after a reboot
        ("running", "ActiveState=active\nSubState=exited", True),
        ("completed", "ActiveState=active\nSubState=running", True),
        (None, "ActiveState=failed\nSubState=failed", True),
    ],
)
def test_b2_the_pull_check_allows_a_round_whose_unit_ended(tmp_path, checkpoint, unit, allowed):
    """B2: a hard-killed round (checkpoint never terminal) can still be pulled."""
    folder = tmp_path / "00"
    folder.mkdir()
    if checkpoint:
        (folder / "round.json").write_text(json.dumps({"status": checkpoint}))
    reply = subprocess.run(
        [sys.executable, "-c", fanout.TERMINAL_CHECK, str(folder)],
        env=stub_systemctl(tmp_path, unit),
        check=False,
    )
    assert (reply.returncode == 0) is allowed


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
