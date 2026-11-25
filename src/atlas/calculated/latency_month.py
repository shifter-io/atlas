"""LATENCY-DELTA-1: the 30d latency change over the latency basket (latency_basket.py), as the 24h
and 7d changes (latency_basket.deltas()): each 30-day period's basket p50, its own basket
countries' p50 and the slowest network's of the same kind and period in the others.

The calculator reads 30 days of raw rows, so a 30-day period's country latency merges the per-day
store's days (latency_store.py): each day record of a basket network holds its basket countries'
merged weights (by_country(), calculated_latency_country_days), which add up to the period's as
the day's weights do. The stored days' are merged in the database (MERGED), the raw rows' days in
Python. A day stored without them (before calculated v24, or with another basket) leaves its
period without a value, so the window keeps its 7-day or day change until 30 days of them are
stored. As with the pooled change, there is no common-country comparison of the stored days.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from psycopg.rows import tuple_row

from .latency import percentiles, weighed
from .latency_basket import P50, basket_latency, penalties
from .latency_days import DAY, compare, day_pair
from .underload import light_votes, time_slots

DAYS = 30
# Each network's basket countries' merged weights over its stored days of the two periods (half
# 0 the earlier, 1 the last) before the raw rows' first day, of the days with the whole basket.
MERGED = """WITH days AS (
 SELECT c.provider,c.kind,c.country,(c.day>=%(last)s)::int AS half,c.slots,c.ms,c.weights
 FROM calculated_latency_country_days c JOIN calculated_latency_days d
 USING(provider,kind,target,day) WHERE c.target=%(target)s AND d.minimum=%(minimum)s
 AND d.basket@>%(basket)s::text[] AND c.day>=%(first)s AND c.day<%(raw)s),
counts AS (SELECT provider,kind,country,half,sum(slots)::int AS slots FROM days GROUP BY 1,2,3,4),
bins AS (SELECT provider,kind,country,half,u.ms,sum(u.weight) AS weight
 FROM days,unnest(days.ms,days.weights) u(ms,weight) GROUP BY 1,2,3,4,5)
SELECT provider,kind,country,half,counts.slots,array_agg(bins.ms ORDER BY bins.ms),
 array_agg(bins.weight ORDER BY bins.ms)
FROM bins JOIN counts USING(provider,kind,country,half) GROUP BY 1,2,3,4,5"""
# The stored days written with the whole basket.
COVERED = (
    "SELECT provider,kind,day FROM calculated_latency_days WHERE target=%(target)s "
    "AND minimum=%(minimum)s AND basket@>%(basket)s::text[] AND day>=%(first)s AND day<%(raw)s"
)


def by_country(rows, minimum, basket):
    """{country: (slots, weights)} of each basket country with a slot of `minimum` light-load
    successes in rows (one day's): latency_basket.countries()'s votes, merged (weighed())."""
    own = defaultdict(list)
    for r in rows:
        if r["country"] in basket:
            own[r["country"]].append(r)
    found = {}
    for country, mine in sorted(own.items()):
        weights, used = weighed(h for *_, h in light_votes(time_slots(mine), minimum))
        if used:
            found[country] = (used, dict(weights))
    return found


def arrays(found):
    """by_country()'s countries as [slots, ms ascending, their weights] (stored, hashed)."""
    return {
        c: [slots, sorted(weights), [weights[m] for m in sorted(weights)]]
        for c, (slots, weights) in sorted(found.items())
    }


def months(conn, found, older, as_of):
    """{(provider, kind): its 30d window's 30-day latency change}: found and older are
    latency_store.complete()'s ({key: (30d window, its "_latency_days")}, {key: stored days});
    the penalties are the measured networks' (a held window's: and its own)."""
    first = next(iter(found.values()))[1]
    basket, aggregate = first["settings"]["basket"], first["settings"]["aggregate"]
    pair = day_pair(as_of, DAYS)
    params = {"target": first["target"], "minimum": first["minimum"], "raw": first["raw"]}
    params |= {"basket": basket, "first": pair[0], "last": pair[1]}
    cur = conn.cursor(row_factory=tuple_row)
    merged = defaultdict(dict)
    for provider, kind, country, half, slots, ms, weights in cur.execute(MERGED, params):
        merged[provider, kind, pair[half]][country] = (slots, dict(zip(ms, weights, strict=True)))
    covered = set(cur.execute(COVERED, params).fetchall())
    periods, since = {}, {}
    for key, (_, own) in found.items():
        days = [r | {"covered": (*key, r["day"]) in covered} for r in older[key]] + own["days"]
        since[key] = min((r["day"] for r in days), default=None)
        periods[key] = {s: _period(days, merged[(*key, s)], s, first["minimum"]) for s in pair}
    live = [periods[k] for k, (w, _) in found.items() if "_held" not in w]
    shared = penalties(live, basket) if live else None
    result = {}
    for key, (w, _) in found.items():
        penalty = shared if "_held" not in w else penalties([*live, periods[key]], basket)
        compared = [_compared(periods[key][s], penalty[s], aggregate, s) for s in pair]
        result[key] = compare(compared, since[key])
    return result


def _period(records, merged, start, minimum):
    """The network's 30-day period from start: its tested countries and, when each of its stored
    days has its basket countries' weights ("complete"), their p50 ("own"). records are its days
    (stored ones with "covered", the raw rows' with "by_country"), merged its stored days' weights
    in the period (MERGED)."""
    successes, slots, weights = Counter(), Counter(), defaultdict(Counter)
    parts = list(merged.items())
    complete = True
    for r in records:
        if start <= r["day"] < start + DAYS * DAY:
            successes.update(r["countries"])
            parts += r.get("by_country", {}).items()
            complete = complete and ("by_country" in r or r["covered"])
    for country, (n, w) in parts:
        slots[country] += n
        weights[country].update(w)
    mine = {c: {"p50": percentiles(weights[c], n)["p50"]} for c, n in slots.items()}
    return {
        "countries": sorted(c for c, n in successes.items() if n >= minimum),
        "own": mine if complete else {},
        "complete": complete,
    }


def _compared(period, penalty, aggregate, start):
    """compare()'s period: the basket p50 of a complete period (else None, no value)."""
    p50 = basket_latency(period["own"], penalty, aggregate, P50)[0]["p50"]
    return {
        "start": start,
        "end": start + DAYS * DAY,
        "p50": p50 if period["complete"] else None,
        "countries": period["countries"],
    }
