"""Build the worker snapshot once: builder droplet, provision, snapshot, copy to every region.

The builder is created with every ownership marker, moved into project Atlas (fail
closed) and, whatever happens, removed through the scope guard at the end. Every id is written
to the local inventory (0600) before the next step.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import secrets
import subprocess
import tarfile
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from ..enrichment import LocalEnricher
from ..onboarding.files import private_dir
from ..regions import REGION_IDS
from . import ops_api
from .contracts import BUILDER_TAG, PROJECT_NAME, Droplet, Scope
from .do_api import CreateError, DigitalOcean

BUILD_REGION, BUILDER_SIZE, BASE_IMAGE = "lon1", "s-1vcpu-1gb", "ubuntu-24-04-x64"
REMOTE_DIR = "/root/atlas-camp-build"
SSH_TRIES = 30
# (argv, stdin bytes or a file streamed to stdin) -> (exit code, stdout)
Shell = Callable[[list[str], bytes | Path], Awaitable[tuple[int, str]]]


async def run_shell(argv: list[str], stdin: bytes | Path) -> tuple[int, str]:
    if isinstance(stdin, Path):  # the payload with a database is too large to hold in memory
        with stdin.open("rb") as source:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=source,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await process.communicate()
    else:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await process.communicate(stdin)
    return process.returncode or 0, out.decode(errors="replace")


@dataclass(frozen=True)
class ImageJob:
    """The build inputs: `uv build` and `uv export` output from the exact main head."""

    wheel: Path
    requirements: Path
    mac_key: Path  # private key; its `.pub` is registered as atlas-camp-mac-<date>
    inventory: Path
    regions: tuple[str, ...] = tuple(REGION_IDS)
    # The licensed DB-IP Location + ISP MMDB for the run-time country check (CTRY-2); placed
    # at /opt/atlas-worker/db-ip/<file name> in the image.
    mmdb: Path | None = None


@dataclass
class Build:
    started_at: datetime
    state: str = "started"
    values: dict[str, Any] = field(default_factory=dict)


def snapshot_name(wheel: Path, sha256: str, day: datetime) -> str:
    version = wheel.name.split("-")[1]
    return f"atlas-camp-worker-{version}-{sha256[:12]}-{day:%Y%m%d}"


def record(job: ImageJob, build: Build, **values: Any) -> None:
    """Write this build into the inventory, replacing the file atomically (0600)."""
    build.values.update(values)
    private_dir(job.inventory.parent)
    current = json.loads(job.inventory.read_text()) if job.inventory.exists() else {}
    builds = [
        b for b in current.get("builds", []) if b["started_at"] != build.started_at.isoformat()
    ]
    builds.append(
        {"started_at": build.started_at.isoformat(), "state": build.state, **build.values}
    )
    temporary = job.inventory.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({**current, "builds": builds}, handle, indent=2, sort_keys=True)
    temporary.replace(job.inventory)


def database(path: Path) -> dict[str, str]:
    """The build record of a DB-IP MMDB: refuses a file that does not open as one."""
    if path.suffix != ".mmdb":
        raise ValueError("--mmdb must name a .mmdb file")
    with LocalEnricher(path) as enricher:
        release = enricher.release
    if not str(release["database_type"]).lower().startswith("dbip"):
        raise ValueError("--mmdb is not a DB-IP database")
    return {"mmdb_file": release["file"], "mmdb_sha256": release["sha256"]}


def payload(job: ImageJob, destination: Path) -> Path:
    """Write the tar sent on SSH stdin: the provision script, the requirements, the wheel and,
    when given, the MMDB (streamed from disk, never held in memory)."""
    script = files("atlas.campaign").joinpath("assets", "worker-provision.sh")
    with tarfile.open(destination, mode="w") as archive:
        for name, data in (
            ("worker-provision.sh", script.read_bytes()),
            ("requirements.txt", job.requirements.read_bytes()),
            (job.wheel.name, job.wheel.read_bytes()),
        ):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o700 if name.endswith(".sh") else 0o600
            archive.addfile(info, io.BytesIO(data))
        if job.mmdb:  # by its own name, as recorded in `mmdb_file`
            database_file = job.mmdb.resolve()
            info = tarfile.TarInfo(database_file.name)
            info.size, info.mode = database_file.stat().st_size, 0o600
            with database_file.open("rb") as source:
                archive.addfile(info, source)
    return destination


async def provision(
    shell: Shell, job: ImageJob, address: str, runtime: Path, sleep: ops_api.Sleep
) -> None:
    """Wait for SSH, push the inputs and run the provision script; it must end with `clean`."""
    known_hosts = runtime / "known_hosts"
    ssh = [
        "ssh",
        "-i",
        str(job.mac_key),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "ConnectTimeout=10",
        f"root@{address}",
    ]
    for _ in range(SSH_TRIES):
        if (await shell([*ssh, "true"], b""))[0] == 0:
            break
        await sleep(ops_api.POLL_SECONDS)
    else:
        raise ops_api.OpsError("builder SSH never became ready")
    command = (
        f"mkdir -m 700 {REMOTE_DIR} && tar -x -C {REMOTE_DIR} && "
        f"bash {REMOTE_DIR}/worker-provision.sh"
    )
    code, out = await shell([*ssh, command], payload(job, runtime / "payload.tar"))
    verdict = (out.strip().splitlines() or [""])[-1]
    if code != 0 or verdict != "clean":
        raise ops_api.OpsError(f"provision failed ({code}): {verdict[:200]}")


async def build(
    cloud: DigitalOcean,
    job: ImageJob,
    *,
    shell: Shell = run_shell,
    sleep: ops_api.Sleep = asyncio.sleep,
    now: datetime | None = None,
) -> dict:
    """Run the whole build; returns the inventory entry. The builder never outlives it."""
    current = Build(now or datetime.now(UTC))
    scope = ops_api.ops_scope(await cloud.project_id(PROJECT_NAME), current.started_at)
    steps = _Steps(cloud, job, current, scope, shell, sleep)
    sha256 = hashlib.sha256(job.wheel.read_bytes()).hexdigest()
    country_database = database(job.mmdb) if job.mmdb else {}  # checked before any cloud write
    key_id, _, key_created = await ops_api.ensure_ssh_key(
        cloud,
        f"atlas-camp-mac-{current.started_at:%Y%m%d}",
        job.mac_key.with_name(job.mac_key.name + ".pub").read_text(),
    )
    steps.note(
        wheel_sha256=sha256, mac_key_id=key_id, mac_key_created=key_created, **country_database
    )
    try:
        builder = await steps.create_builder(key_id)
        await steps.provision_and_snapshot(
            builder, snapshot_name(job.wheel, sha256, current.started_at)
        )
        current.state = "built"
    except Exception as error:
        current.state = "failed"
        steps.note(error=f"{type(error).__name__}: {error}"[:300])
        raise
    finally:
        await steps.cleanup(key_id if key_created else None)
    return current.values | {"state": current.state}


@dataclass
class _Steps:
    cloud: DigitalOcean
    job: ImageJob
    build: Build
    scope: Scope
    shell: Shell
    sleep: ops_api.Sleep
    builder_id: str | None = None
    unresolved: str | None = None  # a builder name whose create outcome stayed unknown

    @property
    def audit(self) -> Path:
        return self.job.inventory.with_name("campaign-ops-audit.jsonl")

    def note(self, **values: Any) -> None:
        record(self.job, self.build, **values)

    async def create_builder(self, key_id: str) -> Droplet:
        """Create the builder; after a lost answer, adopt it by its exact name and tag."""
        name = f"atlas-camp-builder-{self.build.started_at:%Y%m%d}-{secrets.token_hex(2)}"
        self.note(builder_name=name)
        try:
            [builder] = await self.cloud.create(
                [name],
                region=BUILD_REGION,
                size=BUILDER_SIZE,
                image=BASE_IMAGE,
                tags=[BUILDER_TAG],
                ssh_keys=[key_id],
            )
        except CreateError as error:
            adopted = (
                await self.cloud.adopt_uncertain([name], BUILDER_TAG) if (error.uncertain) else []
            )
            if not adopted:
                self.unresolved = name if error.uncertain else None
                raise
            builder = adopted[0]
        self.builder_id = builder.id
        self.note(builder_id=builder.id)
        return builder

    async def provision_and_snapshot(self, builder: Droplet, name: str) -> None:
        cloud, sleep = self.cloud, self.sleep
        await ops_api.assign(cloud, self.scope, builder, self.audit, "builder")
        ready = await ops_api.wait_droplet(cloud, builder.id, "active", sleep)
        with tempfile.TemporaryDirectory(prefix="atlas-camp-build-") as runtime:
            await provision(self.shell, self.job, ready.public_ipv4, Path(runtime), sleep)
        image = await ops_api.snapshot(cloud, self.scope, builder.id, name, sleep)
        self.note(snapshot_id=str(image["id"]), snapshot_name=name)
        self.note(
            regions=await ops_api.transfer(cloud, str(image["id"]), list(self.job.regions), sleep)
        )

    async def cleanup(self, key_id: str | None) -> None:
        """Remove the builder, then the key made for this build; record what stayed behind."""
        # The listing may lag a lost create: check `campaign limits` before building again.
        left = [f"builder_unresolved: {self.unresolved}"] if self.unresolved else []
        for kind, resource_id in (("droplet", self.builder_id), ("ssh_key", key_id)):
            if resource_id is None:
                continue
            try:
                await ops_api.release(self.cloud, kind, resource_id, self.scope, self.audit)
            except Exception as error:  # boundary: audited by the guard; the other one still goes
                left.append(f"{kind} {resource_id}: {type(error).__name__}")
        self.note(leftovers=left)
        if left and self.build.state == "built":
            raise ops_api.OpsError(f"build finished but left {left}")


def ensure_mac_key(path: Path) -> Path:
    """The Mac's campaign key (ed25519, no passphrase prompt, no host name in its comment)."""
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "atlas-camp-mac", "-f", str(path)],
            check=True,
        )
    return path
