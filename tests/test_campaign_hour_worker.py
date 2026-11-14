"""The hour engine on the worker: the OpenSSH fan-out, its pulls and the pull check.

No SSH connection leaves the machine: a fake runner answers. Addresses are RFC 5737
documentation addresses. The start script and the sampler are in
`test_campaign_hour_worker_start.py`.
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
from campaign_hour_worker_cases import member, tar_bytes

from atlas.campaign import fanout
from atlas.campaign.contracts import WORKER_ROOT, Droplet

# --- OpenSSH fan-out -------------------------------------------------------------------------


def test_the_openssh_options(tmp_path):
    """11: batch mode, the droplet's own known hosts (PULSE-KNOWNHOSTS-1), accept-new, control
    master, its errors to a log beside them, no secret in argv."""
    runtime = tmp_path / ".runtime"
    argv = fanout.OpenSSHRemote(tmp_path / "key", runtime, 7).argv(DROPLET, ["true"])
    options = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-o")
    hosts = runtime / "known_hosts" / DROPLET.id
    assert argv[argv.index("-E") + 1] == str(hosts) + ".log"
    assert options == {
        "BatchMode": "yes",
        "IdentitiesOnly": "yes",
        "User": "root",
        "UserKnownHostsFile": str(hosts),
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
    long = Path("/opt/atlas-campaign/campaigns") / CID / ".runtime"
    assert fanout.control_dir(long) == Path("/tmp") / f"atlas-camp-cm-{os.getuid()}"
    with pytest.raises(fanout.RemoteError):
        fanout.OpenSSHRemote(tmp_path, runtime, 0).argv(
            Droplet("1", "n", "lon1", "c-2", "new", DROPLET.created_at, ()), ["true"]
        )


async def test_the_remote_steps_send_fixed_commands(tmp_path):
    runtime = tmp_path / "rt"
    inspect = json.dumps({"assignment": {"unit": "atlas-camp-round"}}).encode()
    runner = FakeRunner((0, b""), (0, inspect), (0, b""), (0, b"journal"))
    remote = fanout.OpenSSHRemote(tmp_path / "key", runtime, 0, runner=runner)
    await remote.push_start(DROPLET, b"payload", fanout.start_command("m", "lon1", 0))
    assert (await remote.inspect(DROPLET))["assignment"]["unit"] == "atlas-camp-round"
    await remote.stop(DROPLET)
    await remote.journal(DROPLET, tmp_path / "logs" / "net00-lon1.journal.txt")
    commands = [call[0][-1] for call in runner.calls]
    assert runner.calls[0][1] == b"payload" and WORKER_ROOT in commands[0]
    assert commands[2] == "systemctl kill --signal=SIGTERM atlas-camp-round"
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
        "mkdir -p /opt/atlas-worker/run/m/regions/lon1/rounds/00 && tar -C "
        "/opt/atlas-worker/run/m/regions/lon1/rounds -cf - 00" in runner.calls[0][0][-1]
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
