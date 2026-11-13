#!/usr/bin/env python3
"""pytest's assertion-rewrite cache (tests/**/__pycache__), kept between tier1 runs (CI-90-6).

Usage: rewrite_cache.py restore|save <cache directory> [--root DIR]
Run with the tests' Python (`uv run`): the key is the SHA-256 of every tests/**/*.py (path and
content), pyproject.toml (pytest's settings), the checkout's path (in each code object), pytest's
version and this Python's.

restore: sets every tests/**/*.py's mtime to one fixed time first (a checkout's mtimes differ
each run, and pytest and Python keep a .pyc only while its source's mtime and size match; the
key already covers the content), then unpacks `<key>.tar` when the cache has it, and compiles
src/ (the tests import atlas from there, pytest's `pythonpath`; the rewrite pass otherwise
writes its bytecode, and every worker would compile it at once). Exit 0 on a hit; 1 on a miss
or an unreadable archive, whose files are removed again, so the caller runs the rewrite pass.
save: packs every tests/**/__pycache__/*.pyc into `<key>.tar`, with their count, under a
temporary name, then renames it into place, so a partial archive is never read (nor one cut
short later: restore checks the count); keeps the newest KEEP archives.
"""

import argparse
import compileall
import contextlib
import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
import time
from importlib import metadata
from pathlib import Path

# The mtime every test source gets: any fixed time; the key changes with any content.
MTIME = 1_000_000_000
KEEP = 8
# The archive's member count, in its global header: an archive cut short is never read.
COUNT = "atlas.members"
# A temporary archive this old is a save that died; younger ones may be a save in progress.
STALE = 3600


def sources(root):
    return sorted((root / "tests").rglob("*.py"))


def key(root, versions=None):
    """The cache's key; `versions` (Python's, pytest's) default to the running ones."""
    digest = hashlib.sha256()
    for part in (str(root), *(versions or (sys.version, metadata.version("pytest")))):
        digest.update(part.encode() + b"\0")
    for path in [root / "pyproject.toml", *sources(root)]:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def caches(root):
    return sorted((root / "tests").rglob("__pycache__"))


def restore(root, cache):
    for path in sources(root):
        os.utime(path, (MTIME, MTIME))
    archive = cache / f"{key(root)}.tar"
    if not archive.exists():
        return False
    try:
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            if tar.pax_headers.get(COUNT) != str(len(members)):
                raise tarfile.TarError(f"{archive.name}: {len(members)} members, not complete")
            if not all(_cached(member.name) for member in members):
                raise tarfile.TarError(f"{archive.name}: a member outside tests/**/__pycache__")
            tar.extractall(root, members, filter="data")
    except (OSError, EOFError, tarfile.TarError) as error:
        print(f"rewrite cache: {error}; rebuilding it", file=sys.stderr)
        for directory in caches(root):
            shutil.rmtree(directory)
        return False
    if (root / "src").is_dir():
        compileall.compile_dir(root / "src", quiet=1, workers=0)
    return True


def _cached(name):
    parts = Path(name).parts
    return parts[0] == "tests" and ".." not in parts and parts[-2:-1] == ("__pycache__",)


def save(root, cache):
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{key(root)}.tar"
    descriptor, temporary = tempfile.mkstemp(dir=cache, prefix=".tmp-", suffix=".part")
    try:
        files = [path for directory in caches(root) for path in sorted(directory.glob("*.pyc"))]
        headers = {COUNT: str(len(files))}
        with (
            os.fdopen(descriptor, "wb") as stream,
            tarfile.open(
                fileobj=stream, mode="w", format=tarfile.PAX_FORMAT, pax_headers=headers
            ) as tar,
        ):
            for path in files:
                tar.add(path, path.relative_to(root).as_posix())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    prune(cache)


def prune(cache):
    # Another run may prune or rename a file between the listing and its stat.
    ages = {}
    for path in cache.iterdir():
        with contextlib.suppress(FileNotFoundError):
            ages[path] = path.stat().st_mtime
    archives = sorted((p for p in ages if p.suffix == ".tar"), key=ages.get, reverse=True)
    for path in archives[KEEP:]:
        path.unlink(missing_ok=True)
    for path in ages:
        if path.name.startswith(".tmp-") and ages[path] < time.time() - STALE:
            path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("restore", "save"))
    parser.add_argument("cache", type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.action == "save":
        save(root, args.cache)
        return 0
    return 0 if restore(root, args.cache) else 1


if __name__ == "__main__":
    raise SystemExit(main())
