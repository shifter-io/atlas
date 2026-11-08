"""SIM-PROFILE-1: one network's figures from its LC-ENRICH-1 hour files (SIMULATE.md).

One pass per hour file, gzip read line by line. IPs are kept only as the process's salted
64-bit hashes (hash(bytes)), in sorted arrays per hour and requested country, to count the
day's distinct IPs and the recurrence; no IP, hash or address leaves this module.
"""

from __future__ import annotations

import gzip
import heapq
import json
import re
from array import array
from datetime import UTC, datetime
from pathlib import Path

from proxy_index.formula.aggregate import HOURS, FormulaError, load_manifest, parse_record

TOP_PLACES = 500
PLACE = ("country_code", "region", "city", "asn", "usage_type")
DAY_HOURS = 24
_ENRICHMENT = re.compile(rb'"enrichment":(\{[^{}]*\})')
_FILE_HOUR = re.compile(r"hour-(\d{8}T\d{2})Z")


class ProfileError(ValueError):
    """A source does not have the shape the profile reads; the message names the file."""


def share(part: int, whole: int) -> float | None:
    return round(part / whole, 6) if whole else None


def bucket(seen: int) -> str:
    """The log-2 bucket of a seen_count: 1, 2-3, 4-7, ..."""
    low = 1 << (seen.bit_length() - 1)
    return str(low) if low == 1 else f"{low}-{2 * low - 1}"


def slot_time(slot: dict) -> datetime | None:
    if slot.get("hour_start"):
        return datetime.fromisoformat(slot["hour_start"].replace("Z", "+00:00"))
    found = _FILE_HOUR.search(slot.get("file", ""))
    return datetime.strptime(found[1], "%Y%m%dT%H").replace(tzinfo=UTC) if found else None


def _errors(slot: dict) -> tuple[int, dict]:
    errors = slot.get("errors", 0)
    if isinstance(errors, dict):
        return sum(errors.values()), dict(sorted(errors.items()))
    return int(errors), {}


class _Places:
    """The (country, region, city, ASN, usage type) tuples of one requested country, by IPs."""

    def __init__(self):
        self.tuples: dict[tuple, list] = {}  # key -> [IPs, latitude, longitude, ASN name]
        self.ips = 0
        self.unmatched = 0

    def add(self, enrichment: dict | None) -> None:
        self.ips += 1
        if not enrichment or not enrichment.get("matched", bool(enrichment.get("country_code"))):
            self.unmatched += 1
            return
        key = tuple(enrichment.get(field) for field in PLACE)
        entry = self.tuples.get(key)
        if entry is None:
            coordinates = (enrichment.get("latitude"), enrichment.get("longitude"))
            self.tuples[key] = [1, *coordinates, enrichment.get("asn_organization")]
        else:
            entry[0] += 1

    def summary(self) -> dict:
        ranked = sorted(self.tuples.items(), key=lambda item: (-item[1][0], repr(item[0])))
        top = [
            dict(zip(PLACE, key, strict=True))
            | {"asn_name": name, "latitude": lat, "longitude": lon, "share": share(n, self.ips)}
            for key, (n, lat, lon, name) in ranked[:TOP_PLACES]
        ]
        tail = sum(entry[0] for _, entry in ranked[TOP_PLACES:])
        return {
            "ips": self.ips,
            "top": top,
            "tail_share": share(tail, self.ips),
            "unmatched_share": share(self.unmatched, self.ips),
        }


class _Hour:
    """One hour file's IP hashes and request rows per requested country, and its totals."""

    def __init__(self):
        self.hashes: dict[str, array] = {}
        self.rows: dict[str, int] = {}
        self.records = 0
        self.seen = 0


class Network:
    """The running counts of one network directory, filled one hour file at a time."""

    def __init__(self, folder: Path):
        self.name = folder.name
        self.folder = folder
        try:
            _, self.manifest = load_manifest(folder)
        except (FormulaError, KeyError, ValueError, OSError) as error:
            raise ProfileError(f"{folder.name}/manifest.json: {error}") from None
        self.hourly: dict[str, dict[str, array]] = {}  # hour -> country -> sorted IP hashes
        self.rows: dict[str, dict[str, int]] = {}  # hour -> country -> request rows
        self.seen: dict[int, int] = {}
        self.places: dict[str, _Places] = {}
        self._enrichments: dict[bytes, dict] = {}

    def slots(self):
        for key in HOURS:
            yield key, self.manifest["slots"].get(key, {"status": "missing"})

    def read(self) -> None:
        for key, slot in self.slots():
            if slot.get("status") == "present":
                self._read_hour(key, slot)

    def _enrichment(self, line: bytes) -> dict | None:
        found = _ENRICHMENT.search(line)
        raw = found[1] if found else None
        if raw is not None and raw in self._enrichments:
            return self._enrichments[raw]
        try:
            value = json.loads(raw) if raw is not None else json.loads(line).get("enrichment")
        except ValueError:
            value = json.loads(line).get("enrichment")
        if raw is not None and len(self._enrichments) < 1 << 18:
            self._enrichments[raw] = value
        return value

    def _read_hour(self, key: str, slot: dict) -> None:
        path = self.folder / slot["file"]
        hour = _Hour()
        try:
            with gzip.open(path, "rb") as lines:
                for line in (line for line in lines if line.strip()):
                    self._add(line, hour)
        except (OSError, EOFError, FormulaError, ValueError) as error:
            raise ProfileError(f"{self.name}/{path.name}: {error}") from None
        if hour.records != slot["unique_ips"] or hour.seen != slot["request_rows"]:
            raise ProfileError(
                f"{self.name}/{path.name}: {hour.records} IPs / {hour.seen} rows, manifest "
                f"{slot['unique_ips']} / {slot['request_rows']}"
            )
        self.hourly[key] = {
            code: array("q", sorted(values)) for code, values in hour.hashes.items()
        }
        self.rows[key] = hour.rows

    def _add(self, line: bytes, hour: _Hour) -> None:
        ip, seen, countries = parse_record(line)
        hour.records, hour.seen = hour.records + 1, hour.seen + seen
        self.seen[seen.bit_length()] = self.seen.get(seen.bit_length(), 0) + 1
        enrichment = self._enrichment(line)
        for code, count in countries.items():
            if count > 0:
                hour.hashes.setdefault(code, array("q")).append(hash(ip))
                hour.rows[code] = hour.rows.get(code, 0) + count
                self.places.setdefault(code, _Places()).add(enrichment)

    def hours(self) -> list[dict]:
        found = []
        for key, slot in self.slots():
            errors, kinds = _errors(slot)
            start = slot_time(slot)
            countries = {
                code: {"unique_ips": len(values), "request_rows": self.rows[key][code]}
                for code, values in sorted(self.hourly.get(key, {}).items())
            }
            found.append(
                {
                    "hour": key,
                    "status": slot.get("status", "missing"),
                    "hour_start": start.isoformat() if start else None,
                    "unique_ips": int(slot.get("unique_ips", 0)),
                    "request_rows": int(slot.get("request_rows", 0)),
                    "attempted_requests": int(slot.get("requests", 0)),
                    "successes": int(slot.get("successes", 0)),
                    "errors": errors,
                    "errors_by_kind": kinds,
                    "countries": countries,
                }
            )
        return found

    def summary(self) -> tuple[dict, dict]:
        """(the network's profile entry, its real_24h enrichment totals)."""
        days = {code: _distinct(self._country_hours(code)) for code in sorted(self.places)}
        shared, distinct = _shared(days)
        recurrence = self.recurrence()
        recurrence["other_country"] = {
            "basis": "the country's distinct IPs of the 24 hours also seen for another country",
            "share": share(sum(shared.values()), sum(len(v) for v in days.values())),
            "countries": {code: share(shared[code], len(v)) for code, v in days.items()},
        }
        seen = {bucket(1 << (k - 1)): self.seen[k] for k in sorted(self.seen)}
        entry = {
            "product": self.manifest.get("product"),
            "hours": self.hours(),
            "seen_count_log2": seen,
            "recurrence": recurrence,
            "places": {code: places.summary() for code, places in sorted(self.places.items())},
        }
        slots = [slot for _, slot in self.slots()]
        totals = {
            "unique_ips_hour_sum": sum(int(s.get("unique_ips", 0)) for s in slots),
            "unique_ips_distinct": distinct,
            "request_rows": sum(int(s.get("request_rows", 0)) for s in slots),
            "attempted_requests": sum(int(s.get("requests", 0)) for s in slots),
        }
        return entry, totals

    def _country_hours(self, code: str) -> list[array]:
        return [hours[code] for hours in self.hourly.values() if code in hours]

    def recurrence(self) -> dict:
        times = {key: slot_time(slot) for key, slot in self.slots() if key in self.hourly}
        pairs, gap = gap_pairs(times)
        counts: dict[str, list[int]] = {}
        for earlier, later in pairs:
            for code, ips in self.hourly[later].items():
                before = self.hourly[earlier].get(code)
                both = len(set(before).intersection(ips)) if before else 0
                total = counts.setdefault(code, [0, 0])
                total[0], total[1] = total[0] + both, total[1] + len(ips)
        basis = "24 h apart" if gap == DAY_HOURS else "the measured gap closest to 24 h"
        return {
            "gap_hours": gap,
            "basis": basis if gap is not None else "fewer than two hours with IPs",
            "pairs": len(pairs),
            "share": share(sum(c[0] for c in counts.values()), sum(c[1] for c in counts.values())),
            "countries": {code: share(*counts[code]) for code in sorted(counts)},
        }


def gap_pairs(times: dict[str, datetime | None]) -> tuple[list[tuple[str, str]], int | None]:
    """The (earlier, later) hours 24 h apart, else those of the measured gap closest to 24 h
    (ties: the shorter); hours: their hour_start."""
    known = sorted((t, key) for key, t in times.items() if t is not None)
    spans = [
        (key_a, key_b, round((t_b - t_a).total_seconds() / 3600))
        for i, (t_a, key_a) in enumerate(known)
        for t_b, key_b in known[i + 1 :]
    ]
    if not spans:
        return [], None
    gap = min({span for *_, span in spans}, key=lambda g: (abs(g - DAY_HOURS), g))
    return [(a, b) for a, b, span in spans if span == gap], gap


def _distinct(arrays: list[array]) -> array:
    found, last = array("q"), None
    for value in heapq.merge(*arrays):
        if value != last:
            found.append(value)
            last = value
    return found


def _tagged(values: array, code: str):
    for value in values:
        yield value, code


def _shared(days: dict[str, array]) -> tuple[dict[str, int], int]:
    """Per country, its distinct IPs also seen for another country; and the network's distinct
    IPs. Each array is one country's sorted, distinct hashes."""
    shared = dict.fromkeys(days, 0)
    distinct, last, group = 0, None, []
    for value, code in heapq.merge(*(_tagged(values, code) for code, values in days.items())):
        if value != last:
            _count_group(group, shared)
            distinct, last, group = distinct + 1, value, []
        group.append(code)
    _count_group(group, shared)
    return shared, distinct


def _count_group(group: list[str], shared: dict[str, int]) -> None:
    if len(group) > 1:
        for code in group:
            shared[code] += 1
