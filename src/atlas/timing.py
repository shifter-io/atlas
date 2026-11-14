"""INGEST-SCALE-1: one `timing:` line on stderr per measured stage of an hour's ingest or
calculation, `timing: <part> <stage> <seconds> s[ key=value ...]`, so the hours themselves give the
numbers the limits (atlas/sizing.py) are tuned with. Where to read them: "Reading the hourly
timings" in README.md.
"""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager

PREFIX = "timing:"


def line(part: str, stage: str, seconds: float, **detail) -> None:
    extra = "".join(f" {key}={value}" for key, value in detail.items())
    print(f"{PREFIX} {part} {stage} {seconds:.3f} s{extra}", file=sys.stderr, flush=True)


@contextmanager
def stage(part: str, name: str, **detail):
    """Time the block; its line is printed also when it fails (failed=1)."""
    began, failed = time.monotonic(), 1
    try:
        yield
        failed = 0
    finally:
        line(part, name, time.monotonic() - began, **detail, **({"failed": 1} if failed else {}))


def lines(text: str) -> list[str]:
    """The timing lines of a process's stderr text."""
    return [row for row in text.splitlines() if row.startswith(PREFIX)]
