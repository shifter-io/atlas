"""Run directory archives: a flat tar.gz of the files a node's run produced.

Packing takes only regular files at the top of the run directory. Unpacking is defensive: the
upload comes from the network, so only plainly named regular files are accepted (no paths,
links, devices or duplicates), with limits on member count and extracted size.
"""

from __future__ import annotations

import re
import tarfile
import zlib
from collections.abc import Callable
from pathlib import Path

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
MAX_MEMBERS = 64
CHUNK = 1 << 20


class ArchiveError(ValueError):
    """The upload is not an acceptable run archive."""


def pack(directory: str | Path, destination: str | Path) -> Path:
    directory, destination = Path(directory), Path(destination)
    with tarfile.open(destination, "w:gz") as archive:
        for path in sorted(directory.iterdir()):
            if path.is_file() and not path.is_symlink() and NAME.fullmatch(path.name):
                archive.add(path, arcname=path.name, recursive=False)
    return destination


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
    directory = Path(directory)
    names: list[str] = []
    total = 0
    try:
        with tarfile.open(source, "r:gz") as archive:
            for member in archive:
                if member.isdir() and nested is not None and nested(member.name):
                    continue
                if len(names) >= max_members:
                    raise ArchiveError("Too many files in the archive")
                if not member.isreg():
                    raise ArchiveError("Archives may contain only regular files")
                plain = NAME.fullmatch(member.name) is not None
                if not (plain or (nested and nested(member.name))) or member.name in names:
                    raise ArchiveError("Archive member names must be plain, unique file names")
                if not plain:
                    (directory / member.name).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                total += member.size
                if member.size < 0 or total > max_bytes:
                    raise ArchiveError("Archive is larger than the extraction limit")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ArchiveError("Archive member cannot be read")
                written = 0
                with stream, (directory / member.name).open("xb") as output:
                    while chunk := stream.read(CHUNK):
                        written += len(chunk)
                        if written > member.size:
                            raise ArchiveError("Archive member is longer than declared")
                        output.write(chunk)
                names.append(member.name)
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        raise ArchiveError(f"Unreadable archive ({type(exc).__name__})") from None
    if "summary.json" not in names:
        raise ArchiveError("Archive has no summary.json")
    return names
