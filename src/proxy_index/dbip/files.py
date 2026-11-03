"""The installed releases in the destination directory: verification, the lock, the state
file, the atomic install behind the `current` link and pruning."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import maxminddb

from ..enrichment import CURRENT, release_record
from .source import PREFIX, Busy, UpdateError, private_file

STATE = "release.json"
LOCK = ".update.lock"
KEEP = 3  # the current release and the previous two


def inspect(path: Path, sha256: str, min_coverage: float) -> dict[str, Any]:
    """Open with the MaxMind reader; require a DB-IP type and countries for a spread of
    addresses (one per public /8). Returns the release record enrichment uses."""
    try:
        reader = maxminddb.open_database(str(path))
    except (OSError, ValueError, maxminddb.InvalidDatabaseError) as exc:
        raise UpdateError(f"verify: not a readable MMDB ({type(exc).__name__})") from None
    with reader:
        metadata = reader.metadata()
        if not str(metadata.database_type).lower().startswith("dbip"):
            raise UpdateError("verify: not a DB-IP database")
        probes = [f"{first}.1.2.3" for first in range(1, 224) if first not in (10, 127)]
        found = sum(1 for address in probes if _country(reader, address))
        if found < min_coverage * len(probes):
            raise UpdateError(f"verify: {found} of {len(probes)} test lookups found a country")
        return release_record(path, metadata, sha256)


def _country(reader, address: str) -> str | None:
    record = reader.get(address)
    country = record.get("country") if isinstance(record, dict) else None
    return country.get("iso_code") if isinstance(country, dict) else None


@contextmanager
def directory_lock(dest: Path):
    fd = os.open(dest / LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("another update is running in this directory") from None
        yield
    finally:
        os.close(fd)


def read_state(dest: Path) -> dict[str, Any]:
    """The installed release (release.json), or {} when nothing usable is installed."""
    try:
        state = json.loads((dest / STATE).read_text())
        current = (dest / CURRENT).resolve(strict=True)
    except (OSError, ValueError):
        return {}
    if current.name != state.get("file") or current.stat().st_size != state.get("size"):
        return {}
    return state


def write_json(path: Path, data: dict[str, Any]) -> None:
    partial = path.with_name(path.name + ".partial")
    with private_file(partial) as out:
        out.write(json.dumps(data, indent=2).encode())
    os.chmod(partial, 0o644)
    partial.replace(path)


def install(dest: Path, partial: Path, sha256: str, name: str) -> Path:
    """Rename the verified MMDB into place as `name` (another file of that name with other
    bytes is kept: the new one gets its SHA-256 prefix) and swing `current` to it, atomically."""
    target = dest / name
    if target.exists() and _sha256(target) != sha256:
        target = target.with_name(f"{target.stem}-{sha256[:8]}.mmdb")
    os.chmod(partial, 0o644)  # licensed data, not a secret: every local service reads it
    partial.replace(target)
    link = dest / f".{CURRENT}.partial"
    link.unlink(missing_ok=True)
    os.symlink(target.name, link)
    link.replace(dest / CURRENT)
    _sync_dir(dest)
    return target


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _sync_dir(dest: Path) -> None:
    fd = os.open(dest, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prune(dest: Path) -> list[str]:
    """Delete releases beyond the current one and the previous KEEP - 1; returns their names."""
    current = (dest / CURRENT).resolve()
    others = [
        p
        for p in dest.glob(f"{PREFIX}*.mmdb")
        if p.is_file() and not p.is_symlink() and p.resolve() != current
    ]
    others.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
    removed = others[KEEP - 1 :]
    for path in removed:
        path.unlink()
    return sorted(p.name for p in removed)
