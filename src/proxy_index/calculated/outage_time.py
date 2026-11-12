"""Time-based uptime from outages ([uptime] basis in the methodology file; aggregate.py
re-exports it).

"window" divides the downtime by the window length times the countries tested in the window;
"measured_time" by each tested country's measured span: from the start of its first tested run
to the end of its last one, clipped to the window, with its downtime clipped to that span.
"""

from __future__ import annotations

BASES = ("window", "measured_time")


def downtime(outages, countries, since, end):
    """Seconds of [since, end) a product was down, summed over `countries`.

    Per country, the union of its outage intervals clipped to the window (an open outage, or
    one that ended after `end`, runs to `end`), so overlapping outages are counted once.
    """
    total = 0.0
    for country in countries:
        spans = sorted(
            (max(o["started_at"], since), min(o["ended_at"] or end, end))
            for o in outages
            if o["country"] == country
        )
        start = stop = None
        for a, b in spans:
            if b <= a:
                continue
            if stop is None or a > stop:
                if stop is not None:
                    total += (stop - start).total_seconds()
                start, stop = a, b
            else:
                stop = max(stop, b)
        if stop is not None:
            total += (stop - start).total_seconds()
    return total


def measured_spans(runs, since, end):
    """Per country, its measured span in [since, end): from the start of its first tested run to
    the end ("last") of its latest, clipped to the window. runs are tested_spans() rows (country,
    started_at, last); a run counts when it overlaps the window, as for the countries tested."""
    first, last = {}, {}
    for r in runs:
        if r["last"] < since or r["started_at"] >= end:
            continue
        country = r["country"]
        first[country] = min(first.get(country, r["started_at"]), r["started_at"])
        last[country] = max(last.get(country, r["last"]), r["last"])
    return {c: (max(first[c], since), min(last[c], end)) for c in first}


def outage_uptime(outages, tested, since, end, runs=None):
    """Time-based uptime: 1 - downtime / available time, over the countries tested in the window.

    tested maps each country the product had a started batch run in to the end of its latest
    one (or `end` while running); a country counts when that is within the window. Without
    runs (the "window" basis) the available time is the window length per country. With runs
    (tested_spans() rows, the "measured_time" basis) it is the sum of the countries' measured
    spans (measured_spans()), each country's downtime clipped to its span, and no measured time
    gives None. downtime_s stays the window's whole downtime either way.
    """
    countries = sorted(c for c, last in tested.items() if last >= since)
    down = downtime(outages, countries, since, end)
    if runs is None:
        available, lost = (end - since).total_seconds() * len(countries), down
    else:
        spans = measured_spans(runs, since, end).items()
        available = sum((b - a).total_seconds() for _, (a, b) in spans)
        lost = sum(downtime(outages, [c], a, b) for c, (a, b) in spans)
    return {
        "value": min(1.0, max(0.0, 1 - lost / available)) if available > 0 else None,
        "downtime_s": round(down, 3),
        "countries": len(countries),
        "basis": "outage time",
    }
