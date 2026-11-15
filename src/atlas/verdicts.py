"""Whether a country counts for a network (COUNTRY-MIN-IPS-1, the owner's decision of 2026-11-15).

A network's country counts only while its latest daily test there found at least MIN_IPS unique
IPs. Each daily test (a test_runs row whose summary has daily.MARKER = daily.DAILY) is one
verdict in country_verdicts (schema v16): its network (scope_stats.product_id), its country
(upper(pool_stats.requested_country)), its start (decided_at), its unique IPs and whether they
reach MIN_IPS (the generated column counts). The count is the test's own summary.unique_ips: the
distinct exit IPs of its successful answers whose exit DB-IP places in the requested country
(an address DB-IP does not match counts as in it; other-country exits, rate-limited and
no-inventory answers never count: answers.py, runner._record).

A verdict holds from its test's start until the next daily test of the same network and country
starts. Before a country's first daily test there is no verdict and the country does not count.
Everything measured for the network in the country while it does not count (its daily tests,
pulses, browser runs, outages and events) is left out of every published figure; nothing is
deleted, and the countries keep being tested so they can count again.
"""

from __future__ import annotations

from psycopg.rows import tuple_row

from .daily import DAILY, MARKER

MIN_IPS = 10  # schema.sql's country_verdicts.counts repeats it
# outages.end_reason of an outage closed because its country stopped counting
END_REASON = "too_few_ips"

# The verdicts of the daily tests among the batch's new runs (incoming_runs, storage.ingest's
# temp table), in run order; a replay adds none. migrate_v15_v16.sql repeats it for every stored
# daily test.
RECORD = (
    "INSERT INTO country_verdicts(product_id,country_code,decided_at,run_key,unique_ips) "
    "SELECT s.product_id,upper(p.requested_country),t.started_at,t.run_key,"
    "coalesce((t.summary->>'unique_ips')::bigint,0) "
    "FROM test_runs t JOIN scope_stats s USING(scope_id) JOIN pool_stats p USING(pool_id) "
    f"WHERE t.summary->>'{MARKER}'='{DAILY}' AND upper(p.requested_country) ~ '^[A-Z]{{2}}$' "
    "AND t.run_key IN (SELECT run_key FROM incoming_runs) "
    "ORDER BY t.run_key ON CONFLICT DO NOTHING"
)


def daily(summary: dict) -> bool:
    """Whether a run's summary is a daily test's (it gets a verdict)."""
    return summary.get(MARKER) == DAILY


def record(conn, runs) -> None:
    """Queue the verdicts of the daily tests among runs (the batch's new runs, already in
    incoming_runs with their run keys); nothing when none is a daily test."""
    if any(daily(run.summary) for run in runs):
        conn.execute(RECORD)


# The counting spans [lo, hi) of every network and country that overlap [%(spans_from)s,
# %(spans_to)s): from a verdict that counts to the next verdict of the same network and country
# (infinity while there is none). Verdicts that do not count leave gaps; so does the time before a
# country's first verdict.
SPANS = (
    "SELECT product_id,country_code::text AS country_code,lo,hi FROM (SELECT product_id,"
    "country_code,decided_at AS lo,counts,lead(decided_at,1,'infinity') OVER (PARTITION BY "
    "product_id,country_code ORDER BY decided_at) AS hi FROM country_verdicts "
    "WHERE decided_at<%(spans_to)s) v WHERE counts AND hi>%(spans_from)s"
)
# The spans cut by UTC day inside [spans_from, spans_to): a row per day a span touches. A reader
# joins its rows to them on network, country and day, then keeps a row whose time is in the
# span (counted()): one hash or merge join, linear in the rows, never a lookup per row.
DAYS = (
    "SELECT s.product_id,s.country_code,(d AT TIME ZONE 'UTC')::date AS day,s.lo,s.hi "
    f"FROM ({SPANS}) s CROSS JOIN LATERAL generate_series(date_trunc('day',"
    "greatest(s.lo,%(spans_from)s),'UTC'),least(s.hi,%(spans_to)s),interval '1 day') d"
)
WITH_DAYS = f"WITH counted_days AS MATERIALIZED ({DAYS}) "
# The condition that keeps a row counted() joined, or every row when %(verdicts)s is false.
COUNTED = "(cv.day IS NOT NULL OR NOT %(verdicts)s)"
# Keeps a row only when its network's country counted at its time (the latest verdict up to it
# counts), or when %(verdicts)s is false (the methodology counts every country), for SQL whose
# rows are few (an hour's geo rows): one index probe per row, or per run where the planner
# memoizes it.
COUNTS_AT = (
    "JOIN LATERAL (SELECT NOT %(verdicts)s OR coalesce((SELECT v.counts FROM country_verdicts v "
    "WHERE v.product_id={product} AND v.country_code={country}::char(2) AND v.decided_at<={at} "
    "ORDER BY v.decided_at DESC LIMIT 1),false) AS counts) {alias} ON {alias}.counts "
)


def counted(product: str, country: str, at: str, alias: str = "cv") -> str:
    """A LEFT JOIN of counted_days (WITH_DAYS) to a row of network `product`, country
    `country` (upper case) and time `at` (SQL expressions): `alias`.day is NOT NULL when the
    country counted for the network then (COUNTED keeps those rows). A span holds a time when
    lo <= time < hi."""
    return (
        f"LEFT JOIN counted_days {alias} ON {alias}.product_id={product} "
        f"AND {alias}.country_code={country} AND {alias}.day=({at} AT TIME ZONE 'UTC')::date "
        f"AND {at}>={alias}.lo AND {at}<{alias}.hi "
    )


def counts_at(product: str, country: str, at: str, alias: str = "cv") -> str:
    """COUNTS_AT for SQL expressions of a row's network, country (upper case) and time."""
    return COUNTS_AT.format(product=product, country=country, at=at, alias=alias)


def bounds(start, end, rule=True) -> dict:
    """The parameters of SPANS, DAYS, counted(), COUNTED and COUNTS_AT for rows of [start, end);
    rule: whether the methodology's country_verdicts applies."""
    return {"spans_from": start, "spans_to": end, "verdicts": rule}


def spans(conn, start, end) -> list[dict]:
    """The counting spans overlapping [start, end), clipped to it, in order: product_id, country,
    lo, hi."""
    query = (
        "SELECT product_id,country_code,greatest(lo,%(spans_from)s),least(hi,%(spans_to)s) "
        f"FROM ({SPANS}) s ORDER BY product_id,country_code,lo"
    )
    rows = conn.cursor(row_factory=tuple_row).execute(query, bounds(start, end)).fetchall()
    return [{"product_id": r[0], "country": r[1], "lo": r[2], "hi": r[3]} for r in rows]


def by_country(rows: list[dict]) -> dict:
    """{product_id: {country: [(lo, hi)]}} of spans()' rows."""
    found = {}
    for r in rows:
        found.setdefault(r["product_id"], {}).setdefault(r["country"], []).append(
            (r["lo"], r["hi"])
        )
    return found


def overlap(pieces, spans) -> list:
    """The parts of the intervals `pieces` inside the union of `spans`, as (lo, hi) in order."""
    merged = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    found = []
    for a, b in pieces:
        found += [(max(a, lo), min(b, hi)) for lo, hi in merged if max(a, lo) < min(b, hi)]
    return found


def clip(outages: list[dict], counting: dict, end) -> list[dict]:
    """The outages (window_outages() rows: product_id, country, started_at, ended_at, None while
    open at `end`) cut to the time their country counted (by_country()): an outage becomes one
    row per counting span it overlaps; a part that ends before `end` ends there, a part that
    reaches `end` keeps the outage's own end."""
    found = []
    for o in outages:
        spans = counting.get(o["product_id"], {}).get(o["country"].strip(), ())
        stop = o["ended_at"] or end
        for lo, hi in overlap([(o["started_at"], stop)], spans):
            found.append(o | {"started_at": lo, "ended_at": o["ended_at"] if hi >= stop else hi})
    return found
