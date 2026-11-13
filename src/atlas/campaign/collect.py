"""Download a campaign root from the manager in one tar stream and verify every file.

The manager first writes `ARCHIVE-MANIFEST.sha256` (every regular file except `.runtime/`),
then streams the tree. The local copy is verified file by file against that manifest, and
`collect-report.json` records mismatches, missing and extra files; a clean collect has none.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shlex
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from ..onboarding.files import atomic_json, digest, private_dir, read_json
from .contracts import short_id
from .hostkeys import known_hosts, known_hosts_option

MANIFEST = "ARCHIVE-MANIFEST.sha256"
REPORT = "collect-report.json"
CAMPAIGNS_ROOT = "/opt/proxy-index-campaign/campaigns"
SPACE_FACTOR = 1.25
FREE_FLOOR = 0.20
# Runs with the manager's system Python (3.11 or newer, for `hashlib.file_digest`). Prints
# the manifest it wrote, then the total size.
REMOTE_MANIFEST = r"""
import hashlib, os, pathlib, sys
if sys.version_info < (3, 11):
    raise SystemExit("the archive manifest needs Python 3.11 or newer on the manager")
root = pathlib.Path(sys.argv[1]); lines = []; size = 0
for path in sorted(root.rglob("*")):
    rel = path.relative_to(root)
    if rel.parts[0] == ".runtime" or rel.as_posix() == "ARCHIVE-MANIFEST.sha256":
        continue
    if path.is_symlink():
        raise SystemExit("campaign root may not contain symbolic links")
    if path.is_file():
        with path.open("rb") as stream:
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
        lines.append(sha + "  ./" + rel.as_posix())
        size += path.stat().st_size
text = "".join(line + "\n" for line in lines)
temporary = root / ".ARCHIVE-MANIFEST.sha256.partial"
temporary.write_text(text); os.replace(temporary, root / "ARCHIVE-MANIFEST.sha256")
sys.stdout.write(text + "size " + str(size) + "\n")
"""

Spawn = Callable[[str], subprocess.Popen]


def load_inventory(path: Path) -> dict:
    """The manager entry of the private local inventory: `host`, `identity_file`, `user`."""
    data = read_json(path)
    ipaddress.ip_address(data["host"])
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", data.get("user", "root")):
        raise ValueError("Invalid SSH user")
    if not PurePosixPath(data.get("campaigns_root", CAMPAIGNS_ROOT)).is_absolute():
        raise ValueError("Remote campaigns root must be absolute")
    return data


def ssh_command(inventory: dict, path: Path) -> list[str]:
    """The SSH prefix for one manager command; the host appears only in this argv.

    With the inventory's ``known_hosts`` (written by ``manager deploy``, found beside the
    inventory at `path`) the manager's key is checked strictly against that file; without it
    the user's own known-hosts file applies.
    """
    pinned = []
    if inventory.get("known_hosts"):
        pinned = [
            "-o",
            known_hosts_option(known_hosts(path, inventory)),
            "-o",
            "StrictHostKeyChecking=yes",
        ]
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout=15",
        *pinned,
        "-i",
        os.path.expanduser(inventory["identity_file"]),
        f"{inventory.get('user', 'root')}@{inventory['host']}",
    ]


def parse_manifest(text: str) -> tuple[dict[str, str], int]:
    """`{relative path: sha256}` and the total size from the remote script's output."""
    *lines, size = text.splitlines()
    if not size.startswith("size "):
        raise ValueError("Remote manifest output is incomplete")
    files = {}
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  \./(.+)", line)
        if not match or ".." in PurePosixPath(match[2]).parts:
            raise ValueError("Invalid archive manifest line")
        files[match[2]] = match[1]
    return files, int(size.removeprefix("size "))


def check_space(dest: Path, size: int) -> None:
    """Free space must hold the archive 1.25 times over and keep 20% of the disk free."""
    usage = shutil.disk_usage(dest)
    if usage.free < size * SPACE_FACTOR or usage.free - size < usage.total * FREE_FLOOR:
        raise ValueError("Not enough local disk space for this campaign archive")


def _run(spawn: Spawn, command: str) -> bytes:
    with spawn(command) as process:
        output = process.stdout.read()
    if process.returncode:
        raise RuntimeError("Remote archive manifest failed")
    return output


def _extract(spawn: Spawn, command: str, dest: Path) -> None:
    with spawn(command) as process:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            # The data filter rejects absolute paths, `..`, links out of the tree and devices.
            archive.extractall(dest, filter="data")
        process.stdout.read()
    if process.returncode:
        raise RuntimeError("Remote archive stream failed")


def verify_tree(dest: Path, expected: dict[str, str]) -> dict:
    """Compare every local file with the manifest; returns the collect report."""
    local = {
        p.relative_to(dest).as_posix(): p for p in dest.rglob("*") if p.is_file() or p.is_symlink()
    }
    local.pop(REPORT, None)
    local.pop(MANIFEST, None)
    mismatches = sorted(
        name
        for name, sha in expected.items()
        if name in local and (local[name].is_symlink() or digest(local[name]) != sha)
    )
    missing = sorted(set(expected) - set(local))
    extra = sorted(set(local) - set(expected))
    return {
        "files": len(expected),
        "verified": len(expected) - len(mismatches) - len(missing),
        "mismatches": mismatches,
        "missing": missing,
        "extra": extra,
        "clean": not (mismatches or missing or extra),
    }


def collect(inventory: Path, cid: str, dest: Path, *, spawn: Spawn | None = None) -> dict:
    """Download campaign `cid` from the manager into a new `dest` and verify it.

    `spawn(command)` starts one manager shell command with its stdout piped (SSH from the
    inventory by default). Refuses a non-empty `dest`. Writes `collect-report.json` in `dest`
    and returns it; `clean` is true only with 0 mismatches, 0 missing and 0 extra files.
    """
    short_id(cid)
    server = load_inventory(inventory)
    if spawn is None:
        prefix = ssh_command(server, inventory)

        def spawn(command: str) -> subprocess.Popen:
            return subprocess.Popen([*prefix, command], stdout=subprocess.PIPE)

    remote = f"{server.get('campaigns_root', CAMPAIGNS_ROOT).rstrip('/')}/{cid}"
    if dest.exists() and any(dest.iterdir()):
        raise ValueError("Destination already holds files; collect into a new directory")
    private_dir(dest)
    output = _run(spawn, shlex.join(["python3", "-c", REMOTE_MANIFEST, remote]))
    expected, size = parse_manifest(output.decode())
    check_space(dest, size)
    _extract(
        spawn, shlex.join(["tar", "-C", remote, "--exclude", "./.runtime", "-cf", "-", "."]), dest
    )
    report = {"schema_version": 1, "campaign_id": cid, "bytes": size} | verify_tree(dest, expected)
    downloaded = dest / MANIFEST
    if not downloaded.is_file() or downloaded.read_text() != output.decode().rsplit("size ", 1)[0]:
        report["mismatches"].append(MANIFEST)
        report["clean"] = False
    atomic_json(dest / REPORT, report)
    return report
