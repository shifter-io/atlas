"""REPLAY-STATS-1: pg_stat_statements as the replay's server-side statement source (REPLAY.md,
"Statement timing").

A window is the change of the database's counters between two `measure.snapshot`s: per key
(queryid, or another role's statements by role) its calls, total exec ms and rows. No reset is
needed. `max_exec_time` is cumulative, so it is kept as `max_ms_since_reset`, never as the
window's maximum. Texts are shapes (`measure.shape`): 200 characters, literals as `?`.
"""

from __future__ import annotations

import measure

SUMS = ("calls", "ms", "rows")


def window(before: dict, after: dict, covers: dict) -> dict:
    """The statements that ran between two snapshots, slowest first, with their totals."""
    shapes = []
    for key, (text, calls, total, rows, top) in after.items():
        _, calls_before, total_before, rows_before, _ = before.get(key, (text, 0, 0.0, 0, None))
        if calls > calls_before:
            found = {"queryid": key, "statement": measure.shape(text)}
            found["calls"] = calls - calls_before
            found |= {"ms": round(total - total_before, 3), "rows": rows - rows_before}
            shapes.append(found | {"max_ms_since_reset": round(top or 0.0, 3)})
    shapes.sort(key=lambda row: (-row["ms"], row["statement"]))
    return {"covers": covers, **totals([{"shapes": shapes}]), "shapes": shapes}


def windows(record: dict) -> list[dict]:
    """The hour's windows (replay.py, Replay.mark)."""
    return record.get("server") or []


def totals(found: list[dict]) -> dict:
    """Calls, total ms, rows and mean ms of windows' shapes."""
    sums = {key: sum(row[key] for w in found for row in w["shapes"]) for key in SUMS}
    sums["ms"] = round(sums["ms"], 3)
    return sums | {"mean_ms": round(sums["ms"] / sums["calls"], 3) if sums["calls"] else None}


def merge(found: list[dict]) -> list[dict]:
    """Windows' shapes added up by statement shape, slowest first, with their mean ms."""
    shapes: dict[str, dict] = {}
    for row in (row for w in found for row in w["shapes"]):
        entry = shapes.setdefault(row["statement"], dict.fromkeys(SUMS, 0))
        for key in SUMS:
            entry[key] += row[key]
        top = max(entry.get("max_ms_since_reset", 0.0), row["max_ms_since_reset"])
        entry["max_ms_since_reset"] = top
    rows = []
    for text, entry in sorted(shapes.items(), key=lambda item: (-item[1]["ms"], item[0])):
        mean = round(entry["ms"] / entry["calls"], 3)
        rows.append({"statement": text, **entry, "ms": round(entry["ms"], 3), "mean_ms": mean})
    return rows
