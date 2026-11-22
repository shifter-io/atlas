"""The methodology's [[browser_exclusions]]: browser runs left out of every figure
(CHROME-STALE-EXCLUDE-1; docs/browser.html "Runs left out").

An entry names a run target, an engine and a span of run starts, [started_from, started_before)
in UTC, with its reason. A browser run of that target and engine that started in the span
measured our browser, not the network: for example a run on a host whose Chrome was behind the
current Stable major, which a site may answer with its bot check almost every time.
browser.excluded() drops its bucket rows where the calculation reads them (incremental.read(),
both modes), so it counts in no window, day, chart point, target figure or score, Success x/10
or bill, for every network the same way. The run and its rows stay stored.
"""

from __future__ import annotations

from datetime import datetime

from ..browser.contracts import RUN_TARGETS
from ..browser.engines import ENGINES

FIELDS = ("target", "engine", "started_from", "started_before", "reason")


def spans(config):
    """[[browser_exclusions]] as {(target, engine): [(started_from, started_before), ...]}, the
    times aware datetimes ({} without the setting); ValueError on an invalid entry."""
    entries = config.get("browser_exclusions", [])
    if not isinstance(entries, list):
        raise ValueError("Invalid browser_exclusions")
    found = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != set(FIELDS):
            raise ValueError("Invalid browser_exclusions")
        reason = entry["reason"]
        if entry["target"] not in RUN_TARGETS or entry["engine"] not in ENGINES:
            raise ValueError("Invalid browser_exclusions")
        if not isinstance(reason, str) or not reason:
            raise ValueError("Invalid browser_exclusions")
        start, end = _time(entry["started_from"]), _time(entry["started_before"])
        if not start < end:
            raise ValueError("Invalid browser_exclusions")
        found.setdefault((entry["target"], entry["engine"]), []).append((start, end))
    return found


def _time(text):
    """An ISO 8601 time with its UTC offset ("2026-11-17T19:07:00Z")."""
    try:
        at = datetime.fromisoformat(text) if isinstance(text, str) else None
    except ValueError:
        at = None
    if at is None or at.utcoffset() is None:
        raise ValueError("Invalid browser_exclusions")
    return at
