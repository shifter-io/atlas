"""Fleet entries of a calculation: product groups together, each exit IP counted once."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from .aggregate import OUTCOMES, series, timed

SUMMED = ("completed", "successes", "response_body_bytes", *OUTCOMES)


def merged(rows):
    """The bucket rows of many groups merged per (bucket, time slot, target, light flag, census
    overlap, outcome flag), with what timed() and series() read of them: integer counters and
    latency bins summed, errors per category, the newest last_observed. timed() and series() of
    the merged rows equal those of the rows (their sums and per-slot histograms are the same
    integers), and a fleet window holds one row per group and bucket otherwise."""
    found = {}
    for r in rows:
        key = (r["bucket_start"], r["slot"], r["target"], r.get("light", True))
        key += (bool(r.get("during_census")), bool(r.get("outcome")))
        c, m = r["counters"], found.get(key)
        if m is None:
            names = ("bucket_start", "slot", "target", "light", "during_census", "outcome")
            sums = dict.fromkeys(SUMMED, 0) | {"errors": Counter(), "bins": Counter()}
            m = found[key] = dict(zip(names, key, strict=True)) | sums
            m["last_observed"] = r["last_observed"]
        for name in SUMMED if key[5] else SUMMED[:3]:  # outcome counters: v6 buckets only
            m[name] += c[name]
        m["errors"].update(c["errors"])
        m["bins"].update(dict(c["latency_histogram"]["bins"]))
        m["last_observed"] = max(m["last_observed"], r["last_observed"])
    for m in found.values():
        counters = {k: m.pop(k) for k in (*SUMMED, "errors")}
        m["counters"] = counters | {"latency_histogram": {"bins": sorted(m.pop("bins").items())}}
    return list(found.values())


def together(members, label):
    """members (product, rows, observations, member) of one window: their merged rows, their
    observations and the window's hours."""
    rows = merged(r for _, rows, _, _ in members for r in rows)
    seen = [o for _, _, obs, _ in members for o in obs]
    return rows, seen, members[0][0]["windows"][label]["period"]["hours"]


def fleet_entry(cohort, label, members, minimum, slot_minutes, joined=None):
    """members together in one window: requests and successes added, success time-weighted and
    light-load latency merged by timed(), and every exit IP counted once however many of them
    saw it. joined: together(members, label), when the caller has it."""
    rows, seen, hours = joined or together(members, label)
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
        rows, seen, hours = joined = together(members, label)
        entry = fleet_entry("*", label, members, minimum, slot_minutes, joined)
        since = as_of - timedelta(hours=hours)
        entry["series"] = series(
            rows, seen, since, as_of, hours, latency_target=timed_target, minimum=minimum
        )
        out.append(entry)
    return out
