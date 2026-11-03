"""Fleet entries of a calculation: product groups together, each exit IP counted once."""

from __future__ import annotations

from datetime import timedelta

from .aggregate import series, timed


def fleet_entry(cohort, label, members, minimum, slot_minutes):
    """members (product, rows, observations, member) together in one window: requests and
    successes added, success time-weighted and light-load latency merged by timed(), and every
    exit IP counted once however many of them saw it."""
    rows = [r for _, rows, _, _ in members for r in rows]
    seen = [o for _, _, obs, _ in members for o in obs]
    hours = members[0][0]["windows"][label]["period"]["hours"]
    return {
        "cohort": cohort,
        "window": label,
        "products": len(members),
        "providers": len({p["provider"] for p, *_ in members}),
        "metrics": timed(rows, hours, minimum, slot_minutes=slot_minutes),
        "unique_ips": len({o["ip_id"] for o in seen}),
    }


def fleet_totals(fleet_groups, as_of, minimum, slot_minutes, timed_target):
    """Every product group of every cohort together, per window: fleet entries with cohort "*",
    plus a series of the window's buckets (5 minutes for 24h, days otherwise) with distinct IPs
    per bucket. It has no availability, which is a per-product fact."""
    out = []
    for label in dict.fromkeys(label for _, label in fleet_groups):
        members = [m for (_, w), ms in fleet_groups.items() if w == label for m in ms]
        entry = fleet_entry("*", label, members, minimum, slot_minutes)
        rows = [r for _, rows, _, _ in members for r in rows]
        seen = [o for _, _, obs, _ in members for o in obs]
        hours = members[0][0]["windows"][label]["period"]["hours"]
        since = as_of - timedelta(hours=hours)
        entry["series"] = series(
            rows, seen, since, as_of, hours, latency_target=timed_target, minimum=minimum
        )
        out.append(entry)
    return out
