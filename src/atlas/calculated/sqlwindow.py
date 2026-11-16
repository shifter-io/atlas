"""A window's IP figures counted in the database (contract K3 of the incremental calculation).

`window_inputs()` returns, for each window spec (a product's runs over a half-open bucket range),
everything the calculator derives from the window's IP observations, without sending one row per
IP back to Python: per label combination the IPs and observations, and the totals and the
first-seen histogram that `poolfig.pool_figures()` turns into `aggregate.pool()`'s counts.

Each IP of a window is counted once, as `aggregate.pool()` does:

- its labels are its DB-IP enrichment in one release. In "newest" mode that is `release_id` for
  every run; in "per_run" mode it is the newest release the run was enriched with
  (`run_enrichments`), else `release_id` (pulse runs have none). The calculator's join today
  (`releases.py`) is "per_run" with no `release_id`: a run without a release stays unlabelled.
  An IP seen by runs with different releases takes the release of its lowest run_key in the
  window, the run whose row `pool()` meets first;
- it is located when it has a DB-IP country code and some run that returned it asked for a
  country; it matches when every asking run asked for that country (`asked == {cc.lower()}`);
- its days are the distinct dates of its buckets in the session time zone (`datetime.date()` of
  the values psycopg returns); it recurs on two or more;
- its first-seen bin is the 5-minute bucket of the product's `product_ips.first_seen`, so the new
  IPs since any cutoff on the 5-minute grid are the bins at or after it.

Combinations keep the raw label values (NULL and empty strings stay apart), so Python applies
the calculator's own truthiness to them. A window's IPs partition exactly by combination.

The windows of one group (a product's runs with their requested countries, as each calculated
group's 24h, 7d and 30d windows are) are counted in one pass over the group's observation rows
(`onepass()`): each row is read and hash-aggregated once and each IP joined to its first-seen
bin once, instead of once per window with a sort per window (CALC-WINDOW-ONEPASS-1).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cache
from typing import Literal

from psycopg.rows import dict_row

ReleaseMode = Literal["newest", "per_run"]
LABELS = (
    "country_code",
    "country",
    "region_code",
    "region",
    "city",
    "asn",
    "asn_organization",
    "usage_type",
)
# The longest run, in distinct observation buckets, whose every contiguous range run_ranges()
# counts (12 buckets give 78 ranges).
MAX_RANGE_BUCKETS = 12
_LAST_BUCKET = timedelta(microseconds=1)  # an inclusive last bucket as a half-open bound

# A run's enrichment release under each mode; %(per_run)s and %(release)s are parameters.
_RUN_RELEASE = (
    "coalesce(CASE WHEN %(per_run)s THEN (SELECT max(e.release_id) FROM run_enrichments e "
    "WHERE e.run_key=k.run_key) END,%(release)s::int)"
)
_LABELS = ",".join(LABELS)
# The most windows one pass counts; a group with more is counted in passes of this many.
MAX_WINDOWS = 8


def _fragments(width: int) -> dict[str, str]:
    """onepass()'s per-window SQL: r<j> is the IP's lowest [run_key, release] in window j, and
    a release row of `releases` is first when no earlier window of the IP has its release."""
    positions = range(width)
    inside = [f"o.bucket_start>=k.s{j} AND o.bucket_start<k.u{j}" for j in positions]
    same = "p.d{i}>0 AND p.r{i}[2] IS NOT DISTINCT FROM p.r{j}[2]"
    first = [
        f"p.d{j}>0" + "".join(f" AND NOT ({same.format(i=i, j=j)})" for i in range(j))
        for j in positions
    ]
    return {
        "bounds": ",".join(f"%(s{j})s::timestamptz[],%(u{j})s::timestamptz[]" for j in positions),
        "names": ",".join(f"s{j},u{j}" for j in positions),
        "days": ",".join(
            f"sum(o.count) FILTER (WHERE {w}) AS c{j},min(k.asked) FILTER (WHERE {w}) AS a{j},"
            f"max(k.asked) FILTER (WHERE {w}) AS b{j},"
            f"min(ARRAY[o.run_key,k.release_id]) FILTER (WHERE {w}) AS r{j}"
            for j, w in enumerate(inside)
        ),
        "per_ip": ",".join(
            f"sum(c{j})::bigint AS c{j},count(c{j}) AS d{j},min(a{j}) AS a{j},max(b{j}) AS b{j},"
            f"min(r{j}) AS r{j}"
            for j in positions
        ),
        "releases": ",".join(
            f"(p.r{j}[2],p.first_seen IS NOT NULL AND {first[j]})" for j in positions
        ),
        "windows": ",".join(f"({j},p.c{j},p.d{j},p.a{j},p.b{j},p.r{j}[2])" for j in positions),
    }


@cache
def onepass(width: int) -> str:
    """The statement that counts up to `width` windows of each group (a product's runs with
    their requested countries) in one pass over the group's observation rows.

    k holds one row per (group, run): asked is the run's requested country in lower case (NULL
    when it asked for none), [lo, hi) the group's widest range and [s<j>, u<j>) its window j.
    Each observation row is read and aggregated once, into its IP's day (`day`), then its IP
    (`per_ip`, with its first-seen time); every aggregate is hash-friendly and filtered per
    window. Each IP is labelled once per distinct release of its windows (the release of its
    lowest run_key in each), then unpivoted into one row per window it was seen in (slot:
    group * width + j).
    """
    sql = _fragments(width)
    return f"""WITH k AS MATERIALIZED (
 SELECT k.*,{_RUN_RELEASE} AS release_id
 FROM unnest(%(groups)s::int[],%(keys)s::bigint[],%(products)s::int[],%(asked)s::text[],
  %(lo)s::timestamptz[],%(hi)s::timestamptz[],{sql["bounds"]})
  AS k(grp,run_key,product_id,asked,lo,hi,{sql["names"]})
), day AS (
 SELECT k.grp,k.product_id,o.ip_id,{sql["days"]}
 FROM k JOIN ip_observations o ON o.run_key=k.run_key AND o.bucket_start>=k.lo
  AND o.bucket_start<k.hi
 GROUP BY k.grp,k.product_id,o.ip_id,o.bucket_start::date
), per_ip AS (
 SELECT grp,ip_id,{sql["per_ip"]},(SELECT m.first_seen FROM product_ips m
  WHERE m.product_id=day.product_id AND m.ip_id=day.ip_id) AS first_seen
 FROM day GROUP BY grp,product_id,ip_id
), labelled AS (
 SELECT p.grp*{width}+w.j AS slot,w.observations,w.days,e.matched,
  {",".join(f"e.{f}" for f in LABELS)},
  coalesce(e.country_code,'')<>'' AND w.asked IS NOT NULL AS located,
  coalesce(e.country_code,'')<>'' AND w.asked=w.asked_max AND w.asked=lower(e.country_code)
   AS geo,
  date_bin('5 minutes',p.first_seen,'1970-01-01 00:00:00+00'::timestamptz) AS bin
 FROM per_ip p CROSS JOIN LATERAL (VALUES {sql["releases"]}) AS x(release,first)
 LEFT JOIN ip_enrichment_fields e ON e.ip_id=p.ip_id AND e.release_id=x.release
 CROSS JOIN LATERAL (VALUES {sql["windows"]}) AS w(j,observations,days,asked,asked_max,release)
 WHERE x.first AND w.days>0 AND w.release IS NOT DISTINCT FROM x.release
)
SELECT slot,GROUPING(country_code,bin) AS part,{_LABELS},bin,count(*) AS ips,
 sum(observations)::bigint AS observations,count(*) FILTER (WHERE located) AS located,
 count(*) FILTER (WHERE geo) AS geo_matched,count(*) FILTER (WHERE days>=2) AS recurring,
 count(*) FILTER (WHERE matched) AS matched
FROM labelled GROUP BY GROUPING SETS ((slot,{_LABELS}),(slot,bin),(slot))
ORDER BY slot,part,{_LABELS},bin"""


# GROUPING(country_code,bin): 1 a label combination, 2 a first-seen bin, 3 the window's totals.
COMBO, BIN, TOTAL = 1, 2, 3

# The distinct observation buckets of each run, with its product, requested country and release.
RUN_BUCKETS = f"""SELECT k.run_key,s.product_id,s.scope->'config'->>'requested_country' AS asked,
 {_RUN_RELEASE} AS release_id,
 ARRAY(SELECT DISTINCT o.bucket_start FROM ip_observations o WHERE o.run_key=k.run_key
  ORDER BY 1) AS buckets
FROM unnest(%(keys)s::bigint[]) AS k(run_key) JOIN test_runs r USING(run_key)
JOIN scope_details s ON s.scope_id=r.scope_id ORDER BY k.run_key"""

# Distinct IPs of each window's runs (slot: the window, run_key and country: its tests), per
# country and in all (country_code NULL), as estimates.window.DISTINCT counts them.
DISTINCT = """SELECT k.slot,{country} AS country_code,count(DISTINCT o.ip_id) AS ips
FROM unnest(%(slots)s::int[],%(keys)s::bigint[],%(countries)s::text[])
 AS k(slot,run_key,country_code) JOIN ip_observations o USING(run_key)
GROUP BY {groups} ORDER BY 1,2"""


@dataclass(frozen=True)
class WindowSpec:
    """A product's runs over the half-open bucket range [since, until); key names it in the
    result, asked maps each run_key to the country it requested (None: none), and early each
    run_key whose range starts before since to its own start (verdicts.REACH)."""

    key: object
    product_id: int
    runs: tuple[int, ...]
    since: datetime
    until: datetime
    asked: dict[int, str | None] = field(default_factory=dict)
    early: dict[int, datetime] = field(default_factory=dict)


@dataclass(frozen=True)
class Combo:
    """The IPs of a window with one label combination (place and network as `pool()` reads
    them from the IP's DB-IP record): located and geo_matched as in `pool()`'s geo_accuracy."""

    place: tuple  # (country_code, country, region_code, region, city)
    network: tuple  # (asn, asn_organization)
    usage_type: str | None
    ips: int
    observations: int
    located: int
    geo_matched: int

    def row(self) -> dict:
        """The DB-IP label fields of the combination's IPs, as `pool()` entries carry them."""
        return dict(zip(LABELS, (*self.place, *self.network, self.usage_type), strict=True))


@dataclass(frozen=True)
class WindowInputs:
    """What one window's IP observations give the calculator; first_seen maps each 5-minute
    bin start to the IPs first seen by the product in it."""

    combos: tuple[Combo, ...] = ()
    unique_ips: int = 0
    recurring_ips: int = 0
    observations: int = 0
    enrichment_matched: int = 0
    first_seen: dict[datetime, int] = field(default_factory=dict)


def _release_params(release_mode: ReleaseMode, release_id: int | None) -> dict:
    if release_mode not in ("newest", "per_run"):
        raise ValueError(f"release_mode must be 'newest' or 'per_run', not {release_mode!r}")
    return {"per_run": release_mode == "per_run", "release": release_id}


def _passes(specs) -> list[tuple[tuple, list[int]]]:
    """The specs' indices batched by group (product, runs and their requested countries, in
    lower case), at most MAX_WINDOWS per pass."""
    groups = defaultdict(list)
    for n, spec in enumerate(specs):
        if spec.until <= spec.since:
            raise ValueError(f"window {spec.key!r} ends before it starts")
        runs = tuple(sorted(set(spec.runs)))
        asked = tuple((spec.asked.get(run) or "").lower() or None for run in runs)
        groups[spec.product_id, runs, asked].append(n)
    return [
        (group, members[i : i + MAX_WINDOWS])
        for group, members in groups.items()
        for i in range(0, len(members), MAX_WINDOWS)
    ]


def window_query(specs, *, release_mode: ReleaseMode, release_id: int | None):
    """The one statement that counts every spec of a batch, its parameters, and the spec index
    of each result slot."""
    specs = list(specs)
    passes = _passes(specs)
    width = max((len(members) for _, members in passes), default=1)
    params, slots = defaultdict(list), {}
    for grp, ((product, runs, asked), members) in enumerate(passes):
        windows = [specs[n] for n in members]
        slots |= {grp * width + j: n for j, n in enumerate(members)}
        bounds = [(w.since, w.until) for w in windows] + [(None, None)] * (width - len(windows))
        lo = min(min([w.since, *w.early.values()]) for w in windows)
        shared = {"groups": grp, "products": product, "lo": lo}
        shared["hi"] = max(w.until for w in windows)
        for j, (since, until) in enumerate(bounds):
            shared |= {f"s{j}": since, f"u{j}": until}
        for run_key, country in zip(runs, asked, strict=True):
            own = {f"s{j}": w.early[run_key] for j, w in enumerate(windows) if run_key in w.early}
            for name, value in (shared | own | {"keys": run_key, "asked": country}).items():
                params[name].append(value)
    return onepass(width), dict(params) | _release_params(release_mode, release_id), slots


def window_inputs(
    conn, specs, *, release_mode: ReleaseMode, release_id: int | None
) -> dict[object, WindowInputs]:
    """WindowInputs of every spec, counted in one statement that reads each observation row of
    a group once; a window without observations gets empty inputs. Spec keys must be distinct."""
    specs = list(specs)
    if len({spec.key for spec in specs}) != len(specs):
        raise ValueError("window spec keys must be distinct")
    parts = [{"combos": [], "first_seen": {}, "totals": {}} for _ in specs]
    statement, params, slots = window_query(specs, release_mode=release_mode, release_id=release_id)
    rows = ()
    if params.get("keys"):
        rows = conn.cursor(row_factory=dict_row).execute(statement, params)
    for row in rows:
        _add(parts[slots[row["slot"]]], row)
    return {spec.key: _inputs(part) for spec, part in zip(specs, parts, strict=True)}


def _add(part: dict, row: dict) -> None:
    """One result row into its window's combinations, first-seen bins or totals."""
    if row["part"] == COMBO:
        part["combos"].append(_combo(row))
    elif row["part"] == BIN:
        part["first_seen"][row["bin"]] = row["ips"]
    else:
        part["totals"] = row


def _combo(row: dict) -> Combo:
    return Combo(
        place=tuple(row[f] for f in LABELS[:5]),
        network=(row["asn"], row["asn_organization"]),
        usage_type=row["usage_type"],
        ips=row["ips"],
        observations=row["observations"],
        located=row["located"],
        geo_matched=row["geo_matched"],
    )


def _inputs(part: dict) -> WindowInputs:
    totals = part["totals"]
    return WindowInputs(
        combos=tuple(part["combos"]),
        unique_ips=totals.get("ips", 0),
        recurring_ips=totals.get("recurring", 0),
        observations=totals.get("observations", 0),
        enrichment_matched=totals.get("matched", 0),
        first_seen=part["first_seen"],
    )


def run_ranges(
    conn,
    run_keys,
    *,
    release_mode: ReleaseMode,
    release_id: int | None,
    max_buckets: int = MAX_RANGE_BUCKETS,
) -> dict[tuple, WindowInputs]:
    """WindowInputs of every contiguous range of each run's observation buckets.

    Keys are (run_key, release_id, first_bucket, last_bucket), the bounds inclusive and the
    release the run's labels came from. A run with more than max_buckets buckets has no ranges.
    """
    params = {"keys": sorted(set(run_keys))} | _release_params(release_mode, release_id)
    rows = conn.cursor(row_factory=dict_row).execute(RUN_BUCKETS, params).fetchall()
    specs = []
    for row in rows:
        buckets = row["buckets"]
        if len(buckets) > max_buckets:
            continue
        for i, first in enumerate(buckets):
            for last in buckets[i:]:
                specs.append(
                    WindowSpec(
                        key=(row["run_key"], row["release_id"], first, last),
                        product_id=row["product_id"],
                        runs=(row["run_key"],),
                        since=first,
                        until=last + _LAST_BUCKET,
                        asked={row["run_key"]: row["asked"]},
                    )
                )
    by_release = defaultdict(list)
    for spec in specs:
        by_release[spec.key[1]].append(spec)
    result = {}
    for release, group in by_release.items():
        # Every run's release is already resolved, so its ranges are counted in "newest" mode.
        result |= window_inputs(conn, group, release_mode="newest", release_id=release)
    return result


def distinct_ips(conn, run_sets, by_country: bool = True) -> list[dict]:
    """For each run set of (run_key, country_code) pairs, the distinct IPs its runs observed,
    by country and in all (key None); only in all when by_country is false. Equals
    `estimates.window.distinct()` for the same pairs."""
    run_sets = [list(pairs) for pairs in run_sets]
    slots, keys, countries = [], [], []
    for slot, pairs in enumerate(run_sets):
        for run_key, country in sorted(set(pairs), key=lambda p: (p[0], p[1] or "")):
            slots.append(slot)
            keys.append(run_key)
            countries.append(country)
    counts = [{} for _ in run_sets]
    if slots:
        statement = DISTINCT.format(
            country="k.country_code" if by_country else "NULL::text",
            groups="GROUPING SETS ((k.slot,k.country_code),(k.slot))" if by_country else "k.slot",
        )
        bounds = {"slots": slots, "keys": keys, "countries": countries}
        for row in conn.cursor(row_factory=dict_row).execute(statement, bounds):
            counts[row["slot"]][row["country_code"]] = row["ips"]
    return counts
