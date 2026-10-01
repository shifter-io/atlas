"""Exact counters and a mergeable, sparse 1 ms ceiling histogram."""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .models import Outcome


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

    def add(self, result: Outcome) -> None:
        self.completed += 1
        self.body_bytes += result.response_body_bytes
        if result.status is not None:
            self.statuses[str(result.status)] += 1
        if result.error:
            self.errors[result.error] += 1
        else:
            self.successes += 1
            self.histogram.add(result.latency_ms)

    def export(self) -> dict[str, Any]:
        return {
            "completed": self.completed, "successes": self.successes,
            "errors": dict(self.errors), "status_codes": dict(self.statuses),
            "response_body_bytes": self.body_bytes,
            "latency_histogram": self.histogram.export(),
        }

