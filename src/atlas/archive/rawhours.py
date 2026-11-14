"""ARCHIVE-RAW-1: each network's raw files of an hour, one zstd-compressed tar in Spaces.

`upload_hours` (the orchestrator's `raw_upload` job) packs, for every hour that ended at least
SETTLE ago, each network folder's files not uploaded yet (rawfiles.spool's
`<root>/YYYY/MM/DD/HH/<network>/`) into one private object
`raw-files/YYYY/MM/DD/HH/<network>.tar.zst`: a tar of `manifest.json` (the hour, the network,
each file's name, size and sha256), then the files exactly as spooled (as received, so they
stay byte for byte what was imported), compressed with zstd level LEVEL (`zstd -d` or
`tar --zstd` reads it). The object's size, MD5 and sha256 are checked in the bucket
before the folder's `uploaded.json` records it. A file filed after its network-hour was
uploaded goes into a further part (`<network>.1.tar.zst`, ...). Objects are never overwritten
or deleted (no expiry). Then every hour older than keep_days whose files are all recorded as
uploaded is removed locally; an hour with a file not uploaded is never removed.

`fetch` (`atlas archive raw`) lists an hour's objects and extracts a network-hour's parts back
into the spool layout under another root, each file checked against its manifest, so
`atlas archive rebuild --spool <that root>` imports them.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import zstandard

from . import ArchiveError
from .export import file_digests
from .rawfiles import FILE, KEEP_DAYS, NETWORK, files, hour_dir, hours, networks
from .spaces import Spaces

PREFIX = "raw-files"
# scripts/archive/raw_zstd_bench.py: the spooled files are gzip already, so any level saves
# under 1% (19 costs about 1 s per network-hour, a long window only adds memory).
LEVEL = 19
SETTLE = timedelta(minutes=5)  # an hour is packed this long after it ended
UPLOADED = "uploaded.json"
MAX_MANIFEST = 16 << 20
CHUNK = 1 << 20
PART = re.compile(rf"(?P<network>{NETWORK.pattern})(?:\.(?P<part>[1-9]\d*))?\.tar\.zst")


def object_key(hour: datetime, network: str, part: int = 0) -> str:
    return f"{PREFIX}/{hour:%Y/%m/%d/%H}/{network}" + (f".{part}" if part else "") + ".tar.zst"


def _member(archive: tarfile.TarFile, name: str, stream, size: int, hour: datetime) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = size, int(hour.timestamp()), 0o600
    archive.addfile(info, stream)


def pack(hour: datetime, network: str, paths: list[Path], out: Path) -> Path:
    """The network-hour's object: deterministic (fixed mtime, owner and mode), so a rerun after
    a crash before `uploaded.json` was written finds the same bytes in the bucket."""
    entries = []
    for path in paths:
        digests = file_digests(path)
        entries.append({"name": path.name, "bytes": digests["bytes"], "sha256": digests["sha256"]})
    manifest = {"hour": hour.isoformat(), "network": network, "files": entries}
    data = json.dumps(manifest, indent=1, sort_keys=True).encode()
    compressor = zstandard.ZstdCompressor(level=LEVEL, write_checksum=True)
    with (
        out.open("wb") as raw,
        compressor.stream_writer(raw, closefd=False) as stream,
        tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as archive,
    ):
        _member(archive, "manifest.json", io.BytesIO(data), len(data), hour)
        for path, entry in zip(paths, entries, strict=True):
            with path.open("rb") as source:
                _member(archive, path.name, source, entry["bytes"], hour)
    return out


def _uploaded(folder: Path) -> dict:
    path = folder / UPLOADED
    return json.loads(path.read_text()) if path.exists() else {"parts": []}


def _upload_part(spaces: Spaces, hour: datetime, network: str, new: list[Path], part: int):
    key = object_key(hour, network, part)
    with tempfile.TemporaryDirectory(prefix="atlas-raw-") as temporary:
        path = pack(hour, network, new, Path(temporary, "hour.tar.zst"))
        digests = file_digests(path)
        spaces.put_verified(key, path, digests, what="the network's hour")
    return {"key": key, "sha256": digests["sha256"], "files": [p.name for p in new]}


def _network_hour(spaces, hour: datetime, network: str, folder: Path, ended: bool):
    """Upload the folder's files not uploaded yet once the hour ended: (the new object's key or
    None, whether a file is left not uploaded)."""
    state = _uploaded(folder)
    done = {name for part in state["parts"] for name in part["files"]}
    new = [p for p in files(folder) if p.name not in done]
    if not new or not ended:
        return None, bool(new)
    state["parts"].append(_upload_part(spaces, hour, network, new, len(state["parts"])))
    partial = folder / f".{UPLOADED}.partial"
    partial.write_text(json.dumps(state, indent=1))
    partial.replace(folder / UPLOADED)
    return state["parts"][-1]["key"], False


def upload_hours(root, spaces: Spaces, now: datetime, keep_days: int = KEEP_DAYS) -> dict:
    """Upload every ended network-hour's files not uploaded yet, then remove the expired hours.
    Files directly in an hour folder (the layout before ARCHIVE-RAW-1) are never uploaded by
    this, so their hour is never removed."""
    uploaded, removed = [], []
    for hour, folder in hours(Path(root)):
        ended = hour + timedelta(hours=1) + SETTLE <= now
        left = bool(files(folder))
        for network, own in networks(folder):
            key, waiting = _network_hour(spaces, hour, network, own, ended)
            uploaded += [key] if key else []
            left = left or waiting
        if not left and hour + timedelta(days=keep_days, hours=1) <= now:
            shutil.rmtree(folder)
            removed.append(f"{hour:%Y-%m-%dT%H}")
            _drop_empty_parents(folder)
    return {"uploaded": uploaded, "removed_hours": removed}


def _drop_empty_parents(folder: Path) -> None:
    """Remove the day, month and year folders a removed hour leaves empty."""
    for parent in folder.parents[:3]:
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()


def listing(spaces: Spaces, hour: datetime) -> list[str]:
    """The hour's objects in the bucket, in key order."""
    prefix = f"{PREFIX}/{hour:%Y/%m/%d/%H}/"
    return [k for k in spaces.list_keys(prefix) if PART.fullmatch(k.removeprefix(prefix))]


def _network(key: str) -> str:
    return PART.fullmatch(key.rsplit("/", 1)[1])["network"]


def _copy(stream, target: Path, entry: dict) -> None:
    partial, digest, size = target.with_name(f".{target.name}.partial"), hashlib.sha256(), 0
    with partial.open("wb") as out:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
    if (size, digest.hexdigest()) != (entry["bytes"], entry["sha256"]):
        partial.unlink()
        raise ArchiveError(f"{target.name} does not match the manifest")
    partial.chmod(0o600)
    partial.replace(target)


def extract(path: Path, folder: Path) -> list[str]:
    """Write a network-hour object's files into folder, each checked against its manifest."""
    decompressor = zstandard.ZstdDecompressor()
    with (
        path.open("rb") as raw,
        decompressor.stream_reader(raw) as stream,
        tarfile.open(fileobj=stream, mode="r|") as archive,
    ):
        members = iter(archive)
        first = next(members, None)
        if first is None or first.name != "manifest.json" or first.size > MAX_MANIFEST:
            raise ArchiveError(f"{path.name} does not start with its manifest")
        expected = {f["name"]: f for f in json.loads(archive.extractfile(first).read())["files"]}
        written = []
        for member in members:
            entry = expected.pop(member.name, None)
            if entry is None or not member.isreg() or not FILE.fullmatch(member.name):
                raise ArchiveError(f"{member.name!r} is not a file of the manifest")
            _copy(archive.extractfile(member), folder / member.name, entry)
            written.append(member.name)
    if expected:
        raise ArchiveError(f"missing from the object: {sorted(expected)}")
    return written


def fetch(spaces: Spaces, hour: datetime, network: str, out: Path) -> dict:
    """Download and extract every part of a network-hour into `<out>/YYYY/MM/DD/HH/<network>/`
    (the spool layout), each object checked against its stored sha256 first."""
    keys = [k for k in listing(spaces, hour) if _network(k) == network]
    if not keys:
        raise ArchiveError(f"no raw files of {network} for {hour:%Y-%m-%dT%H} in the bucket")
    folder = hour_dir(Path(out), hour.astimezone(UTC)) / network
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    written = []
    for key in keys:
        with tempfile.TemporaryDirectory(prefix="atlas-raw-") as temporary:
            path = Path(temporary, "part.tar.zst")
            spaces.get_file(key, path)
            if (spaces.head(key) or {}).get("sha256") != file_digests(path)["sha256"]:
                raise ArchiveError(f"{key} does not match its stored sha256")
            written += extract(path, folder)
    return {"objects": keys, "folder": str(folder), "files": written}
