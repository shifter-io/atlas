"""Counts across product groups: fleet entries and overlaps (contract K6, CALC-FLEET-1).

Every count is made in the database from memos, so nothing per IP comes back and an hour reads
only the observations that changed:

- Each 5-minute bucket of the longest window has a memo (calculated_bucket_ips): the digest of
  the runs with a bucket row there and the bucket's distinct IPs. A bucket whose runs changed (a
  new or a late run) is counted again and folded into calculated_ip_last_seen, each IP's newest
  bucket, with greatest(). A memo row exists only for a folded bucket, and a stored run's
  observations never change, so the table holds the newest sighting of every observation of the
  folded buckets.
- The `*` window totals (every group: every observation of the window) are the IPs whose newest
  sighting is in the window, every window's from one read of calculated_ip_last_seen
  (last_seen()). That holds when no folded bucket is at or after the cutoff, the
  hourly (latest) build; an older cutoff counts the window's distinct IPs directly and changes
  no memo.
- The `*` series: one point per UTC hour (24h) or day (7d/30d; aggregate.series_axis(), #1301),
  each point's distinct IPs its IP slot's (below): the same span, counted once. A point off the
  slots (an as-of time on the hour) is a span of its own, remembered like a slice.
- The `*` IP slots (fleet.slot_spans(): each UTC hour of 24h, each UTC day of 7d and 30d): a
  closed slot is remembered like a slice, so each is counted once, after it closes; the newest
  hour and day, which end at the cutoff, are the IPs last seen since their start, from the same
  read as the totals. An hourly build counts one hour, and the first after midnight one day.
- A cohort of one group has its group's pool count and no overlap: no query. A cohort of two or
  more is a sum over the IP state's vectors in the latest build (ipstate.py); an older cutoff
  counts it (COUNTS) only when a member's runs in the window changed: its counts are
  memoised in calculated_distinct under the digest of each member's runs clipped to the window.
  Its distinct values are counted in two levels of GROUP BY, without DISTINCT aggregates
  (CALC-FLEET-DISTINCT-1), and the cohorts in batches of a bounded estimate of observation rows.
- Old memo rows are collected: IPs and buckets older than KEEP, counts unused for 2 days.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from itertools import combinations

from psycopg.rows import dict_row, tuple_row

from . import fleet, fleet_verdicts, ipstate
from .aggregate import series_axis
from .parallel_reads import export, settings, snapshot_connection

KEEP = ipstate.KEEP
PARALLEL = 4
# Prefix of this module's calculated_distinct digests (hourly.py keys its own entries there).
MEMO = "fleet-v1-"
# Per (slot, member pair a <= b): the IPs both members saw, and of those the IPs some member of
# another provider saw too (for a = b: the member's own IPs, and those shared with a peer); and
# lead, of the (a, a) rows, the IPs whose lowest member is a (summed: the slot's distinct IPs).
# Distinct values are counted in two levels of GROUP BY, not by DISTINCT aggregates (each sorts
# its own input): (slot, IP, member) once, then each IP's members (provider: its member's
# provider's index; more than one provider when min <> max). The planner hashes the levels or
# feeds both from one sort, whichever costs less.
COUNTS = """WITH k AS (
 SELECT * FROM unnest(%(slots)s::int[],%(members)s::int[],%(providers)s::int[],
  %(keys)s::bigint[],%(since)s::timestamptz[]) AS k(slot,member,provider,run_key,since)
), seen AS (
 SELECT k.slot,o.ip_id,k.member,k.provider FROM k JOIN ip_observations o ON o.run_key=k.run_key
  AND o.bucket_start>=k.since AND o.bucket_start<%(as_of)s GROUP BY 1,2,3,4
), ip AS (
 SELECT slot,array_agg(member) AS members,min(member) AS lead,
  min(provider)<>max(provider) AS shared
 FROM seen GROUP BY slot,ip_id
)
SELECT slot,a,b,count(*) AS ips,count(*) FILTER (WHERE shared) AS shared,
 count(*) FILTER (WHERE a=lead AND b=lead) AS lead
FROM ip CROSS JOIN LATERAL unnest(members) a CROSS JOIN LATERAL unnest(members) b WHERE a<=b
GROUP BY 1,2,3"""
# The runs of each 5-minute bucket in [start, end), as a digest of their keys.
RUN_SETS = """SELECT bucket_start,md5(string_agg(run_key::text,',' ORDER BY run_key)) AS runs
FROM run_buckets WHERE bucket_start>=%s AND bucket_start<%s GROUP BY 1"""
MEMO_ROWS = """SELECT bucket_start,runs_digest,unique_ips FROM calculated_bucket_ips
WHERE bucket_start>=%s AND bucket_start<%s"""
NEWEST = "SELECT max(bucket_start) AS newest FROM calculated_bucket_ips"
BUCKET_IPS = """SELECT b.t AS bucket_start,count(DISTINCT o.ip_id) AS ips
FROM unnest(%s::timestamptz[]) b(t) JOIN ip_observations o ON o.bucket_start=b.t GROUP BY 1"""
FOLD = """INSERT INTO calculated_ip_last_seen(ip_id,last_seen)
SELECT o.ip_id,max(o.bucket_start) FROM unnest(%s::timestamptz[]) b(t)
JOIN ip_observations o ON o.bucket_start=b.t GROUP BY 1
ON CONFLICT(ip_id) DO UPDATE SET last_seen=greatest(calculated_ip_last_seen.last_seen,
 excluded.last_seen)"""
STORE = """INSERT INTO calculated_bucket_ips(bucket_start,runs_digest,unique_ips)
SELECT * FROM unnest(%s::timestamptz[],%s::text[],%s::bigint[])
ON CONFLICT(bucket_start) DO UPDATE SET runs_digest=excluded.runs_digest,
 unique_ips=excluded.unique_ips"""
# The IPs last seen in each [cuts[n - 1], cuts[n]) (n = len(cuts) for the last), cuts ascending:
# one read of the last_seen index from the earliest cut.
LAST_SEEN = """SELECT width_bucket(last_seen,%(cuts)s::timestamptz[]) AS n,count(*) AS ips
FROM calculated_ip_last_seen WHERE last_seen>=%(first)s GROUP BY 1"""
SPAN = """SELECT count(DISTINCT ip_id) FROM ip_observations
WHERE bucket_start>=%s AND bucket_start<%s"""
# Each run's first and last bucket in its set's window: what a cohort's counts depend on.
# rows: the run's successful requests there, each at most one observation row (its estimate).
RANGES = """SELECT k.slot,k.run_key,min(b.bucket_start) AS first,max(b.bucket_start) AS last,
 sum(b.successes)::bigint AS rows
FROM unnest(%s::int[],%s::bigint[],%s::timestamptz[]) AS k(slot,run_key,since)
JOIN run_buckets b ON b.run_key=k.run_key AND b.bucket_start>=k.since AND b.bucket_start<%s
GROUP BY 1,2"""
REMEMBERED = "SELECT digest,counts FROM calculated_distinct WHERE digest=ANY(%s)"
USED = "UPDATE calculated_distinct SET last_used=now() WHERE digest=ANY(%s)"
REMEMBER = """INSERT INTO calculated_distinct(digest,counts) SELECT d,c::jsonb
FROM unnest(%s::text[],%s::text[]) AS k(d,c)
ON CONFLICT(digest) DO UPDATE SET counts=excluded.counts,last_used=now()"""
COLLECT = (
    "DELETE FROM calculated_ip_last_seen WHERE last_seen<%(old)s",
    "DELETE FROM calculated_bucket_ips WHERE bucket_start<%(old)s",
    "DELETE FROM calculated_distinct WHERE starts_with(digest,%(prefix)s) "
    "AND last_used<now()-interval '2 days'",
)


def fleet_entries(conn, as_of, cohort_members, windows, ruled=None, scope_of=None, close=None):
    """The fleet and overlap output of a calculation, with every IP count across groups counted.

    cohort_members maps each (cohort, window label) to (its fleet entry, its members), each
    member (product group, run keys); windows maps each label to {"hours", "fleet": the entry of
    every group together, cohort "*"}. The entries are fleet.fleet_entry() and fleet_totals()
    of the request buckets, without IPs. Returns {"fleet": the entries in that order with their
    unique_ips (and the "*" series' IPs and ip_slots), "overlap": each pair of a cohort's
    products of different providers that both saw IPs, "shares": {(group id, label): overlap
    shares}}, as the full calculation's pass over IP sets (calculator.build()) gives them.
    ruled: with the methodology's country_verdicts, the counted runs' rows
    (fleet_verdicts.Counted): the `*` entries (and the IP state) count only their observations.
    With scope_of ({run_key: scope}, ipstate.scopes()) the latest build counts its cohorts from
    the IP state and returns its vectors ({hours: ipstate.Vectors}, else None) as "vectors";
    with close, it first moves the state to each UTC midnight it passes (ipstate.midnights())
    and calls close(midnight, its vectors there).
    """
    out = {"fleet": [], "overlap": [], "shares": {}, "vectors": None}
    latest, runs, _ = _latest(conn, as_of, windows, ruled) if windows else (False, {}, {})
    if latest and scope_of is not None:
        hours = [w["hours"] for w in windows.values()]
        for day in ipstate.midnights(conn, as_of) if close else ():
            before = {t: d for t, d in runs.items() if t < day}
            closed = {"day": day.date().isoformat()}  # its timing line's field
            close(day, ipstate.advance(conn, day, hours, before, scope_of, ruled, closed))
        out["vectors"] = ipstate.advance(conn, as_of, hours, runs, scope_of, ruled)
    found = _cohorts(conn, as_of, cohort_members, out["vectors"], scope_of)
    for ((cohort, label), (entry, members)), counts in zip(
        cohort_members.items(), found, strict=True
    ):
        out["fleet"].append(entry | {"unique_ips": counts.get((None, None), (0, 0))[0]})
        _overlaps(out, cohort, label, members, counts)
    if not windows:
        return out
    since = {label: _since(as_of, label, windows) for label in windows}
    slots = {label: fleet.slot_spans(as_of, w["hours"]) for label, w in windows.items()}
    # Each series point's span: a UTC hour (24h) or day (series_axis(), #1301), its IP slot.
    width = {label: series_axis(as_of, w["hours"])[1] for label, w in windows.items()}
    point_spans = {
        label: {t: (t, min(t + width[label], as_of)) for t in _missing(w["fleet"]["series"])}
        for label, w in windows.items()
    }
    slotted = {span for found in slots.values() for span in found}
    spans = sorted({span for p in point_spans.values() for span in p.values()} - slotted)
    totals, counted = _window_counts(conn, since, spans, slots, (as_of, latest, runs, ruled))
    for label, w in windows.items():
        points = w["fleet"]["series"]
        series = fill(points, {t: counted.get(span, 0) for t, span in point_spans[label].items()})
        found = {"unique_ips": totals[label], "series": series}
        found["ip_slots"] = fleet.ip_slots(slots[label], counted)
        out["fleet"].append(w["fleet"] | found)
    return out


def _window_counts(conn, since, spans, slots, held):
    """({label: the window's distinct IPs}, {span: its distinct IPs}) of the `*` windows that
    start at since ({label: start}), for spans (the series points off the slots) and slots ({label: its
    slot_spans()}); held: (as_of, latest, the run sets read, ruled), as fleet_entries() has them.
    The latest build counts from the memos and collects them (the module docstring)."""
    as_of, latest, runs, ruled = held
    slotted = sorted({s for found in slots.values() for s in found})
    if not latest:
        ends = [(s, as_of) for s in since.values()]
        counted = count_spans(conn, spans + ends + slotted, ruled)
        return {label: counted[s, as_of] for label, s in since.items()}, counted
    # The newest hour and day end at the cutoff: their IPs are the last seen since they start.
    newest = [s for s in slotted if s[1] == as_of]
    seen = last_seen(conn, since | {s: s[0] for s in newest})
    closed = [s for s in slotted if s[1] != as_of]
    counted = remembered_spans(conn, spans + closed, runs, ruled) | {s: seen[s] for s in newest}
    collect(conn, as_of)
    return {label: seen[label] for label in since}, counted


def _latest(conn, as_of, windows, ruled):
    """(latest: no folded bucket at or after the cutoff, the run sets of the buckets read, their
    IPs): the longest window's in the latest build, else the 24h one's (if any)."""
    newest = conn.cursor(row_factory=dict_row).execute(NEWEST).fetchone()["newest"]
    latest = newest is None or newest < as_of
    fleet_verdicts.ruled(conn, ruled, latest, as_of)
    if not (latest or any(w["hours"] == 24 for w in windows.values())):
        return latest, {}, {}
    start = as_of - timedelta(hours=max(w["hours"] for w in windows.values()) if latest else 24)
    runs = run_sets(conn, start, as_of) if ruled is None else ruled.digests(start, as_of)
    return latest, runs, bucket_ips(conn, start, as_of, runs, fold=latest, ruled=ruled)


def last_seen(conn, since):
    """{label: the IPs last seen at or after its cutoff} of since ({label: cutoff}), from one
    read of calculated_ip_last_seen: LAST_SEEN's IPs between cuts, summed from the latest."""
    cuts = sorted(set(since.values()))
    rows = conn.cursor(row_factory=dict_row).execute(LAST_SEEN, {"cuts": cuts, "first": cuts[0]})
    between = {r["n"]: r["ips"] for r in rows}
    at, ips = {}, 0
    for n in range(len(cuts), 0, -1):
        ips += between.get(n, 0)
        at[cuts[n - 1]] = ips
    return {label: at[s] for label, s in since.items()}


def _since(as_of, label, windows):
    return as_of - timedelta(hours=windows[label]["hours"])


def _missing(points):
    """The series points whose IPs are counted here: those with requests (fill())."""
    return [p["at"] for p in points if p["unique_ips"] == 0]


def run_sets(conn, start, end):
    """{bucket: the digest of its runs} of every 5-minute bucket in [start, end) with runs."""
    return dict(conn.cursor(row_factory=tuple_row).execute(RUN_SETS, (start, end)))


def bucket_ips(conn, start, end, runs, fold, ruled=None):
    """Distinct IPs of every observation per 5-minute bucket of runs (run_sets() of [start, end)):
    {bucket: IPs}, from the memo of each bucket whose runs did not change. fold: count the others
    into the memo and fold them into calculated_ip_last_seen (only the latest build). ruled
    (fleet_verdicts.Counted): runs are its digests and only the counted runs' observations count."""
    cursor = conn.cursor(row_factory=dict_row)
    memo = {r["bucket_start"]: r for r in cursor.execute(MEMO_ROWS, (start, end))}
    kept = {t: m["unique_ips"] for t, m in memo.items() if runs.get(t) == m["runs_digest"]}
    changed = sorted(t for t in runs if t not in kept)
    if not changed:
        return kept
    counted = dict.fromkeys(changed, 0)
    if ruled is not None:
        pairs = ruled.pairs(changed)
        found = cursor.execute(fleet_verdicts.BUCKET_IPS, pairs)
    else:
        found = cursor.execute(BUCKET_IPS, (changed,))
    counted |= {r["bucket_start"]: r["ips"] for r in found}
    if fold:
        cursor.execute(*((fleet_verdicts.FOLD, pairs) if ruled is not None else (FOLD, [changed])))
        cursor.execute(STORE, (changed, [runs[t] for t in changed], [counted[t] for t in changed]))
    return kept | counted


def count_spans(conn, spans, ruled=None):
    """count(DISTINCT ip_id) of every observation in each [start, end) of spans: {span: IPs}
    (ruled, fleet_verdicts.Counted: of the counted runs' only). At most PARALLEL queries run at
    once, each on its own connection importing the calculation's snapshot (parallel_reads.py:
    so they read exactly what the calculation reads)."""
    spans = sorted(set(spans))
    if not spans:
        return {}

    def read(other, span):
        if ruled is None:
            return other.execute(SPAN, span).fetchone()[0]
        return other.execute(fleet_verdicts.SPAN, (ruled.keys(*span), *span)).fetchone()[0]

    found = on_snapshots(conn, spans, read)
    return dict(zip(spans, found, strict=True))


def remembered_spans(conn, spans, runs, ruled=None):
    """count_spans() of spans, each remembered under its bounds and the run sets of its buckets
    (runs: run_sets() over every span, the counted runs' with ruled), which fix the observations
    it holds."""
    held, digests = sorted((t.timestamp(), d) for t, d in runs.items()), {}
    times = [h[0] for h in held]
    for since, until in sorted(set(spans)):
        inside = held[bisect_left(times, since.timestamp()) : bisect_left(times, until.timestamp())]
        key = json.dumps(["span", since.timestamp(), until.timestamp(), inside])
        digests[since, until] = MEMO + hashlib.sha256(key.encode()).hexdigest()
    stored = _recall(conn, digests.values())
    counted = count_spans(conn, [s for s in digests if digests[s] not in stored], ruled)
    _remember(conn, {digests[s]: ips for s, ips in counted.items()})
    return {s: counted[s] if s in counted else stored[digests[s]] for s in digests}


def on_snapshots(conn, items, read):
    """[read(connection, item) for item in items], at most PARALLEL at once, each connection
    importing the calculation's snapshot (parallel_reads.py: so they read exactly what the
    calculation reads)."""
    exported = export(conn)
    shares = [range(len(items))[i::PARALLEL] for i in range(min(PARALLEL, len(items)))]

    def run(share):
        with snapshot_connection(conn, exported) as other:
            return [(n, read(other, items[n])) for n in share]

    with ThreadPoolExecutor(len(shares)) as pool:
        found = dict(pair for done in pool.map(run, shares) for pair in done)
    return [found[n] for n in range(len(items))]


def collect(conn, as_of):
    """Remove the memo rows no window needs: IPs last seen and buckets older than KEEP before
    as_of, and this module's cohort counts unused for 2 days."""
    for statement in COLLECT:
        conn.execute(statement, {"old": as_of - KEEP, "prefix": MEMO})


def _cohorts(conn, as_of, cohort_members, vectors=None, scope_of=None):
    """Per cohort window, {(a, b): (IPs, shared)} as _counts() gives it: one member's from its
    window's pool count, two or more from vectors (ipstate.pair_counts()), else from the memo of
    their runs, counted (and remembered) when a member's runs changed."""
    found, sets = [], {}
    for (_, label), (_, members) in cohort_members.items():
        hours = members[0][0]["windows"][label]["period"]["hours"]
        if len(members) == 1:
            ips = members[0][0]["windows"][label]["pool"]["unique_ips"]
            found.append({(0, 0): (ips, 0), (None, None): (ips, 0)} if ips else {})
        elif vectors is not None:
            found.append(ipstate.pair_counts(vectors[hours], members, scope_of))
        else:
            sets[len(found)] = (as_of - timedelta(hours=hours), members)
            found.append(None)
    if not sets:
        return found
    digests, estimated = _digests(conn, as_of, sets)
    stored = _recall(conn, digests.values())
    missed = [i for i in sets if digests[i] not in stored]
    counted = _counts(conn, as_of, [sets[i] for i in missed], [estimated[i] for i in missed])
    pairs = zip(missed, counted, strict=True)
    fresh = {digests[i]: [[a, b, *n] for (a, b), n in c.items()] for i, c in pairs}
    stored |= _remember(conn, fresh)
    for i, digest in digests.items():
        found[i] = {(a, b): (ips, shared) for a, b, ips, shared in stored[digest]}
    return found


def _recall(conn, digests):
    """{digest: counts} of the digests remembered in calculated_distinct, marked used."""
    cursor = conn.cursor(row_factory=dict_row)
    stored = {r["digest"]: r["counts"] for r in cursor.execute(REMEMBERED, (list(digests),))}
    cursor.execute(USED, (list(stored),))
    return stored


def _remember(conn, fresh):
    """Remember counts ({digest: a JSON value}); returns them."""
    conn.execute(REMEMBER, (list(fresh), [json.dumps(v) for v in fresh.values()]))
    return fresh


def _digests(conn, as_of, sets):
    """Each set's memo key: its members' providers and runs, each run with its first and last
    bucket in the window (which fix the run's observations in it); and each set's estimated
    observation rows (RANGES)."""
    runs = [
        (i, run_key, since, m)
        for i, (since, members) in sets.items()
        for m, (_, keys) in enumerate(members)
        for run_key in sorted(set(keys))
    ]
    owner = {(i, run_key): m for i, run_key, _, m in runs}
    params = [[run[n] for run in runs] for n in range(3)]
    ranges = {i: [[] for _ in members] for i, (_, members) in sets.items()}
    rows = dict.fromkeys(sets, 0)
    if params[0]:
        for r in conn.cursor(row_factory=dict_row).execute(RANGES, (*params, as_of)):
            rows[r["slot"]] += r["rows"]
            ranges[r["slot"]][owner[r["slot"], r["run_key"]]].append(
                [r["run_key"], *(r[k].astimezone(UTC).isoformat() for k in ("first", "last"))]
            )
    digests = {}
    for i, (_, members) in sets.items():
        runs = zip(members, ranges[i], strict=True)
        key = [[group["provider"], sorted(found)] for (group, _), found in runs]
        digests[i] = MEMO + hashlib.sha256(json.dumps(key).encode()).hexdigest()
    return digests, rows


def _counts(conn, as_of, sets, rows):
    """sets: [(since, [(group, run keys) per member])] -> per set, {(a, b): (IPs, shared)} with
    (None, None) the set's distinct IPs. rows: each set's estimated observation rows; the sets
    are counted in batches of at most calculate.heavy_rows of them (a larger set alone), on
    snapshot connections (on_snapshots()) when there are several, so each statement's
    aggregates stay near work_mem."""
    if not sets:
        return []
    batches, size, limit = [], 0, settings(conn)["heavy_rows"]
    for n, estimated in enumerate(rows):
        if not batches or size + estimated > limit:
            batches.append([])
            size = 0
        batches[-1].append(sets[n])
        size += estimated
    if len(batches) == 1:
        return _count_sets(conn, as_of, sets)
    found = on_snapshots(conn, batches, lambda other, batch: _count_sets(other, as_of, batch))
    return [counts for batch in found for counts in batch]


def _count_sets(conn, as_of, sets):
    """_counts() of sets in one COUNTS statement."""
    providers = {g["provider"] for _, members in sets for g, _ in members}
    providers = {p: n for n, p in enumerate(sorted(providers))}
    rows = [
        (slot, member, providers[group["provider"]], run_key, since)
        for slot, (since, members) in enumerate(sets)
        for member, (group, keys) in enumerate(members)
        for run_key in sorted(set(keys))
    ]
    found, distinct = [{} for _ in sets], [0] * len(sets)
    if not rows:
        return found
    names = ("slots", "members", "providers", "keys", "since")
    params = {name: [row[n] for row in rows] for n, name in enumerate(names)}
    for row in conn.cursor(row_factory=dict_row).execute(COUNTS, params | {"as_of": as_of}):
        found[row["slot"]][row["a"], row["b"]] = (row["ips"], row["shared"])
        distinct[row["slot"]] += row["lead"]
    for counts, ips in zip(found, distinct, strict=True):
        if ips:
            counts[None, None] = (ips, 0)
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
