"""OpenSSH fan-out to workers: one control master per worker, inputs only on stdin.

Secrets travel only inside the stdin tar; `argv` holds the host address and fixed, non-secret
arguments. Host addresses live in memory, in `argv` and under `.runtime/` only.

Host keys (SEC-SSH-PIN-1): a droplet this machine created has its key pinned at creation
(`hostkeys`) and is reached only with that key (`StrictHostKeyChecking=yes`). One created before
this machine's first pin keeps trust on first use on its own `.runtime/known_hosts/<droplet id>`
(PULSE-KNOWNHOSTS-1: DigitalOcean reuses addresses, never ids); any other is refused. A changed
key is never accepted. The ssh errors go to `<id>.log` (`-E`), which `wait_ready` reads for
`ready_failure`; a file unused for KNOWN_HOSTS_DAYS (each successful call touches it) is pruned.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shlex
import shutil
import tarfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from ..onboarding.files import private_dir
from ..onboarding.fleet import REMOTE_INSPECT
from . import hostkeys
from .contracts import WORKER_ROOT, WORKER_SECRETS, Droplet

ROUND_UNIT, SAMPLER_UNIT = "atlas-camp-round", "atlas-camp-sampler"
JOURNAL_BYTES = 5_000_000
OUTPUT_BYTES, PULL_BYTES = 16 << 20, 8 << 30  # caps on what one worker may send (m8)
READY_POLL_SECONDS = 2.0
MMDB_DIR = f"{WORKER_ROOT}/db-ip"  # the image's DB-IP database, if any (`image build --mmdb`)
SOCKET_LIMIT = 104 - 17  # sun_path on macOS, less the suffix ssh adds while it binds
KNOWN_HOSTS_DAYS = 2  # a droplet's known_hosts file and ssh log are kept this long
# A failed ssh's -E log: the first match is the reason.
REFUSALS = (
    ("host_key_changed", ("REMOTE HOST IDENTIFICATION HAS CHANGED", "Host key verification")),
    ("auth_failed", ("Permission denied",)),
    ("unreachable", ("timed out", "Connection refused", "No route to host", "closed by")),
)
Runner = Callable[[list[str], bytes | None, float, BinaryIO, int], Awaitable[int]]

# Worker script lines: install `wheel` into the image's venv under `root` unless that exact
# wheel is installed already. Shared by the round start and `campaign probe` (one install path).
# The image holds the dependencies (the wheel goes in --no-deps): `uv pip check` stops the start
# when the new wheel needs one the image lacks (IMAGE-AUTO-1), so no test runs on broken code.
INSTALL_WHEEL = r"""sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
marker = root / "installed-wheel.sha256"
installed = marker.read_text().strip() if marker.exists() else ""
python = str(root / "venv" / "bin" / "python")
if sha != installed:
    subprocess.run(["uv", "pip", "install", "--python", python, "--no-deps", "--offline",
                    str(wheel)], check=True)
    if subprocess.run(["uv", "pip", "check", "--python", python], capture_output=True).returncode:
        sys.exit("the image's dependencies do not match the wheel: rebuild the image")
    marker.write_text(sha + "\n")
"""

# Runs with the worker's system Python: argv = root, secrets dir, mcid, region, round.
START_SCRIPT = (
    r"""
import hashlib, io, json, os, pathlib, subprocess, sys, tarfile, time
root, secrets = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
mcid, region, hour = sys.argv[3], sys.argv[4], int(sys.argv[5])
archive = tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()), mode="r:")
secret = archive.extractfile("secrets/credentials.toml").read()
secrets.mkdir(mode=0o700, parents=True, exist_ok=True)
fd = os.open(secrets / "credentials.toml", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, secret); os.close(fd)
members = [m for m in archive.getmembers() if m.name.startswith(("run/", "wheel/"))]
archive.extractall(root, members=members, filter="data")
wheel = next(root / m.name for m in members if m.name.endswith(".whl"))
databases = sorted((root / "db-ip").glob("*.mmdb"))  # exactly one: the round's country check
country_check = ["--mmdb", str(databases[0])] if len(databases) == 1 else []
"""
    + INSTALL_WHEEL
    + r"""for _ in range(11):
    synced = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                            capture_output=True, text=True).stdout.strip() == "yes"
    if synced:
        break
    time.sleep(1)
run = root / "run" / mcid
folder = run / "regions" / region / "rounds" / f"{hour:02d}"
folder.mkdir(mode=0o700, parents=True, exist_ok=True)
(folder / "worker-start.json").write_text(json.dumps(
    {"wheel_installed": sha != installed, "ntp_synchronized": synced, "time": time.time()}))
(root / "active-run.json").write_text(json.dumps(
    {"kind": "round", "region": region, "unit": "atlas-camp-round",
     "state_path": str((folder / "round.json").relative_to(root))}))
subprocess.run(["systemd-run", "--unit=atlas-camp-round", "--property=LimitNOFILE=65536",
                "--property=UMask=0077", "--property=Restart=no",
                "--property=RemainAfterExit=yes", f"--property=WorkingDirectory={root}",
                str(root / "venv" / "bin" / "atlas"), "onboarding", "round",
                "--manifest", str(run / "campaign.json"), "--region", region,
                "--round", str(hour), "--provider-config", str(run / "provider.toml"),
                "--credentials-file", str(secrets / "credentials.toml"), *country_check],
               check=True)
subprocess.run(["systemd-run", "--unit=atlas-camp-sampler", "--property=Restart=no", python,
                "-m", "atlas.campaign.sampler", "--interval", "1",
                "--output", str(folder / "host-samples.jsonl")], check=True)
"""
)

# A round directory is pulled once its checkpoint is terminal or the round unit has ended (a
# killed round never writes a terminal checkpoint, B2). An unknown unit state refuses.
TERMINAL_CHECK = r"""
import json, pathlib, subprocess, sys
path = pathlib.Path(sys.argv[1]) / "round.json"
if path.exists() and json.loads(path.read_text())["status"] in ("completed", "stopped", "failed"):
    sys.exit(0)
reply = subprocess.run(["systemctl", "show", "atlas-camp-round", "--property=ActiveState,SubState"],
                       capture_output=True, text=True)
unit = dict(line.split("=", 1) for line in reply.stdout.splitlines() if "=" in line)
ended = unit.get("ActiveState") in ("inactive", "failed") or unit.get("SubState") == "exited"
sys.exit(0 if reply.returncode == 0 and ended else 3)
"""


class RemoteError(RuntimeError):
    """An SSH step failed; the message is fixed text with no host address or output."""


def start_command(mcid: str, region: str, hour: int) -> list[str]:
    """The fixed worker command; every argument is public."""
    return ["python3", "-c", START_SCRIPT, WORKER_ROOT, WORKER_SECRETS, mcid, region, str(hour)]


def control_dir(runtime: Path) -> Path:
    """`runtime` when a control socket path fits, else a private per-user directory in /tmp."""
    if len(str(runtime / ("cm-" + "0" * 40))) <= SOCKET_LIMIT:
        return runtime
    return Path("/tmp") / f"atlas-camp-cm-{os.getuid()}"


def known_hosts_file(runtime: Path, droplet_id: str) -> Path:
    """The droplet's own known_hosts file (its ssh log is the same path with `.log`)."""
    return runtime / "known_hosts" / str(droplet_id)


def refusal(log: Path) -> str:
    """Why an ssh failed, from its -E log: one of REFUSALS, else `ssh_failed`."""
    text = log.read_text(errors="replace") if log.is_file() else ""
    return next((r for r, marks in REFUSALS if any(m in text for m in marks)), "ssh_failed")


def boot_reason(remote, droplet: Droplet | None) -> str:
    """Why a droplet did not boot: `not_active`, else its `ready_failure` (or `ssh_failed`)."""
    found = remote.ready_failure(droplet) if droplet is not None else "not_active"
    return found or "ssh_failed"


def prune_known_hosts(runtime: Path, now: float) -> None:
    """Remove the droplets' known_hosts files and ssh logs older than KNOWN_HOSTS_DAYS."""
    for path in (runtime / "known_hosts").glob("*"):
        with contextlib.suppress(OSError):
            if now - path.stat().st_mtime > KNOWN_HOSTS_DAYS * 86400:
                path.unlink()


def host_key_options(runtime: Path, droplet: Droplet) -> list[str]:
    """The pinned key when this machine pinned one; else the droplet's own file, learned on
    first use only for a droplet older than this machine's first pin (`hostkeys.legacy`)."""
    pinned = hostkeys.pinned(droplet.name)
    if pinned is not None:
        return hostkeys.pinned_options(droplet.name, pinned)
    mode = "accept-new" if hostkeys.legacy(droplet.created_at) else "yes"
    hosts = known_hosts_file(runtime, droplet.id)
    return ["-o", f"UserKnownHostsFile={hosts}", "-o", f"StrictHostKeyChecking={mode}"]


def ssh_options(runtime: Path, droplet: Droplet) -> list[str]:
    hosts = known_hosts_file(runtime, droplet.id)
    options = {
        "BatchMode": "yes",
        "IdentitiesOnly": "yes",
        "User": "root",
        "ConnectTimeout": "5",
        "ServerAliveInterval": "10",
        "ServerAliveCountMax": "3",
        "ControlMaster": "auto",
        "ControlPath": str(control_dir(runtime) / "cm-%C"),
        "ControlPersist": "4h",
    }
    found = [item for key, value in options.items() for item in ("-o", f"{key}={value}")]
    log = str(hosts.with_name(hosts.name + ".log"))
    return [*found, *host_key_options(runtime, droplet), "-E", log]


def safe_extract(archive: Path, destination: Path, top: str) -> None:
    """Extract a pulled tar whose entries all lie under `top`; absolute paths, `..`, links and
    devices raise `tarfile.FilterError`."""

    def only_data(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo:
        parts = member.name.split("/")
        if member.name.startswith("/") or ".." in parts or parts[0] != top:
            raise tarfile.FilterError(f"entry outside {top}")
        if member.issym() or member.islnk():
            raise tarfile.FilterError("links are refused")
        return tarfile.data_filter(member, path)

    with tarfile.open(archive, mode="r:") as stream:
        stream.extractall(destination, filter=only_data)


async def _exchange(
    process: asyncio.subprocess.Process, stdin: bytes | None, sink: BinaryIO, limit: int
) -> int:
    """Write `stdin`, then stream at most `limit` stdout bytes to `sink`; 254 when longer."""
    with contextlib.suppress(ConnectionError):
        process.stdin.write(stdin or b"")
        await process.stdin.drain()
        process.stdin.close()
    size = 0
    while chunk := await process.stdout.read(1 << 16):
        size += len(chunk)
        if size > limit:
            return 254
        sink.write(chunk)
    return await process.wait()


async def run_process(
    argv: list[str], stdin: bytes | None, seconds: float, sink: BinaryIO, limit: int
) -> int:
    """Stream at most `limit` stdout bytes to `sink`; exit code, 255 timeout, 254 too long."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(seconds):
            return await _exchange(process, stdin, sink, limit)
    except TimeoutError:
        return 255
    finally:
        if process.returncode is None:
            process.kill()
        # wait() returns only once stdout reaches EOF, and a pipe left unread past the
        # reader's buffer is paused and never does: read the rest away first.
        while await process.stdout.read(1 << 16):
            pass
        await process.wait()


def owned_dir(path: Path) -> None:
    """A private directory of this user; one planted by another user is refused (m4)."""
    if path.exists() and path.lstat().st_uid != os.getuid():
        raise RemoteError("control directory belongs to another user")
    private_dir(path)


@dataclass(frozen=True)
class _Call:
    """One remote command's input, time limit, and where its output goes and how much of it."""

    stdin: bytes | None = None
    seconds: float = 120
    sink: BinaryIO | None = None
    limit: int = OUTPUT_BYTES


class OpenSSHRemote:
    """`Remote` over the system `ssh`, with one persistent control master per worker."""

    def __init__(
        self,
        identity: Path,
        runtime: Path,
        hour: int,
        *,
        runner: Runner = run_process,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.identity, self.runtime, self.hour = identity, runtime, hour
        self._run, self._sleep, self._clock = runner, sleep, clock
        self._pruned, self._failures = False, {}

    def argv(self, droplet: Droplet, command: list[str]) -> list[str]:
        if not droplet.public_ipv4:
            raise RemoteError("worker has no public address yet")
        return [
            "ssh",
            "-i",
            str(self.identity),
            *ssh_options(self.runtime, droplet),
            droplet.public_ipv4,
            "--",
            shlex.join(command),
        ]

    async def _call(
        self, droplet: Droplet, command: list[str], what: str, call: _Call | None = None
    ) -> bytes:
        """Run one remote command; its output goes to `sink`, or is returned when none."""
        call = call or _Call()
        owned_dir(self.runtime)
        owned_dir(control_dir(self.runtime))
        owned_dir(self.runtime / "known_hosts")
        if not self._pruned:
            prune_known_hosts(self.runtime, time.time())
            self._pruned = True
        buffer = call.sink or io.BytesIO()
        argv = self.argv(droplet, command)
        code = await self._run(argv, call.stdin, call.seconds, buffer, call.limit)
        if code != 0:
            raise RemoteError(f"{what} failed (exit {code})")
        pinned = hostkeys.pinned(droplet.name)
        for used in (known_hosts_file(self.runtime, droplet.id), *filter(None, [pinned])):
            with contextlib.suppress(OSError):  # a droplet in use keeps its key past the pruning
                os.utime(used)
        return b"" if call.sink else buffer.getvalue()

    async def run(
        self,
        droplet: Droplet,
        command: list[str],
        what: str,
        *,
        stdin: bytes | None = None,
        seconds: float = 120,
    ) -> bytes:
        """Run one fixed command (inputs on stdin only) and return its output."""
        return await self._call(droplet, command, what, _Call(stdin=stdin, seconds=seconds))

    async def wait_ready(self, droplet: Droplet, limit_seconds: float) -> bool:
        """Poll until a ready check passes or `limit_seconds`; a refused host key is polled like
        any failure and never replaced. `ready_failure` says why it failed."""
        deadline = self._clock() + limit_seconds
        hosts = known_hosts_file(self.runtime, droplet.id)
        log = hosts.with_name(hosts.name + ".log")
        while True:
            log.unlink(missing_ok=True)
            try:
                await self._call(droplet, ["true"], "ready check", _Call(seconds=15))
                self._failures.pop(droplet.id, None)
                return True
            except RemoteError:
                self._failures[droplet.id] = refusal(log)
            if self._clock() + READY_POLL_SECONDS >= deadline:
                return False
            await self._sleep(READY_POLL_SECONDS)

    def ready_failure(self, droplet: Droplet) -> str | None:
        """Why the droplet's last ready check failed (REFUSALS, `ssh_failed`); None if it passed."""
        return self._failures.get(droplet.id)

    async def push_start(self, droplet: Droplet, payload: bytes, command: list[str]) -> None:
        await self._call(droplet, command, "push and start", _Call(stdin=payload, seconds=300))

    async def inspect(self, droplet: Droplet) -> dict[str, Any]:
        out = await self._call(
            droplet, ["python3", "-c", REMOTE_INSPECT, WORKER_ROOT], "inspect", _Call(seconds=30)
        )
        return json.loads(out)

    async def stop(self, droplet: Droplet) -> None:
        """SIGTERM to the round's main process only: it stops its country and ends its HTTP
        worker processes itself; a SIGTERM to those as well failed the round (DAILY-BUDGET-STOP-1).
        `--kill-who` is the name every systemd release accepts."""
        command = ["systemctl", "kill", "--kill-who=main", "--signal=SIGTERM", ROUND_UNIT]
        await self._call(droplet, command, "stop")

    async def pull(self, droplet: Droplet, remote: str, local: Path) -> None:
        """Replace `local` with the remote round directory, once its state is terminal."""
        parent, name = remote.rstrip("/").rsplit("/", 1)
        where = shlex.quote(remote)  # an unstarted round's directory is pulled empty
        command = (
            f"python3 -c {shlex.quote(TERMINAL_CHECK)} {where} && mkdir -p {where} && "
            f"tar -C {shlex.quote(parent)} -cf - {shlex.quote(name)}"
        )
        private_dir(local.parent)
        archive, staging = (local.parent / f".{local.name}.pull{suffix}" for suffix in (".tar", ""))
        shutil.rmtree(staging, ignore_errors=True)
        try:
            with archive.open("wb") as sink:
                await self._call(
                    droplet,
                    ["sh", "-c", command],
                    "pull",
                    _Call(seconds=1800, sink=sink, limit=PULL_BYTES),
                )
            safe_extract(archive, staging, name)
            shutil.rmtree(local, ignore_errors=True)
            (staging / name).replace(local)
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)

    async def journal(self, droplet: Droplet, local: Path) -> None:
        command = (
            f"journalctl -u {ROUND_UNIT} -u {SAMPLER_UNIT} --no-pager -o short-iso"
            f" | tail -c {JOURNAL_BYTES}"
        )
        private_dir(local.parent)
        fd = os.open(local, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as sink:
            await self._call(
                droplet,
                ["sh", "-c", command],
                "journal",
                _Call(seconds=120, sink=sink, limit=JOURNAL_BYTES),
            )
