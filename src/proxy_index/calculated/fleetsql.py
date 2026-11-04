"""Counts across product groups: fleet entries and overlaps (contract K6; CALC-FLEET-1 replaces
the body). The full-mode body counts each exit IP once in the database; nothing per IP comes back.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from itertools import combinations

from psycopg.rows import dict_row

from . import hourly

# Per (slot, member pair a <= b): the IPs both members saw, and of those the IPs some member of
# another provider saw too (for a = b: the member's own IPs, and those shared with a peer).
# Per slot, with a and b NULL: every IP once.
COUNTS = """WITH k AS (
 SELECT * FROM unnest(%(slots)s::int[],%(members)s::int[],%(providers)s::text[],
  %(keys)s::bigint[],%(since)s::timestamptz[]) AS k(slot,member,provider,run_key,since)
), ip AS (
 SELECT k.slot,o.ip_id,array_agg(DISTINCT k.member) AS members,
  count(DISTINCT k.provider) AS providers
 FROM k JOIN ip_observations o ON o.run_key=k.run_key AND o.bucket_start>=k.since
  AND o.bucket_start<%(as_of)s GROUP BY 1,2
)
SELECT slot,a,b,count(*) AS ips,count(*) FILTER (WHERE providers>1) AS shared
FROM ip CROSS JOIN LATERAL unnest(members) a CROSS JOIN LATERAL unnest(members) b WHERE a<=b
GROUP BY 1,2,3
UNION ALL SELECT slot,NULL,NULL,count(*),0 FROM ip GROUP BY 1"""


def fleet_entries(conn, as_of, cohort_members, windows):
    """The fleet and overlap output of a calculation, with every IP count across groups counted.

    cohort_members maps each (cohort, window label) to (its fleet entry, its members), each
    member (product group, run keys); windows maps each label to {"hours", "fleet": the entry of
    every group together, cohort "*"}. The entries are fleet.fleet_entry() and fleet_totals()
    of the request buckets, without IPs. Returns {"fleet": the entries in that order with their
    unique_ips (and the "*" series' IPs), "overlap": each pair of a cohort's products of different
    providers that both saw IPs, "shares": {(group id, label): overlap shares}}, as the full
    calculation's pass over IP sets (calculator.build()) gives them.
    """
    labels, everyone = list(windows), defaultdict(list)  # label -> run keys of every group
    for (_, label), (_, members) in cohort_members.items():
        everyone[label] += [k for _, keys in members for k in keys]
    sets = [(_since(as_of, w, windows), ms) for (_, w), (_, ms) in cohort_members.items()]
    sets += [(_since(as_of, w, windows), [({"provider": ""}, everyone[w])]) for w in labels]
    counts = _counts(conn, as_of, sets)
    out = {"fleet": [], "overlap": [], "shares": {}}
    cohorts = zip(cohort_members.items(), counts[: len(cohort_members)], strict=True)
    for ((cohort, label), (entry, members)), found in cohorts:
        out["fleet"].append(entry | {"unique_ips": found.get((None, None), (0, 0))[0]})
        _overlaps(out, cohort, label, members, found)
    for label, found in zip(labels, counts[len(cohort_members) :], strict=True):
        hours, entry = windows[label]["hours"], windows[label]["fleet"]
        step = timedelta(minutes=5) if hours == 24 else timedelta(days=1)
        span = (_since(as_of, label, windows), as_of, step)
        ips = hourly.bucket_ips(conn, {label: everyone[label]}, {label: span})[label]
        series = fill(entry["series"], ips)
        out["fleet"].append(
            entry | {"unique_ips": found.get((None, None), (0, 0))[0], "series": series}
        )
    return out


def _since(as_of, label, windows):
    return as_of - timedelta(hours=windows[label]["hours"])


def _counts(conn, as_of, sets):
    """sets: [(since, [(group, run keys) per member])] -> per set, {(a, b): (IPs, shared)} with
    (None, None) the set's distinct IPs."""
    params = {k: [] for k in ("slots", "members", "providers", "keys", "since")}
    for slot, (since, members) in enumerate(sets):
        for member, (group, keys) in enumerate(members):
            for run_key in sorted(set(keys)):
                values = (slot, member, group["provider"], run_key, since)
                for name, value in zip(params, values, strict=True):
                    params[name].append(value)
    found = [{} for _ in sets]
    if params["slots"]:
        rows = conn.cursor(row_factory=dict_row).execute(COUNTS, params | {"as_of": as_of})
        for row in rows:
            found[row["slot"]][row["a"], row["b"]] = (row["ips"], row["shared"])
    return found


def _overlaps(out, cohort, label, members, found):
    """The cohort's pairwise overlaps and each member's shares, as the full pass computes them
    from IP sets."""
    own = [found.get((i, i), (0, 0)) for i in range(len(members))]
    for i, (p, _) in enumerate(members):
        ips, shared = own[i]
        peers = sum(
            1 for j, (q, _) in enumerate(members) if q["provider"] != p["provider"] and own[j][0]
        )
        if ips:
            out["shares"][p["id"], label] = {
                "exclusive_share": 1 - shared / ips,
                "shared_share": shared / ips,
                "compared_products": peers,
            }
    for (i, (a, _)), (j, (b, _)) in combinations(enumerate(members), 2):
        if a["provider"] != b["provider"] and own[i][0] and own[j][0]:
            out["overlap"].append(
                {
                    "cohort": cohort,
                    "window": label,
                    "product_a": a["product_id"],
                    "product_b": b["product_id"],
                    "shared_ips": found.get((i, j), (0, 0))[0],
                    "ips_a": own[i][0],
                    "ips_b": own[j][0],
                }
            )


def fill(points, ips):
    """series() points computed without observations (unique_ips 0 where the point has
    requests), with each such point's distinct IPs from ips."""
    return [p | {"unique_ips": ips.get(p["at"], 0)} if p["unique_ips"] == 0 else p for p in points]
