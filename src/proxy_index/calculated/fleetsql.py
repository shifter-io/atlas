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
  sighting is in the window. That holds when no folded bucket is at or after the cutoff, the
  hourly (latest) build; an older cutoff counts the window's distinct IPs directly and changes
  no memo.
- The `*` series: the 24h points are buckets (their memo); the 7d/30d points are daily slices
  anchored at the window's start, one count(DISTINCT ip_id) per slice on ip_observations'
  (bucket_start, ip_id) index, at most PARALLEL at a time in the calculation's snapshot.
- A cohort of one group has its group's pool count and no overlap: no query. A cohort of two or
  more is counted (COUNTS) only when a member's runs in the window changed: its counts are
  memoised in calculated_distinct under the digest of each member's runs clipped to the window.
- Old memo rows are collected: IPs and buckets older than KEEP, cohort counts unused for 2 days.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from itertools import combinations

import psycopg
from psycopg import sql
from psycopg.rows import dict_row, tuple_row

DAY = timedelta(days=1)
KEEP = timedelta(days=31)
PARALLEL = 4
# Prefix of this module's calculated_distinct digests (hourly.py keys its own entries there).
MEMO = "fleet-v1-"
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
LAST_SEEN = """SELECT s AS since,(SELECT count(*) FROM calculated_ip_last_seen
WHERE last_seen>=s) AS ips FROM unnest(%s::timestamptz[]) s"""
SPAN = """SELECT count(DISTINCT ip_id) FROM ip_observations
WHERE bucket_start>=%s AND bucket_start<%s"""
# Each run's first and last bucket in its set's window: what a cohort's counts depend on.
RANGES = """SELECT k.slot,k.run_key,min(b.bucket_start) AS first,max(b.bucket_start) AS last
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
    out = {"fleet": [], "overlap": [], "shares": {}}
    found = _cohorts(conn, as_of, cohort_members)
    for ((cohort, label), (entry, members)), counts in zip(
        cohort_members.items(), found, strict=True
    ):
        out["fleet"].append(entry | {"unique_ips": counts.get((None, None), (0, 0))[0]})
        _overlaps(out, cohort, label, members, counts)
    if not windows:
        return out
    newest = conn.cursor(row_factory=dict_row).execute(NEWEST).fetchone()["newest"]
    latest = newest is None or newest < as_of  # no folded bucket at or after the cutoff
    buckets = {}
    if latest or any(w["hours"] == 24 for w in windows.values()):
        longest = max(w["hours"] for w in windows.values()) if latest else 24
        buckets = bucket_ips(conn, as_of - timedelta(hours=longest), as_of, fold=latest)
    since = {label: _since(as_of, label, windows) for label in windows}
    spans = [
        (t, min(t + DAY, as_of))
        for w in windows.values()
        if w["hours"] != 24
        for t in _missing(w["fleet"]["series"])
    ]
    if latest:
        rows = conn.cursor(row_factory=dict_row).execute(LAST_SEEN, (list(since.values()),))
        totals = {r["since"]: r["ips"] for r in rows}
        totals = {label: totals[s] for label, s in since.items()}
        counted = count_spans(conn, spans)
        collect(conn, as_of)
    else:
        counted = count_spans(conn, spans + [(s, as_of) for s in since.values()])
        totals = {label: counted[s, as_of] for label, s in since.items()}
    for label, w in windows.items():
        points = w["fleet"]["series"]
        ips = buckets if w["hours"] == 24 else {t: counted[t, e] for t, e in spans}
        series = fill(points, {t: ips.get(t, 0) for t in _missing(points)})
        out["fleet"].append(w["fleet"] | {"unique_ips": totals[label], "series": series})
    return out


def _since(as_of, label, windows):
    return as_of - timedelta(hours=windows[label]["hours"])


def _missing(points):
    """The series points whose IPs are counted here: those with requests (fill())."""
    return [p["at"] for p in points if p["unique_ips"] == 0]


def bucket_ips(conn, start, end, fold):
    """Distinct IPs of every observation per 5-minute bucket in [start, end) that has runs:
    {bucket: IPs}, from the memo of each bucket whose runs did not change. fold: count the
    others into the memo and fold them into calculated_ip_last_seen (only the latest build)."""
    cursor = conn.cursor(row_factory=dict_row)
    runs = {r["bucket_start"]: r["runs"] for r in cursor.execute(RUN_SETS, (start, end))}
    memo = {r["bucket_start"]: r for r in cursor.execute(MEMO_ROWS, (start, end))}
    kept = {t: m["unique_ips"] for t, m in memo.items() if runs.get(t) == m["runs_digest"]}
    changed = sorted(t for t in runs if t not in kept)
    if not changed:
        return kept
    counted = dict.fromkeys(changed, 0)
    counted |= {r["bucket_start"]: r["ips"] for r in cursor.execute(BUCKET_IPS, (changed,))}
    if fold:
        cursor.execute(FOLD, (changed,))
        cursor.execute(STORE, (changed, [runs[t] for t in changed], [counted[t] for t in changed]))
    return kept | counted


def count_spans(conn, spans):
    """count(DISTINCT ip_id) of every observation in each [start, end) of spans: {span: IPs}.
    At most PARALLEL queries run at once, each on its own connection importing the calculation's
    snapshot (so they read exactly what the calculation reads)."""
    spans = sorted(set(spans))
    if not spans:
        return {}
    cursor = conn.cursor(row_factory=tuple_row)
    snapshot = cursor.execute("SELECT pg_export_snapshot()").fetchone()[0]
    shares = [spans[i::PARALLEL] for i in range(min(PARALLEL, len(spans)))]
    with ThreadPoolExecutor(len(shares)) as pool:
        results = list(pool.map(lambda share: _count(conn, snapshot, share), shares))
    return {span: ips for found in results for span, ips in found.items()}


def _count(conn, snapshot, spans):
    params = {"password": conn.info.password, "connect_timeout": 5}
    with psycopg.connect(conn.info.dsn, **params) as other:
        other.isolation_level, other.read_only = psycopg.IsolationLevel.REPEATABLE_READ, True
        other.execute(sql.SQL("SET TRANSACTION SNAPSHOT {}").format(sql.Literal(snapshot)))
        return {span: other.execute(SPAN, span).fetchone()[0] for span in spans}


def collect(conn, as_of):
    """Remove the memo rows no window needs: IPs last seen and buckets older than KEEP before
    as_of, and this module's cohort counts unused for 2 days."""
    for statement in COLLECT:
        conn.execute(statement, {"old": as_of - KEEP, "prefix": MEMO})


def _cohorts(conn, as_of, cohort_members):
    """Per cohort window, {(a, b): (IPs, shared)} as _counts() gives it: one member's from its
    window's pool count, two or more from the memo of their runs, counted (and remembered) when
    a member's runs changed."""
    found, sets = [], {}
    for (_, label), (_, members) in cohort_members.items():
        if len(members) == 1:
            ips = members[0][0]["windows"][label]["pool"]["unique_ips"]
            found.append({(0, 0): (ips, 0), (None, None): (ips, 0)} if ips else {})
        else:
            since = as_of - timedelta(hours=members[0][0]["windows"][label]["period"]["hours"])
            sets[len(found)] = (since, members)
            found.append(None)
    if not sets:
        return found
    digests = _digests(conn, as_of, sets)
    cursor = conn.cursor(row_factory=dict_row)
    rows = cursor.execute(REMEMBERED, (list(digests.values()),))
    stored = {r["digest"]: r["counts"] for r in rows}
    cursor.execute(USED, (list(stored),))
    missed = [i for i in sets if digests[i] not in stored]
    for i, counts in zip(missed, _counts(conn, as_of, [sets[i] for i in missed]), strict=True):
        stored[digests[i]] = [[a, b, *n] for (a, b), n in counts.items()]
    fresh = {digests[i]: json.dumps(stored[digests[i]]) for i in missed}
    cursor.execute(REMEMBER, (list(fresh), list(fresh.values())))
    for i, digest in digests.items():
        found[i] = {(a, b): (ips, shared) for a, b, ips, shared in stored[digest]}
    return found


def _digests(conn, as_of, sets):
    """Each set's memo key: its members' providers and runs, each run with its first and last
    bucket in the window (which fix the run's observations in it)."""
    runs = [
        (i, run_key, since, m)
        for i, (since, members) in sets.items()
        for m, (_, keys) in enumerate(members)
        for run_key in sorted(set(keys))
    ]
    owner = {(i, run_key): m for i, run_key, _, m in runs}
    params = [[run[n] for run in runs] for n in range(3)]
    ranges = {i: [[] for _ in members] for i, (_, members) in sets.items()}
    if params[0]:
        for r in conn.cursor(row_factory=dict_row).execute(RANGES, (*params, as_of)):
            ranges[r["slot"]][owner[r["slot"], r["run_key"]]].append(
                [r["run_key"], *(r[k].astimezone(UTC).isoformat() for k in ("first", "last"))]
            )
    digests = {}
    for i, (_, members) in sets.items():
        runs = zip(members, ranges[i], strict=True)
        key = [[group["provider"], sorted(found)] for (group, _), found in runs]
        digests[i] = MEMO + hashlib.sha256(json.dumps(key).encode()).hexdigest()
    return digests


def _counts(conn, as_of, sets):
    """sets: [(since, [(group, run keys) per member])] -> per set, {(a, b): (IPs, shared)} with
    (None, None) the set's distinct IPs."""
    rows = [
        (slot, member, group["provider"], run_key, since)
        for slot, (since, members) in enumerate(sets)
        for member, (group, keys) in enumerate(members)
        for run_key in sorted(set(keys))
    ]
    found = [{} for _ in sets]
    if not rows:
        return found
    names = ("slots", "members", "providers", "keys", "since")
    params = {name: [row[n] for row in rows] for n, name in enumerate(names)}
    for row in conn.cursor(row_factory=dict_row).execute(COUNTS, params | {"as_of": as_of}):
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
