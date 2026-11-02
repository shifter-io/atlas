"""Count one network's 24-hour campaign per requested country, from its enriched hour files.

Input is one network directory of the hour enrichment: manifest.json (schema 2) and one
hour-<YYYYMMDDTHH>Z.jsonl.gz file per clock hour with IPs. Each file record is one distinct exit
IP of that hour: ip, seen_count (request rows it served), regions, countries (request rows per
requested country), enrichment and dbip_release. The output holds counts only, never an IP:

- per clock hour (00-23): status, unique IPs, request rows and attempted requests (manifest);
- per requested country and hour: unique IPs and request rows;
- whole-day unique IPs for the network and for each country (the union over the 24 hours).

Every file is checked against its manifest slot while it is read: compressed sha256, records =
unique_ips and distinct, sum of seen_count = request_rows, and each record's country counts sum
to its seen_count. A mismatch raises FormulaError; nothing is skipped or repaired.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
from collections.abc import Iterator
from pathlib import Path

HOURS = tuple(f"{hour:02d}" for hour in range(24))
MANIFEST_SCHEMA = 2
STATUSES = ("present", "requests_no_ips", "missing")
# The enrichment writes compact records with these keys first; any other layout is parsed whole.
_RECORD = re.compile(
    rb'^\{"ip":"([^"]+)","seen_count":(\d+),"regions":\{[^}]*\},"countries":\{([^}]*)\}'
)
_PAIR = re.compile(rb'"([^"]+)":(\d+)')
_CHUNK = 1 << 20


class FormulaError(ValueError):
    """An input does not match its manifest or the formula's preconditions."""


class _HashingReader(io.RawIOBase):
    """Read a binary file and hash every byte read."""

    def __init__(self, raw):
        self._raw = raw
        self.digest = hashlib.sha256()

    def readable(self):
        return True

    def readinto(self, buffer):
        count = self._raw.readinto(buffer)
        if count:
            self.digest.update(memoryview(buffer)[:count])
        return count


def parse_record(line: bytes) -> tuple[bytes, int, dict[str, int]]:
    """One hour-file line as (ip, seen_count, request rows per requested country)."""
    match = _RECORD.match(line)
    if match:
        countries = {key.decode(): int(value) for key, value in _PAIR.findall(match[3])}
        return match[1], int(match[2]), countries
    try:
        record = json.loads(line)
        return str(record["ip"]).encode(), int(record["seen_count"]), dict(record["countries"])
    except (ValueError, KeyError, TypeError) as error:
        raise FormulaError(f"unreadable hour-file record: {error}") from None


def read_hour_file(path: Path, sha256: str) -> Iterator[tuple[bytes, int, dict[str, int]]]:
    """Yield the records of one hour file, then check its compressed sha256."""
    with path.open("rb", buffering=0) as raw:
        hashed = _HashingReader(raw)
        with gzip.GzipFile(fileobj=io.BufferedReader(hashed, _CHUNK)) as lines:
            for line in lines:
                if line.strip():
                    yield parse_record(line)
        while hashed.read(_CHUNK):
            pass
    if hashed.digest.hexdigest() != sha256:
        raise FormulaError(f"{path.name}: sha256 differs from the manifest")


class _Counter:
    """Running per-country and whole-day counts of one network."""

    def __init__(self):
        self.country_ips: dict[str, list[int]] = {}
        self.country_requests: dict[str, list[int]] = {}
        self.country_day: dict[str, set[bytes]] = {}
        self.day: set[bytes] = set()

    def add_file(self, index: int, path: Path, slot: dict) -> None:
        hour: set[bytes] = set()
        rows = 0
        for ip, seen, countries in read_hour_file(path, slot["sha256"]):
            if sum(countries.values()) != seen:
                raise FormulaError(f"{path.name}: country counts differ from seen_count")
            hour.add(ip)
            rows += seen
            for country, count in countries.items():
                self._add(index, ip, country, count)
        self.day |= hour
        if len(hour) != slot["unique_ips"] or rows != slot["request_rows"]:
            raise FormulaError(
                f"{path.name}: {len(hour)} IPs / {rows} rows, manifest "
                f"{slot['unique_ips']} / {slot['request_rows']}"
            )

    def _add(self, index: int, ip: bytes, country: str, count: int) -> None:
        if country not in self.country_ips:
            self.country_ips[country] = [0] * 24
            self.country_requests[country] = [0] * 24
            self.country_day[country] = set()
        self.country_ips[country][index] += 1
        self.country_requests[country][index] += count
        self.country_day[country].add(ip)

    def countries(self) -> dict[str, dict]:
        return {
            country: {
                "unique_ips": self.country_ips[country],
                "requests": self.country_requests[country],
                "day_unique_ips": len(self.country_day[country]),
            }
            for country in sorted(self.country_ips)
        }


def _slot_summary(slot: dict) -> dict:
    status = slot.get("status", "missing")
    if status not in STATUSES:
        raise FormulaError(f"unknown slot status {status!r}")
    return {
        "status": status,
        "unique_ips": int(slot.get("unique_ips", 0)),
        "requests": int(slot.get("request_rows", 0)),
        "attempted_requests": int(slot.get("requests", 0)),
        "runs": sorted(slot.get("runs", [])),
    }


def load_manifest(network_dir: Path) -> tuple[bytes, dict]:
    """The manifest's bytes (for its sha256) and its parsed, checked content."""
    data = (network_dir / "manifest.json").read_bytes()
    manifest = json.loads(data)
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise FormulaError(f"manifest schema {manifest.get('schema_version')!r} is not 2")
    unknown = set(manifest["slots"]) - set(HOURS)
    if unknown:
        raise FormulaError(f"manifest slots outside 00-23: {sorted(unknown)}")
    return data, manifest


def aggregate_network(network_dir: Path) -> dict:
    """The verified per-hour and per-country counts of one network directory."""
    data, manifest = load_manifest(network_dir)
    counter = _Counter()
    hours = []
    for index, key in enumerate(HOURS):
        slot = manifest["slots"].get(key, {"status": "missing"})
        summary = _slot_summary(slot)
        if summary["status"] == "present":
            counter.add_file(index, network_dir / slot["file"], slot)
        elif summary["unique_ips"]:
            raise FormulaError(f"slot {key} has IPs but status {summary['status']}")
        hours.append(summary)
    return {
        "network": manifest["network"],
        "product": manifest["product"],
        "manifest_sha256": hashlib.sha256(data).hexdigest(),
        "dbip_release_sha256": manifest["dbip_release"]["sha256"],
        "hours": hours,
        "day_unique_ips": len(counter.day),
        "countries": counter.countries(),
    }
