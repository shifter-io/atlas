"""Shared helpers of the hour formula tests: hour files, network directories and counts."""

from __future__ import annotations

import gzip
import hashlib
import json

RUN = "20261025t160000z-0000000000aa"


def _record(ip, countries, compact=True):
    record = {
        "ip": ip,
        "seen_count": sum(countries.values()),
        "regions": {"r": 1},
        "countries": countries,
        "enrichment": {"asn": 64500},
        "dbip_release": "x",
    }
    if compact:
        return json.dumps(record, separators=(",", ":"))
    return json.dumps(record)


def _hour(directory, name, records, compact=True):
    """Write one hour file and return its manifest slot."""
    data = gzip.compress(("\n".join(_record(ip, c, compact) for ip, c in records) + "\n").encode())
    (directory / name).write_bytes(data)
    return {
        "status": "present",
        "file": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "unique_ips": len(records),
        "request_rows": sum(sum(c.values()) for _, c in records),
        "requests": sum(sum(c.values()) for _, c in records) + 1,
        "successes": sum(sum(c.values()) for _, c in records),
        "runs": [RUN],
    }


def _network(tmp_path, hours, name="demo", extra_slots=None):
    """A network directory: hours maps 'HH' to its records; other hours are requests_no_ips."""
    directory = tmp_path / name
    directory.mkdir()
    slots = {}
    for key in (f"{hour:02d}" for hour in range(24)):
        if key in hours:
            slots[key] = _hour(directory, f"hour-20261025T{key}Z.jsonl.gz", hours[key])
        else:
            slots[key] = {
                "status": "requests_no_ips",
                "unique_ips": 0,
                "request_rows": 0,
                "requests": 3,
                "runs": [RUN],
            }
    slots.update(extra_slots or {})
    manifest = {
        "schema_version": 2,
        "network": name,
        "product": "residential",
        "dbip_release": {"sha256": "d" * 64},
        "slots": slots,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


def _rewrite(directory, change):
    manifest = json.loads((directory / "manifest.json").read_text())
    change(manifest)
    (directory / "manifest.json").write_text(json.dumps(manifest))


A, B, C, D = "192.0.2.1", "198.51.100.2", "203.0.113.3", "2001:db8::4"


def _counts(hour_ips, countries, day=None, status=None):
    status = status or ["present" if ips else "requests_no_ips" for ips in hour_ips]
    return {
        "network": "demo",
        "product": "residential",
        "manifest_sha256": "m" * 64,
        "dbip_release_sha256": "d" * 64,
        "hours": [
            {
                "status": state,
                "unique_ips": ips,
                "requests": ips * 2,
                "attempted_requests": ips * 2 + 1,
                "runs": [RUN] if state != "missing" else [],
            }
            for state, ips in zip(status, hour_ips, strict=True)
        ],
        "day_unique_ips": day if day is not None else sum(hour_ips),
        "countries": countries,
    }


def _country(ips, day=None):
    return {
        "unique_ips": ips,
        "requests": [value * 3 for value in ips],
        "day_unique_ips": day if day is not None else max(ips),
    }
