"""Shared helpers of the schema v6 tests: a v6 census artifact (`census`) with its geo artifact,
and the summary readers and writers. test_storage_v6.py keeps these names importable for the
other test modules that use them.
"""

import gzip
import hashlib
import json

from storage_cases import measurement

from proxy_index.storage import OUTCOME_KEYS

BERLIN = {
    "country_code": "DE",
    "country": "Germany",
    "region_code": "BE",
    "region": "Berlin",
    "city": "Berlin",
    "latitude": 52.52,
    "longitude": 13.4,
}
NETWORK = {
    "isp": "Deutsche Telekom",
    "organization": "Telekom",
    "asn": 3320,
    "asn_organization": "Deutsche Telekom AG",
    "connection_type": "Cable/DSL",
    "usage_type": "residential",
}


def load(directory):
    return json.loads((directory / "summary.json").read_text())


def save(directory, summary):
    (directory / "summary.json").write_text(json.dumps(summary))
    return summary


def legacy(directory):
    """Strip every v6 key, whatever the runner emits, leaving a v5-era artifact."""
    summary = load(directory)
    for counters in (summary, *summary["buckets"]):
        for key in OUTCOME_KEYS:
            counters.pop(key, None)
    summary.pop("stop_reason", None)
    summary["artifacts"].pop("geo", None)
    (directory / "geo.jsonl.gz").unlink(missing_ok=True)
    return save(directory, summary)


def write_geo(directory, summary, rows):
    path = directory / "geo.jsonl.gz"
    with gzip.open(path, "wt") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
    summary["artifacts"]["geo"] = {
        "file": path.name,
        "rows": len(rows),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    return save(directory, summary)


async def census(tmp_path, *, location=BERLIN, network=NETWORK, **options):
    """A v6 artifact: one more request that timed out without an exit address, typed outcome
    counters, requested=null and a geo artifact splitting the bucket in two."""
    directory = await measurement(tmp_path, **options)
    summary = legacy(directory)
    bucket = summary["buckets"][0]
    for counters in (summary, bucket):
        counters["completed"] += 1
        counters["errors"]["timeout"] = counters["errors"].get("timeout", 0) + 1
    for name in ("started", "total_errors"):
        summary[name] += 1
    summary["requested"] = summary["config"]["requests"] = None
    summary["config"]["stop_rule"] = "unique_ratio"
    # The runner records min_sample and stall_seconds for every non-count run (RunConfig.public).
    summary["config"]["min_sample"] = 1000
    summary["config"]["stall_seconds"] = 600
    summary["stop_reason"] = "unique_ratio"
    outcomes = {
        "challenges": 0,
        "blocks": 0,
        "timeouts": 1,
        "proxy_errors": 0,
        "bytes_up": 300,
        "bytes_down": 900,
    }
    summary |= outcomes
    bucket |= outcomes
    rows = [
        {
            "bucket_start": bucket["bucket_start"],
            "location": location,
            "network": network,
            "completed": bucket["successes"],
            "successes": bucket["successes"],
            "challenges": 0,
            "blocks": 0,
            "timeouts": 0,
            "bytes_up": 200,
            "bytes_down": 800,
            "latency_histogram": bucket["latency_histogram"],
        },
        {
            "bucket_start": bucket["bucket_start"],
            "location": None,
            "network": None,
            "completed": 1,
            "successes": 0,
            "challenges": 0,
            "blocks": 0,
            "timeouts": 1,
            "bytes_up": 100,
            "bytes_down": 100,
            "latency_histogram": {"format": "ceil_ms_v1", "bins": []},
        },
    ]
    return directory, write_geo(directory, summary, rows)
