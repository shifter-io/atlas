"""Each network's unique IPs per closed hour, UTC day, rolling 30 UTC days and calendar month,
stored once in calculated_unique_ips (CALC-SCALE-1: the owner's Q9 option 1; the website's
pool.rolling30d and pool.months). A network is a (provider, kind) pair (network.py).

The latest build (its IP state at the cutoff: ipstate.py) records:

- every closed hour and UTC day of the last two days (KEPT: the per-test IP rows the calculation
  keeps), each network's unique IPs and countries, regions, cities and ASNs over its counted
  runs (hourly.slice_figures(): memoised, so an unchanged hour reads nothing), rewritten while a
  late test can still change it, then left as it is;
- at a day's close (a cutoff at 00:00 UTC): the rolling 30 UTC days ending then, the IP state's
  30-day window (ipstate.Vectors.distinct() of the network's scopes), and the month to date, its
  scopes' newest sightings since the month began, with the days that had data; each ranked among
  the networks of the kind (1 + the networks with strictly more unique IPs; ranked_of: the
  networks with any). A month is complete at its last day's close.
"""

from __future__ import annotations

from datetime import UTC, timedelta

from psycopg.rows import tuple_row

from . import hourly
from .policy import DIMENSIONS

HOUR, DAY = timedelta(hours=1), timedelta(days=1)
KEPT = timedelta(days=2)  # the per-test IP rows kept: the hours and days still counted
ROLLING = 720  # the IP state's window of the rolling 30 days (hours)
COLUMNS = ("unique_ips", *DIMENSIONS, "days", "complete", "rank", "ranked_of")
UPSERT = """INSERT INTO calculated_unique_ips(provider,kind,period,starts,unique_ips,countries,
 regions,cities,asns,days,complete,rank,ranked_of)
SELECT * FROM unnest(%s::text[],%s::text[],%s::text[],%s::timestamptz[],%s::bigint[],%s::int[],
 %s::int[],%s::int[],%s::int[],%s::int[],%s::boolean[],%s::int[],%s::int[])
ON CONFLICT(provider,kind,period,starts) DO UPDATE SET unique_ips=excluded.unique_ips,
 countries=excluded.countries,regions=excluded.regions,cities=excluded.cities,asns=excluded.asns,
 days=excluded.days,complete=excluded.complete,rank=excluded.rank,ranked_of=excluded.ranked_of,
 computed_at=now()"""
# The days with data of each network in [start, end) (its stored day rows).
DAYS = """SELECT provider,kind,count(*) FROM calculated_unique_ips WHERE period='day'
 AND starts>=%s AND starts<%s AND unique_ips>0 GROUP BY 1,2"""
# Each network's (n: its index) IPs with a newest sighting of one of its scopes since %s.
SINCE = """SELECT s.n,count(DISTINCT g.ip_id) FROM unnest(%s::int[],%s::bigint[]) AS s(n,scope)
JOIN calculated_scope_ip_last_seen g ON g.scope=s.scope AND g.last_seen>=%s GROUP BY 1"""


def record(conn, as_of, networks, vectors):
    """Record the networks' unique IPs at as_of, the latest build's cutoff. networks: {(provider,
    kind): (product_id, {run_key: requested country}, its scopes)}, the counted runs of the
    window; vectors: ipstate.advance()'s ({window hours: Vectors})."""
    _write(conn, closed(conn, as_of, networks))
    if as_of == _day(as_of) and ROLLING in vectors:
        _write(conn, closing(conn, as_of, networks, vectors[ROLLING]))


def closed(conn, as_of, networks):
    """The rows of the closed hours and UTC days of the last KEPT, each network's."""
    hours = [as_of - n * HOUR for n in range(int(KEPT / HOUR), 0, -1)]
    first = _day(as_of - KEPT)
    days = [d for d in (first, first + DAY, first + 2 * DAY) if d >= as_of - KEPT]
    slices = [(h, h + HOUR) for h in hours] + [(d, d + DAY) for d in days if d + DAY <= as_of]
    runs = {key: (product, asked) for key, (product, asked, _) in networks.items()}
    found = hourly.slice_figures(conn, runs, {key: slices for key in networks})
    rows = []
    for key, figures in found.items():
        for (since, until), f in zip(slices, figures, strict=True):
            if f is not None:
                period = "hour" if until - since == HOUR else "day"
                counts = tuple(f[name] for name in ("unique_ips", *DIMENSIONS))
                rows.append((*key, period, since, *counts, None, True, None, None))
    return rows


def closing(conn, as_of, networks, window):
    """The rows of the day that closes at as_of: each network's rolling 30 days (window: the IP
    state's 30-day Vectors) and month to date, ranked."""
    keys = sorted(networks)
    month = _day(as_of - DAY).replace(day=1)
    rolling = {key: window.distinct(networks[key][2]) for key in keys}
    pairs = [(n, scope) for n, key in enumerate(keys) for scope in sorted(networks[key][2])]
    params = ([n for n, _ in pairs], [scope for _, scope in pairs], month)
    found = dict(conn.cursor(row_factory=tuple_row).execute(SINCE, params))
    to_date = {key: found.get(n, 0) for n, key in enumerate(keys)}
    rows = []
    for period, since, starts, values in (
        ("rolling30d", as_of - 30 * DAY, as_of - DAY, rolling),
        ("month", month, month, to_date),
    ):
        days = _days(conn, since, as_of)
        complete = period == "rolling30d" or as_of.day == 1
        for key, (rank, of) in ranks(values).items():
            counts = (values[key], *(None,) * len(DIMENSIONS), days.get(key, 0))
            rows.append((*key, period, starts, *counts, complete, rank, of))
    return rows


def ranks(values):
    """{(provider, kind): (rank, ranked_of)} of values ({(provider, kind): unique IPs}) among
    the networks of the kind with any: 1 + those with strictly more; (None, None) for none."""
    kinds = {}
    for (_, kind), ips in values.items():
        if ips:
            kinds.setdefault(kind, []).append(ips)
    found = {}
    for (provider, kind), ips in values.items():
        peers = kinds.get(kind, [])
        ranked = (1 + sum(v > ips for v in peers), len(peers))
        found[provider, kind] = ranked if ips else (None, None)
    return found


def _days(conn, since, until):
    rows = conn.cursor(row_factory=tuple_row).execute(DAYS, (since, until))
    return {(provider, kind): n for provider, kind, n in rows}


def _write(conn, rows):
    if rows:
        conn.cursor().execute(UPSERT, [list(column) for column in zip(*rows, strict=True)])


def _day(at):
    return at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
