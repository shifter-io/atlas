"""LATENCY-ALWAYS-1 (the owner's rule: the latency change always shows a value): a window whose
latency change has no value this build (latency_days.py: no pair passes, not even on the
countries both days have) keeps the last value computed for its network, the current build's,
with its own periods: their basis and last day say what it compares and for when (the API's
latencyDeltaBasis and latencyDeltaAsOf). A network never computed keeps its null and reason.
A value for a later day than the window's (the current build of a later cutoff, a replay) is
never carried back. The chain needs no table of its own: each build carries the value on.
"""

from __future__ import annotations

from datetime import datetime

from psycopg.rows import tuple_row

# The current build's latency changes with a value, by network (provider, kind) and window.
CURRENT = (
    "SELECT n->>'provider',n->>'kind',w.key,w.value->'latency_day_delta' "
    "FROM calculated_snapshots s JOIN current_calculation USING(build_id),"
    "jsonb_array_elements(s.payload->'networks') n,jsonb_each(n->'windows') w "
    "WHERE w.value->'latency_day_delta'->>'pct' IS NOT NULL"
)


def carry(conn, networks):
    """Give each network window whose latency_day_delta has no value the current build's value
    for it, unless that one's last period ends after the window's (marked "carried")."""
    missing = [
        (entry, label, window)
        for entry in networks
        for label, window in entry["windows"].items()
        if "latency_day_delta" in window and window["latency_day_delta"]["pct"] is None
    ]
    if not missing:
        return
    found = {
        (provider, kind, label): delta
        for provider, kind, label, delta in conn.cursor(row_factory=tuple_row).execute(CURRENT)
    }
    for entry, label, window in missing:
        old = found.get((entry["provider"], entry["kind"], label))
        if old is not None and len(old.get("days", ())) == 2:
            old = parsed(old)
            if old["days"][1]["end"] <= window["latency_day_delta"]["days"][1]["end"]:
                window["latency_day_delta"] = old | {"carried": True}


def parsed(delta):
    """A stored latency_day_delta with its times as datetimes, as the calculation makes them."""
    days = [d | {k: datetime.fromisoformat(d[k]) for k in ("start", "end")} for d in delta["days"]]
    return delta | {"computed_at": datetime.fromisoformat(delta["computed_at"]), "days": days}
