"""DAILY-DIAG-1: a run's tries, successes and error classes per 5 seconds (the summary's
`slices`).

The 5-minute buckets cannot show when inside a run of a few minutes its errors came. Each
completed try is counted in the slice of the run's elapsed time it ended in (coordinator
receipt, as the buckets): slice i covers [started_at + i*seconds, started_at + (i+1)*seconds).
Its error class is its error code (`timeout`, `proxy_connect`, `dns`, ...), except a target
status error (`http_status`), which is `http_429`, `http_4xx` or `http_5xx` by its status.

A run longer than LIMIT slices doubles the slice width, merging pairs, until it fits, so the
field never holds more than LIMIT slices whatever the run's length (120 slices of 5 s cover
10 minutes, of 10 s 20 minutes) and its counts stay exact: a few KB in a summary. The export
is one array per counter, all of one length (up to the last slice with a try); an error class
only when it occurred. Summaries written before the field existed lack it: readers treat a
missing key as unknown.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .models import Outcome

SECONDS = 5
LIMIT = 120


def error_class(result: Outcome) -> str | None:
    """The try's error class; None for a success."""
    status = result.status
    if result.error == "http_status" and status is not None and 400 <= status < 600:
        return "http_429" if status == 429 else f"http_{status // 100}xx"
    return result.error


@dataclass(slots=True)
class Slices:
    seconds: int = SECONDS
    tries: Counter[int] = field(default_factory=Counter)
    successes: Counter[int] = field(default_factory=Counter)
    errors: dict[str, Counter[int]] = field(default_factory=dict)

    def add(self, elapsed: float, result: Outcome) -> None:
        """Count one try that ended `elapsed` seconds after the run began."""
        while int(max(0.0, elapsed) // self.seconds) >= LIMIT:
            self._widen()
        index = int(max(0.0, elapsed) // self.seconds)
        self.tries[index] += 1
        name = error_class(result)
        if name is None:
            self.successes[index] += 1
        else:
            self.errors.setdefault(name, Counter())[index] += 1

    def _widen(self) -> None:
        self.seconds *= 2
        for counts in (self.tries, self.successes, *self.errors.values()):
            merged: Counter[int] = Counter()
            for index, count in counts.items():
                merged[index // 2] += count
            counts.clear()
            counts.update(merged)

    def export(self) -> dict[str, Any]:
        size = max(self.tries, default=-1) + 1

        def column(counts: Counter[int]) -> list[int]:
            return [counts[index] for index in range(size)]

        return {
            "seconds": self.seconds,
            "tries": column(self.tries),
            "successes": column(self.successes),
            "errors": {name: column(counts) for name, counts in sorted(self.errors.items())},
        }
