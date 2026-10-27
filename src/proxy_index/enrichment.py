"""Optional local MMDB enrichment, independent of the request path."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import maxminddb


def find_database(path: str | Path | None = None) -> Path:
    configured = path or os.environ.get("DBIP_MMDB")
    if configured:
        result = Path(configured).expanduser().resolve()
        if not result.is_file():
            raise FileNotFoundError("Configured MMDB file does not exist")
        return result
    roots = [Path.cwd() / "db-ip", Path(__file__).resolve().parents[2] / "db-ip"]
    candidates = {p.resolve() for root in roots for p in root.glob("*.mmdb")}
    if len(candidates) != 1:
        raise ValueError("Set DBIP_MMDB or --mmdb to select exactly one local MMDB file")
    return candidates.pop()


class LocalEnricher:
    """Memory-mapped database; bounded lookup cache shared across result rows."""

    def __init__(self, path: str | Path | None = None, *, cache_size: int = 100_000):
        self.path = find_database(path)
        self.reader = maxminddb.open_database(str(self.path))
        self.cache_size = cache_size
        self.cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        metadata = self.reader.metadata()
        with self.path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        self.release = {
            "file": self.path.name,
            "sha256": checksum,
            "database_type": metadata.database_type,
            "build_epoch": metadata.build_epoch,
            "built_at": datetime.fromtimestamp(metadata.build_epoch, UTC).isoformat(),
        }

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


def enrich_run(directory: str | Path, mmdb: str | Path | None = None) -> dict[str, Any]:
    """Append a release-specific artifact without modifying original measurements."""
    directory = Path(directory)
    summary = json.loads((directory / "summary.json").read_text())
    if summary.get("schema_version") != "1" or not summary.get("pool_eligible"):
        raise ValueError("Enrichment requires a version 1 Generic HTTP pool result")
    source = directory / "ips.jsonl.gz"
    with source.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if checksum != summary["artifacts"]["ips"]["sha256"]:
        raise ValueError("IP artifact checksum does not match summary")
    with LocalEnricher(mmdb) as enricher:
        release_id = enricher.release["sha256"]
        destination = directory / f"enriched-{release_id}.jsonl.gz"
        report_path = directory / f"enrichment-{release_id}.json"
        if destination.exists() or report_path.exists():
            raise FileExistsError("This run already has enrichment for the selected MMDB")
        partial = destination.with_suffix(".gz.partial")
        count, unmatched = 0, 0
        # An exclusive creation also prevents two writers racing this release.
        with partial.open("xb") as raw_output:
            os.chmod(partial, 0o600)
            with (
                gzip.open(source, "rt", encoding="utf-8") as src,
                gzip.open(raw_output, "wt", encoding="utf-8") as dst,
            ):
                for line in src:
                    row = json.loads(line)
                    if row["run_id"] != summary["run_id"]:
                        raise ValueError("IP artifact contains a different run ID")
                    data = enricher.lookup(row["ip"])
                    unmatched += not data["matched"]
                    count += 1
                    dst.write(
                        json.dumps(
                            row
                            | {
                                "enrichment": data,
                                "dbip_release": release_id,
                            },
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
        if count != summary["artifacts"]["ips"]["rows"]:
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
