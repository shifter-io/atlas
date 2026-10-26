"""Exact counters and a mergeable, sparse 1 ms ceiling histogram."""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .models import Outcome

# LocalEnricher.lookup() fields, split the way storage keys them:
# enrichment_locations and enrichment_networks.
LOCATION_FIELDS = (
    "country_code", "country", "region_code", "region", "city", "latitude", "longitude",
)
NETWORK_FIELDS = (
    "isp", "organization", "asn", "asn_organization", "connection_type", "usage_type",
)
# Verdict -> counter attribute, for the non-ok verdicts that have one.
VERDICT_COUNTERS = {
    "challenge": "challenges", "block": "blocks", "timeout": "timeouts",
    "proxy_error": "proxy_errors",
}


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="microseconds")


def bucket_of(timestamp: float) -> int:
    return int(timestamp // 300) * 300


@dataclass(slots=True)
class Histogram:
    counts: Counter[int] = field(default_factory=Counter)

    def add(self, milliseconds: float) -> None:
        self.counts[max(0, math.ceil(milliseconds))] += 1

    def merge(self, other: Histogram) -> None:
        self.counts.update(other.counts)

    def percentiles(self) -> dict[str, float | None]:
        total = self.counts.total()
        result: dict[str, float | None] = dict.fromkeys(("p50", "p90", "p95", "p99"))
        if not total:
            return result
        ordered = sorted(self.counts.items())
        for label, fraction in (("p50", .5), ("p90", .9), ("p95", .95), ("p99", .99)):
            rank, seen = math.ceil(total * fraction), 0
            for value, count in ordered:
                seen += count
                if seen >= rank:
                    result[label] = value
                    break
        return result

    def mean(self) -> float | None:
        """Mean latency in ms over the bins, rounded to 0.1 ms; None when empty.

        Each bin is the ceiling of the latency in ms, so with the 0.05 ms rounding the result
        is under 1.05 ms above the true mean.
        """
        total = self.counts.total()
        if not total:
            return None
        return round(sum(value * count for value, count in self.counts.items()) / total, 1)

    def export(self) -> dict[str, Any]:
        return {"format": "ceil_ms_v1", "bins": sorted(self.counts.items())}


@dataclass(slots=True)
class Counters:
    completed: int = 0
    successes: int = 0
    errors: Counter[str] = field(default_factory=Counter)
    statuses: Counter[str] = field(default_factory=Counter)
    body_bytes: int = 0
    histogram: Histogram = field(default_factory=Histogram)
    challenges: int = 0
    blocks: int = 0
    timeouts: int = 0
    proxy_errors: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    # Run totals only (RunSummary.redirects); not part of bucket or geo exports.
    redirects: int = 0

    def add(self, result: Outcome) -> None:
        self.completed += 1
        self.redirects += result.redirected
        self.body_bytes += result.response_body_bytes
        self.bytes_up += result.bytes_up
        self.bytes_down += result.bytes_down
        if result.status is not None:
            self.statuses[str(result.status)] += 1
        if result.error:
            self.errors[result.error] += 1
            counter = VERDICT_COUNTERS.get(result.verdict or "")
            if counter:
                setattr(self, counter, getattr(self, counter) + 1)
        else:
            self.successes += 1
            self.histogram.add(result.latency_ms)

    def verdicts(self) -> dict[str, int]:
        """The six typed counters shared by run totals, buckets and progress."""
        return {
            "challenges": self.challenges, "blocks": self.blocks, "timeouts": self.timeouts,
            "proxy_errors": self.proxy_errors, "bytes_up": self.bytes_up,
            "bytes_down": self.bytes_down,
        }

    def export(self) -> dict[str, Any]:
        return {
            "completed": self.completed, "successes": self.successes,
            "errors": dict(self.errors), "status_codes": dict(self.statuses),
            "response_body_bytes": self.body_bytes,
            "latency_histogram": self.histogram.export(),
            **self.verdicts(),
        }

    def export_geo(self) -> dict[str, Any]:
        """The run_geo_buckets columns (no proxy_errors: those never have an exit IP)."""
        return {
            "completed": self.completed, "successes": self.successes,
            "challenges": self.challenges, "blocks": self.blocks, "timeouts": self.timeouts,
            "bytes_up": self.bytes_up, "bytes_down": self.bytes_down,
            "latency_histogram": self.histogram.export(),
        }

