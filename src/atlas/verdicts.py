"""Whether a country counts for a network (COUNTRY-MIN-IPS-1, the owner's decision of 2026-11-15).

A network's country counts only while its latest daily test there found at least MIN_IPS unique
IPs, or that test is the first under MIN_IPS of a country that was normally big (two days in a
row, LOWAVAIL-1, schema v19: RECORD sets counts from the verdicts before it). Each daily test (a
test_runs row whose summary has daily.MARKER = daily.DAILY) is one verdict in country_verdicts
(schema v16): its network (scope_stats.product_id), its country (upper(pool_stats.
requested_country)), its start (decided_at), its unique IPs and whether the country counts
(counts). The count is the test's own summary.unique_ips: the distinct exit IPs of its
successful answers whose exit DB-IP places in the requested country (an address DB-IP does not
match counts as in it; other-country exits, rate-limited and no-inventory answers never count:
answers.py, runner._record).

A verdict holds from its test's start until the next daily test of the same network and country
starts. Before a country's first daily test there is no verdict and the country does not count.
Everything measured for the network in the country while it does not count (its daily tests,
pulses, browser runs, outages and events) is left out of every published figure; nothing is
deleted, and the countries keep being tested so they can count again.
"""

from __future__ import annotations

from psycopg.rows import tuple_row

from .daily import DAILY, MARKER

MIN_IPS = 10
# Two days in a row (LOWAVAIL-1, the owner's rule; country_status.first_day): a test under MIN_IPS
# still counts when the country was normally big: its previous verdict had MIN_IPS or more and its
# verdicts of the RECENT before it reached BIG. migrate_v18_v19.sql repeats the three numbers.
BIG = 50
RECENT_DAYS = 7
RECENT = f"{RECENT_DAYS} days"
# outages.end_reason of an outage closed because its country stopped counting
END_REASON = "too_few_ips"

# The verdicts of the daily tests among the batch's new runs (incoming_runs, storage.ingest's
# temp table), in run order; a replay adds none. counts follows the verdicts before each (HISTORY),
# the stored ones of its network and country and the batch's own. Daily tests are imported in
# time order, so a verdict's counts never changes later; a test imported after a later one of the
# same country (a late upload) leaves that later verdict as it was (db-migrate's v19 step would
# set it from both). migrate_v15_v16.sql and
# migrate_v18_v19.sql repeat it for every stored daily test.
NEW = (
    "SELECT s.product_id,upper(p.requested_country) AS country_code,t.started_at AS decided_at,"
    "t.run_key,coalesce((t.summary->>'unique_ips')::bigint,0) AS unique_ips "
    "FROM test_runs t JOIN scope_stats s USING(scope_id) JOIN pool_stats p USING(pool_id) "
    f"WHERE t.summary->>'{MARKER}'='{DAILY}' AND upper(p.requested_country) ~ '^[A-Z]{{2}}$' "
    "AND t.run_key IN (SELECT run_key FROM incoming_runs) "
    "AND t.run_key NOT IN (SELECT run_key FROM country_verdicts)"
)
# Each verdict of {rows} (product_id, country_code, decided_at, run_key, unique_ips, new) with its
# counts: MIN_IPS or more, or a previous verdict with MIN_IPS or more and BIG reached in RECENT.
HISTORY = (
    f"SELECT y.*,y.unique_ips>={MIN_IPS} OR coalesce(y.previous>={MIN_IPS} AND y.recent>={BIG},"
    "false) AS counts FROM (SELECT x.*,lag(x.unique_ips) OVER (PARTITION BY x.product_id,"
    "x.country_code ORDER BY x.decided_at) AS previous,max(x.unique_ips) OVER (PARTITION BY "
    f"x.product_id,x.country_code ORDER BY x.decided_at RANGE BETWEEN interval '{RECENT}' "
    "PRECEDING AND CURRENT ROW EXCLUDE CURRENT ROW) AS recent FROM ({rows}) x) y"
)
RECORD = (
    f"WITH n AS ({NEW}) "
    "INSERT INTO country_verdicts(product_id,country_code,decided_at,run_key,unique_ips,counts) "
    "SELECT h.product_id,h.country_code,h.decided_at,h.run_key,h.unique_ips,h.counts FROM ("
    + HISTORY.format(
        rows="SELECT n.*,true AS new FROM n UNION ALL SELECT w.product_id,w.country_code,"
        "w.decided_at,w.run_key,w.unique_ips,false FROM country_verdicts w "
        "WHERE (w.product_id,w.country_code) IN (SELECT product_id,country_code FROM n)"
    )
    + ") h WHERE h.new ORDER BY h.run_key ON CONFLICT DO NOTHING"
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
# The spans cut by UTC day inside [spans_from, spans_to): a row per day a span touches (UTC dates
# stepped as timestamps without a zone, so no session time zone moves a day). A reader
# joins its rows to them on network, country and day, then keeps a row whose time is in the
# span (counted()): one hash or merge join, linear in the rows, never a lookup per row.
DAYS = (
    "SELECT s.product_id,s.country_code,d::date AS day,s.lo,s.hi "
    f"FROM ({SPANS}) s CROSS JOIN LATERAL generate_series("
    "(greatest(s.lo,%(spans_from)s) AT TIME ZONE 'UTC')::date::timestamp,"
    "(least(s.hi,%(spans_to)s) AT TIME ZONE 'UTC')::date::timestamp,interval '1 day') d"
)
WITH_DAYS = f"WITH counted_days AS MATERIALIZED ({DAYS}) "
# A batch run's time for its verdict (r: its test_runs row, br: batch_runs, b: test_batches): its
# stored start, else the orchestrator's, else its slot's.
RUN_AT = "coalesce(r.started_at,br.started_at,b.scheduled_for)"
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


# Each network's latest verdict per country decided by %(as_of)s.
LATEST = (
    "SELECT DISTINCT ON (product_id,country_code) product_id,country_code::text,counts,unique_ips "
    "FROM country_verdicts WHERE decided_at<=%(as_of)s "
    "ORDER BY product_id,country_code,decided_at DESC"
)
# The API's country_status codes. too_few_ips is shown as "low availability", no_exits ("no
# exits") is the same with no IP found at all (LOWAVAIL-1, the owner's names).
STATUS = ("counts", "too_few_ips", "no_daily_test", "no_exits")


def latest(conn, as_of) -> dict:
    """{product_id: {country: (counts, unique_ips)}} of every network's latest verdicts."""
    found = {}
    for product, country, counts, ips in conn.cursor(row_factory=tuple_row).execute(
        LATEST, {"as_of": as_of}
    ):
        found.setdefault(product, {})[country] = (counts, ips)
    return found


def status(found: list) -> dict:
    """A requested country's status for a network from its products' latest verdicts there
    (latest()'s (counts, unique_ips) pairs): it counts when it counts for one of them (the most
    IPs of those), else low availability (the most found), or no exits when none found one,
    else no daily test yet."""
    for wanted, code in ((True, STATUS[0]), (False, STATUS[1])):
        ips = [n for counts, n in found if counts is wanted]
        if ips:
            most = max(ips)
            return {"status": STATUS[3] if not (wanted or most) else code, "unique_ips": most}
    return {"status": STATUS[2], "unique_ips": None}


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


# The open outages of a network's country that does not count at %(now)s (its latest verdict up to
# then does not count, or it has none): each ends at that verdict's start, or its own start
# without one (it never counted), with end_reason END_REASON.
CLOSE = f"""UPDATE outages o
 SET ended_at=greatest(o.started_at,coalesce(v.decided_at,o.started_at)),end_reason='{END_REASON}'
 FROM outages x LEFT JOIN LATERAL (SELECT w.decided_at,w.counts
 FROM country_verdicts w WHERE w.product_id=x.product_id AND w.country_code=x.country_code
 AND w.decided_at<=%(now)s ORDER BY w.decided_at DESC LIMIT 1) v ON true
WHERE o.outage_id=x.outage_id AND x.ended_at IS NULL AND x.started_at<=%(now)s
 AND NOT coalesce(v.counts,false) RETURNING o.outage_id"""
