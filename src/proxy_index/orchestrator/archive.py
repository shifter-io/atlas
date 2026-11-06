"""Run directory archives: a flat tar.gz of the files a node's run produced.

Packing takes only regular files at the top of the run directory. Unpacking is defensive: the
upload comes from the network, so only plainly named regular files are accepted (no paths,
links, devices or duplicates), with limits on member count and extracted size.
"""

from __future__ import annotations

import gzip
import os
import re
import tarfile
import zlib
from collections.abc import Callable
from pathlib import Path

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
MAX_MEMBERS = 64
CHUNK = 1 << 20
GZIP_LEVEL = 6


class ArchiveError(ValueError):
    """The upload is not an acceptable run archive."""


def pack(
    directory: str | Path,
    destination: str | Path,
    *,
    nested: Callable[[str], bool] | None = None,
    mtime: int | None = None,
) -> Path:
    """The run directory's plain top-level files as tar.gz. `nested` (as unpack's) adds the
    files below it whose relative path it admits. With `mtime` the archive is deterministic:
    members sorted by name, uid/gid 0 without owner names, mode 0600, that mtime, gzip level 6
    with the same header time and no file name."""
    directory, destination = Path(directory), Path(destination)
    names = [p.name for p in directory.iterdir() if NAME.fullmatch(p.name) and _regular(p)]
    if nested is not None:
        for folder, _, files in os.walk(directory):  # links to directories are not followed
            found = (Path(folder, file).relative_to(directory).as_posix() for file in files)
            names += [name for name in found if "/" in name and nested(name)]
    names = sorted(name for name in names if _regular(directory / name))
    if mtime is None:
        with tarfile.open(destination, "w:gz") as archive:
            for name in names:
                archive.add(directory / name, arcname=name, recursive=False)
        return destination
    with (
        destination.open("wb") as raw,
        gzip.GzipFile("", "wb", GZIP_LEVEL, raw, mtime) as stream,
        tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        for name in names:
            info = archive.gettarinfo(directory / name, arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode, info.mtime = 0o600, mtime
            with (directory / name).open("rb") as source:
                archive.addfile(info, source)
    return destination


def _regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def unpack(
    source: str | Path,
    directory: str | Path,
    *,
    max_bytes: int,
    nested: Callable[[str], bool] | None = None,
    max_members: int = MAX_MEMBERS,
) -> list[str]:
    """Extract a run archive into an empty directory; returns the file names.

    `nested` admits more names (exact relative paths, such as a browser bundle's
    `browser/tries.jsonl`), extracted under their parent directories; directory entries it
    admits are skipped. It must accept only safe, fixed path shapes.
    """
    try:
        with tarfile.open(source, "r:gz") as archive:
            names = _extract_all(archive, Path(directory), max_bytes, nested, max_members)
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        raise ArchiveError(f"Unreadable archive ({type(exc).__name__})") from None
    if "summary.json" not in names:
        raise ArchiveError("Archive has no summary.json")
    return names


def _extract_all(
    archive: tarfile.TarFile,
    directory: Path,
    max_bytes: int,
    nested: Callable[[str], bool] | None,
    max_members: int,
) -> list[str]:
    """Extract unpack's admitted members in archive order; returns their names."""
    names: list[str] = []
    total = 0
    for member in archive:
        if member.isdir() and nested is not None and nested(member.name):
            continue
        if not _admit(member, names, nested, max_members):
            (directory / member.name).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        total += member.size
        if member.size < 0 or total > max_bytes:
            raise ArchiveError("Archive is larger than the extraction limit")
        _extract(archive, member, directory / member.name)
        names.append(member.name)
    return names


def _admit(
    member: tarfile.TarInfo,
    names: list[str],
    nested: Callable[[str], bool] | None,
    max_members: int,
) -> bool:
    """Refuse a member unpack may not extract; True when its name is plain (no parent)."""
    if len(names) >= max_members:
        raise ArchiveError("Too many files in the archive")
    if not member.isreg():
        raise ArchiveError("Archives may contain only regular files")
    plain = NAME.fullmatch(member.name) is not None
    if not (plain or (nested and nested(member.name))) or member.name in names:
        raise ArchiveError("Archive member names must be plain, unique file names")
    return plain


def _extract(archive: tarfile.TarFile, member: tarfile.TarInfo, path: Path) -> None:
    """Copy a regular member to a new file, never past its declared size."""
    stream = archive.extractfile(member)
    if stream is None:
        raise ArchiveError("Archive member cannot be read")
    written = 0
    with stream, path.open("xb") as output:
        while chunk := stream.read(CHUNK):
            written += len(chunk)
            if written > member.size:
                raise ArchiveError("Archive member is longer than declared")
            output.write(chunk)
