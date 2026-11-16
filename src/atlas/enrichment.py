"""Optional local MMDB enrichment, independent of the request path."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import threading
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import maxminddb

from .storage_read import declared_lines

# The one file name `dbip update` keeps the current release under (replaced atomically).
CURRENT = "dbip-full.mmdb"
MMDB_ENV = "DBIP_MMDB"  # the variable naming the file (find_source)
OPEN_ATTEMPTS = 3  # the file replaced while it was being opened: open the new one
# The checkout's db-ip/, the second place find_source looks in.
CHECKOUT_DB_IP = Path(__file__).resolve().parents[2] / "db-ip"
ENRICHED_NAME = re.compile(r"enrich(?:ed|ment)-.+")  # enrich_run's two files of a release


def find_source(path: str | Path | None = None) -> Path:
    """The configured MMDB as given: --mmdb, else $DBIP_MMDB, else db-ip/dbip-full.mmdb, else
    the only *.mmdb in db-ip/. A link is returned unresolved, so a reader can follow it."""
    configured = path or os.environ.get(MMDB_ENV)
    if configured:
        result = Path(configured).expanduser().absolute()
        if not result.is_file():
            raise FileNotFoundError("Configured MMDB file does not exist")
        return result
    roots = [Path.cwd() / "db-ip", CHECKOUT_DB_IP]
    for root in roots:
        if (root / CURRENT).is_file():
            return (root / CURRENT).absolute()
    candidates = {p.resolve() for root in roots for p in root.glob("*.mmdb")}
    if len(candidates) != 1:
        raise ValueError("Set DBIP_MMDB or --mmdb to select exactly one local MMDB file")
    return candidates.pop()


def find_database(path: str | Path | None = None) -> Path:
    """The MMDB file itself (find_source with every link resolved)."""
    return find_source(path).resolve()


def release_record(path: Path, metadata: Any, sha256: str) -> dict[str, Any]:
    """What identifies a release in enrichment artifacts and `dbip_releases.metadata`."""
    return {
        "file": path.name,
        "sha256": sha256,
        "database_type": metadata.database_type,
        "build_epoch": metadata.build_epoch,
        "built_at": datetime.fromtimestamp(metadata.build_epoch, UTC).isoformat(),
    }


def _identity(path: Path) -> tuple[int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_mtime_ns


class LocalEnricher:
    """Memory-mapped database; bounded lookup cache shared across result rows.

    `source` is the path as configured (often db-ip/dbip-full.mmdb); `path` is the file open now.
    `reload()` reopens the database when the file was replaced (another inode) or a link moved,
    so a long-lived process follows `dbip update` without a restart."""

    def __init__(self, path: str | Path | None = None, *, cache_size: int = 100_000):
        self.source = find_source(path)
        self.cache_size = cache_size
        self.cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.path, self.reader, self.release, self._opened = self._open()

    def _open(self) -> tuple[Path, Any, dict[str, Any], tuple[int, int, int]]:
        """Open the reader and hash the same file: when it is replaced in between (the update
        renames a new release over it), the identities differ and the new file is opened."""
        for _ in range(OPEN_ATTEMPTS):
            path = self.source.resolve()
            opened = _identity(path)
            reader = maxminddb.open_database(str(path))
            try:
                with path.open("rb") as stream:
                    checksum = hashlib.file_digest(stream, "sha256").hexdigest()
                release = release_record(path, reader.metadata(), checksum)
                if _identity(path) == opened:
                    return path, reader, release, opened
            except BaseException:
                reader.close()
                raise
            reader.close()
        raise OSError("The MMDB file kept changing while it was opened")

    def stale(self) -> bool:
        """True when `source` now names another file than the one open (cheap: two stats)."""
        try:
            return _identity(self.source.resolve()) != self._opened
        except OSError:
            return False  # mid-swap or gone: keep the open release

    def reload(self) -> bool:
        """Reopen when stale; True when it did. The old reader stays open if the new one fails.
        Call it only while no lookup runs (it closes the old reader)."""
        if not self.stale():
            return False
        old = self.reader
        self.path, self.reader, self.release, self._opened = self._open()
        self.cache.clear()
        old.close()
        return True

    def __enter__(self) -> LocalEnricher:
        return self

    def __exit__(self, *args: object) -> None:
        self.reader.close()

    def lookup(self, address: str) -> dict[str, Any]:
        if address in self.cache:
            self.cache.move_to_end(address)
            return self.cache[address].copy()
        record = self.reader.get(address)
        data = record or {}
        traits = data.get("traits", {})
        subdivisions = data.get("subdivisions", [])
        region = subdivisions[0] if subdivisions else {}
        result = {
            "matched": record is not None,
            "country_code": data.get("country", {}).get("iso_code"),
            "country": data.get("country", {}).get("names", {}).get("en"),
            "region_code": region.get("iso_code"),
            "region": region.get("names", {}).get("en"),
            "city": data.get("city", {}).get("names", {}).get("en"),
            "latitude": data.get("location", {}).get("latitude"),
            "longitude": data.get("location", {}).get("longitude"),
            "isp": traits.get("isp"),
            "organization": traits.get("organization"),
            "asn": traits.get("autonomous_system_number"),
            "asn_organization": traits.get("autonomous_system_organization"),
            "connection_type": traits.get("connection_type"),
            "usage_type": traits.get("user_type"),
        }
        self.cache[address] = result
        if len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)
        return result.copy()


def enrich_run(
    directory: str | Path, mmdb: str | Path | None = None, *, enricher: LocalEnricher | None = None
) -> dict[str, Any]:
    """Append a release-specific artifact without modifying original measurements; with
    `enricher` (an open one, kept open) instead of opening `mmdb`. The artifact's bytes depend
    only on the run and the release, so enriching a run again with its release gives the same
    checksum."""
    directory = Path(directory)
    summary = json.loads((directory / "summary.json").read_text())
    if summary.get("schema_version") != "1" or not summary.get("pool_eligible"):
        raise ValueError("Enrichment requires a version 1 Generic HTTP pool result")
    source = directory / "ips.jsonl.gz"
    with source.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if checksum != summary["artifacts"]["ips"]["sha256"]:
        raise ValueError("IP artifact checksum does not match summary")
    if enricher is not None:
        return _enrich(directory, summary, enricher)
    with LocalEnricher(mmdb) as opened:
        return _enrich(directory, summary, opened)


def _enrich(directory: Path, summary: dict[str, Any], enricher: LocalEnricher) -> dict[str, Any]:
    """enrich_run's artifact and report for the run in `directory`."""
    release_id = enricher.release["sha256"]
    destination = directory / f"enriched-{release_id}.jsonl.gz"
    report_path = directory / f"enrichment-{release_id}.json"
    if destination.exists() or report_path.exists():
        raise FileExistsError("This run already has enrichment for the selected MMDB")
    partial = destination.with_suffix(".gz.partial")
    source = directory / "ips.jsonl.gz"
    declared = summary["artifacts"]["ips"]["rows"]
    count, unmatched = _write_enriched(source, partial, summary["run_id"], enricher, declared)
    if count != declared:
        raise ValueError("IP artifact row count does not match summary")
    partial.replace(destination)
    report = {
        "schema_version": "1",
        "run_id": summary["run_id"],
        "release": enricher.release,
        "rows": count,
        "unmatched_rows": unmatched,
        "file": destination.name,
    }
    with report_path.open("x", encoding="utf-8") as out:
        os.chmod(report_path, 0o600)
        json.dump(report, out, indent=2)
    return report


def strip(directory: str | Path) -> None:
    """Remove every enrichment of the run in `directory` (enrich_run's files)."""
    for path in Path(directory).glob("enrich*-*"):
        if ENRICHED_NAME.fullmatch(path.name):
            path.unlink()


def reenrich(directory: str | Path, enricher: LocalEnricher) -> dict[str, Any]:
    """enrich_run with `enricher` in place of every enrichment the run has (a node's)."""
    strip(directory)
    return enrich_run(directory, enricher=enricher)


class Unavailable(Exception):
    """The configured MMDB cannot be opened (missing, ambiguous or not a database)."""


class SharedEnricher:
    """One LocalEnricher for a process's threads (the manager's uploads, PULSE-ENRICH-1): opened
    on first use, reopened when `dbip update` replaced the file, one run at a time (a reload
    closes the reader another lookup would use)."""

    def __init__(self, path: str | Path):
        self.path = path
        self._lock = threading.Lock()
        self._enricher: LocalEnricher | None = None

    def _current(self) -> LocalEnricher:
        """The open enricher, reopened when the file was replaced; a replacement that cannot be
        opened keeps the open release (the next run tries again)."""
        try:
            if self._enricher is None:
                self._enricher = LocalEnricher(self.path)
            else:
                self._enricher.reload()
        except (OSError, ValueError, RuntimeError) as exc:  # maxminddb's InvalidDatabaseError
            if self._enricher is None:
                raise Unavailable(type(exc).__name__) from exc
        return self._enricher

    def reenrich(self, directory: str | Path) -> dict[str, Any]:
        """reenrich the run with the current release; Unavailable leaves the run untouched."""
        with self._lock:
            return reenrich(directory, self._current())

    def close(self) -> None:
        with self._lock:
            if self._enricher is not None:
                self._enricher.reader.close()
                self._enricher = None


def _write_enriched(
    source: Path, partial: Path, run_id: str, enricher: LocalEnricher, declared: int
) -> tuple[int, int]:
    """Write the enriched rows of `source` to `partial`, refused past the `declared` rows its
    summary counts; returns (rows, unmatched rows)."""
    release_id = enricher.release["sha256"]
    count, unmatched = 0, 0
    # An exclusive creation also prevents two writers racing this release.
    with partial.open("xb") as raw_output:
        os.chmod(partial, 0o600)
        # No name or time in the gzip header: the same rows give the same bytes.
        packed = gzip.GzipFile(filename="", fileobj=raw_output, mode="wb", mtime=0)
        with (
            gzip.open(source, "rt", encoding="utf-8") as src,
            io.TextIOWrapper(packed, encoding="utf-8") as dst,
        ):
            for line in declared_lines(src, declared):
                data, enriched = _enrich_line(line, run_id, enricher, release_id)
                unmatched += not data["matched"]
                count += 1
                dst.write(enriched)
    return count, unmatched


def _enrich_line(
    line: str, run_id: str, enricher: LocalEnricher, release_id: str
) -> tuple[dict[str, Any], str]:
    """One IP artifact row's enrichment and its enriched JSON line."""
    row = json.loads(line)
    if row["run_id"] != run_id:
        raise ValueError("IP artifact contains a different run ID")
    data = enricher.lookup(row["ip"])
    enriched = row | {"enrichment": data, "dbip_release": release_id}
    return data, json.dumps(enriched, separators=(",", ":")) + "\n"
