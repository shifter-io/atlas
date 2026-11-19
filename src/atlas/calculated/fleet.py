"""Fleet entries of a calculation: product groups together, each exit IP counted once."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import timedelta

from .aggregate import OUTCOMES, SeriesSpec, series, series_axis, timed

SUMMED = ("completed", "successes", "response_body_bytes", *OUTCOMES)
# Cohort prefix of the per-kind fleet totals (fleet_totals()).
KIND = "*:"
ROLLING = timedelta(hours=24)  # the span of a 24h IP slot (ip_spans())


def merged(rows):
    """The bucket rows of many groups merged per (bucket, time slot, target, light and loaded
    flags, census overlap, outcome flag), with what timed() and series() read of them: integer
    counters and latency bins summed, errors per category, the newest last_observed. timed() and
    series() of the merged rows equal those of the rows (their sums and per-slot histograms are
    the same integers), and a fleet window holds one row per group and bucket otherwise."""
    found = {}
    for r in rows:
        key = (r["bucket_start"], r["slot"], r["target"], r.get("light", True))
        key += (bool(r.get("loaded")), bool(r.get("during_census")), bool(r.get("outcome")))
        c, m = r["counters"], found.get(key)
        if m is None:
            names = ("bucket_start", "slot", "target", "light", "loaded", "during_census")
            names += ("outcome",)
            sums = dict.fromkeys(SUMMED, 0) | {"errors": Counter(), "bins": Counter()}
            m = found[key] = dict(zip(names, key, strict=True)) | sums
            m["last_observed"] = r["last_observed"]
        for name in SUMMED if key[6] else SUMMED[:3]:  # outcome counters: v6 buckets only
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


def fleet_entry(cohort, label, members, minimum, slot_minutes):
    """members together in one window: requests and successes added, success time-weighted and
    light-load latency merged by timed(), and every exit IP counted once however many of them
    saw it."""
    return _entry(cohort, label, members, together(members, label), (minimum, slot_minutes))


def _entry(cohort, label, members, joined, timing):
    """fleet_entry() of members whose together() is joined; timing: (minimum, slot_minutes)."""
    rows, seen, hours = joined
    since = members[0][0]["windows"][label]["period"]["start"]  # timed(): the cut slot
    return {
        "cohort": cohort,
        "window": label,
        "products": len(members),
        "providers": len({p["provider"] for p, *_ in members}),
        "metrics": timed(rows, hours, timing[0], slot_minutes=timing[1], since=since),
        "unique_ips": len({o["ip_id"] for o in seen}),
    }


def fleet_totals(fleet_groups, as_of, minimum, slot_minutes, timed_target):
    """Every product group of every cohort together, per window: fleet entries with cohort "*",
    plus a series of the window's buckets (5 minutes for 24h, days otherwise) with distinct IPs
    per bucket, and (cohort "*" only) its IP slots (ip_spans()), counted over the longest
    window's observations, which hold every slot's span. It has no availability, which is a
    per-product fact. Then the same entries over each kind's groups alone (cohort "*:<kind>",
    kinds by name), for the market totals per kind.
    """
    timing = (as_of, minimum, slot_minutes, timed_target)
    out = _totals("*", fleet_groups, timing)
    labelled = [
        (label, ms, ms[0][0]["windows"][label]["period"]["hours"])
        for label, ms in _labelled(fleet_groups)
    ]
    longest = max(labelled, key=lambda found: found[2], default=(None, [], 0))[1]
    for entry, (_, _, hours) in zip(out, labelled, strict=True):
        pairs = ip_spans(as_of, hours)
        entry["ip_slots"] = ip_slots(pairs, slot_counts(sorted({s for _, s in pairs}), longest))
    for kind in sorted({p["kind"] for ms in fleet_groups.values() for p, *_ in ms}):
        own = {key: [m for m in ms if m[0]["kind"] == kind] for key, ms in fleet_groups.items()}
        out += _totals(f"{KIND}{kind}", {key: ms for key, ms in own.items() if ms}, timing)
    return out


def _totals(cohort, fleet_groups, timing):
    """The members of fleet_groups together per window, as cohort; timing: (as_of, minimum,
    slot_minutes, timed_target)."""
    as_of, minimum, slot_minutes, timed_target = timing
    out = []
    for label, members in _labelled(fleet_groups):
        rows, seen, hours = joined = together(members, label)
        entry = _entry(cohort, label, members, joined, (minimum, slot_minutes))
        since = as_of - timedelta(hours=hours)
        spec = SeriesSpec(since, as_of, hours, latency_target=timed_target, minimum=minimum)
        entry["series"] = series(rows, seen, spec)
        out.append(entry)
    return out


def _labelled(fleet_groups):
    """[(window label, its members of every cohort)] of fleet_groups, labels in their order."""
    labels = dict.fromkeys(label for _, label in fleet_groups)
    return [(w, [m for (_, x), ms in fleet_groups.items() if x == w for m in ms]) for w in labels]


def slot_spans(as_of, hours):
    """The `*` entry's IP slots of a window of hours ending at as_of as [start, end) spans,
    oldest first: each UTC clock hour (24h: 24 slots) or UTC day (7d, 30d: one per day), the
    window's series points (aggregate.series_axis(), SERIES-HOURLY-DAILY-1). The last is the
    hour or day holding the window's last instant (partial: it ends at as_of), so every slot lies
    inside the window, and the whole window has its slots, before the data starts too: the axis
    of the API's successScoreCurve (#1301) (FLEET-POOL-SERIES-1, #1302)."""
    first, step = series_axis(as_of, hours)
    count = round(timedelta(hours=hours) / step)
    return [(first + n * step, min(first + (n + 1) * step, as_of)) for n in range(count)]


def ip_spans(as_of, hours):
    """[(slot start, the span its IPs are counted over)] of slot_spans(): in the 24h window the
    trailing 24 hours to the slot's end (a rolling pool, FLEET-POOL-SERIES-3: the newest is the
    window itself, the 24h unique IPs), in the 7d and 30d windows the UTC day itself."""
    if hours != 24:
        return [(t, (t, e)) for t, e in slot_spans(as_of, hours)]
    return [(t, (e - ROLLING, e)) for t, e in slot_spans(as_of, hours)]


def ip_slots(pairs, counts):
    """The IP slots of pairs (ip_spans()) with counts ({span: distinct IPs}): {"at": the slot's
    start, "unique_ips": its span's IPs, None where it has none (no data), never 0}."""
    return [{"at": t, "unique_ips": counts.get(span) or None} for t, span in pairs]


def slot_counts(spans, members):
    """{span: distinct IPs} of the members' observations in each of spans (sorted, none inside
    another: the ones holding a time are consecutive), each IP once in a span however many
    members saw it: the full calculation's IP sets (the incremental one has none,
    fleetsql.fleet_entries() counts them instead)."""
    starts, ends, seen = [s for s, _ in spans], [e for _, e in spans], defaultdict(set)
    for _, _, obs, _ in members:
        for o in obs:
            t = o["bucket_start"]
            for n in range(bisect_right(ends, t), bisect_right(starts, t)):
                seen[spans[n]].add(o["ip_id"])
    return {span: len(ips) for span, ips in seen.items()}
