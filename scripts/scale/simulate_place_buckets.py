#!/usr/bin/env python3
"""PLACE-LATENCY-TEST-1 (#870): generated per-place buckets for the stored daily tests of the
test days (test data, model place-v1), so their region, city, ASN and City x ASN rows get
requests and latency.

Usage:
  simulate_place_buckets.py --database <toml> --from <ISO> --to <ISO> [--dry-run]
      [--largest-bucket]
      [--owner-go "owner-go PRODUCTION-AS-STAGING <YYYY-MM-DD>" --i-know-this-is-production]
      (production: with PROXY_INDEX_ALLOW_PRODUCTION_TARGET=1 in the environment)

A daily test records its requests per 5-minute bucket (run_buckets) and its exit IPs, not its
requests per exit place (run_geo_buckets). For every daily test (no batch, test_kind daily) that
started in [--from, --to), this writes run_geo_buckets from its stored rows (SIMULATE.md,
"Per-place buckets of the daily tests"):
- its observed IPs are grouped by DB-IP location and ASN (the run's newest enrichment release);
  the IPs in the requested country make the place groups, every other IP (another country, or
  unmatched) the all-NULL location and network row, which the calculation counts towards the
  requested country, as a request without an exit address;
- each bucket's outcomes and bytes are split over the groups by IP count (largest remainder),
  its proxy errors go to the all-NULL row, and its latency histogram is spread over the groups'
  successes rank by rank, so every sum over the groups equals the stored bucket exactly;
- a place group's latency is its share of the histogram at ceil(ms x g), g = exp(0.15 u), u in
  [-1, 1] from md5(provider:requested_country:location_id:asn), centred per run so the groups'
  success-weighted geometric mean of g is 1; the all-NULL row keeps g = 1. A group without
  successes has no latency.
No IP is written anywhere. Each patched run's summary gets artifacts.geo {"model": "place-v1",
"rows": n}: the test-data mark, and what makes the calculation read the run's requests from its
geo rows instead of its requested country (calculator._run_fields, "placeless"). A run that has
the mark or any geo row is left as it is, so a second run writes nothing. The geo rollup marks
of the patched hours are deleted, so the next calculation counts those hours again.
--largest-bucket writes each run's rows into its largest bucket only (fewer rows; a run that
spans a window's cut then counts in one window). --dry-run writes nothing and prints the counts.
Production's proxy_reviews needs #817's three (daily/audits.py target_refusal).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import accumulate

from simulate_places import PHI
from synthetic import apportion, signed_unit

from proxy_index import names, partitions
from proxy_index.calculated.rollups import LOCK
from proxy_index.daily import DAILY, MARKER
from proxy_index.daily.audits import target_refusal
from proxy_index.production_guard import ACKNOWLEDGE, PRODUCTION_GO_FORM
from proxy_index.stats import LOCATION_FIELDS, NETWORK_FIELDS
from proxy_index.storage import connect
from proxy_index.storage_read import ENRICHMENT_FIELDS

MODEL = "place-v1"
SPREAD = 0.15
BATCH = 200  # runs per transaction: a stopped patch keeps the batches it committed
MARGIN = timedelta(days=1)  # a run's buckets and IPs end at most this long after its start
RUNS = (
    "SELECT r.run_key,p.provider,ps.requested_country,(r.summary->'artifacts' ? 'geo' "
    "OR EXISTS (SELECT FROM run_geo_buckets g WHERE g.run_key=r.run_key)) AS patched "
    "FROM test_runs r JOIN scope_stats s ON s.scope_id=r.scope_id "
    "JOIN products p ON p.product_id=s.product_id JOIN pool_stats ps ON ps.pool_id=s.pool_id "
    "WHERE r.batch_id IS NULL AND r.summary->>%(marker)s=%(daily)s "
    "AND r.started_at>=%(lo)s AND r.started_at<%(hi)s ORDER BY r.run_key"
)
# Each run's IPs per DB-IP location and network (its newest release: lists.ENRICHMENTS).
GROUPS = (
    "WITH release AS (SELECT DISTINCT ON (run_key) run_key,release_id FROM run_enrichments "
    "WHERE run_key=ANY(%(runs)s) ORDER BY run_key,release_id DESC) "
    "SELECT o.run_key,e.location_id,e.network_id,l.country_code,n.asn,count(DISTINCT o.ip_id) "
    "FROM ip_observations o LEFT JOIN release r ON r.run_key=o.run_key "
    "LEFT JOIN ip_enrichment e ON e.ip_id=o.ip_id AND e.release_id=r.release_id "
    "LEFT JOIN enrichment_location_fields l ON l.location_id=e.location_id "
    "LEFT JOIN enrichment_network_fields n ON n.network_id=e.network_id "
    "WHERE o.run_key=ANY(%(runs)s) AND o.bucket_start>=%(lo)s AND o.bucket_start<%(end)s "
    "GROUP BY 1,2,3,4,5 ORDER BY 1,2,3"
)
BUCKETS = (
    "SELECT run_key,bucket_start,completed,successes,challenges,blocks,timeouts,proxy_errors,"
    "bytes_up,bytes_down,histogram_steps,histogram_counts FROM run_buckets "
    "WHERE run_key=ANY(%(runs)s) AND bucket_start>=%(lo)s AND bucket_start<%(end)s ORDER BY 1,2"
)
COLUMNS = "bucket_start,run_key,location_id,network_id,completed,successes,challenges,blocks,"
COLUMNS += "timeouts,bytes_up,bytes_down,histogram_steps,histogram_counts"
SPLIT = ("successes", "challenges", "blocks", "timeouts", "other", "bytes_up", "bytes_down")
# One all-NULL enrichment row: the unknown location and network (names.py).
UNKNOWN = (*LOCATION_FIELDS, *NETWORK_FIELDS)
UNKNOWN = f"(SELECT {','.join(f'NULL::{ENRICHMENT_FIELDS[f]} AS {f}' for f in UNKNOWN)}) u"
MARK = (
    "UPDATE test_runs t SET summary=t.summary||jsonb_build_object('artifacts',"
    "coalesce(t.summary->'artifacts','{}')||jsonb_build_object('geo',"
    "jsonb_build_object('model',%s::text,'rows',m.rows))) "
    "FROM unnest(%s::bigint[],%s::bigint[]) m(run_key,rows) WHERE t.run_key=m.run_key"
)
STALE = "DELETE FROM calculated_geo_rollups WHERE hour=ANY(%s)"


class Refused(Exception):
    """The database or a stored run does not allow the patch; the open batch is rolled back."""


@dataclass(frozen=True)
class Run:
    run_key: int
    provider: str
    country: str
    patched: bool


@dataclass(frozen=True)
class Group:
    location_id: int | None
    network_id: int | None
    ips: int
    spread: float  # 0 for the all-NULL row; else SPREAD x u, before the run's centring


def groups(run: Run, found: list[tuple], unknown: tuple) -> list[Group]:
    """The run's place groups (its IPs in the requested country, by location and network) and,
    last, the all-NULL row with every other IP."""
    country, places, other = (run.country or "").strip().upper(), [], 0
    for location, network, code, asn, ips in found:
        if location is None or code != country:
            other += ips
            continue
        key = f"{run.provider}:{run.country}:{location}:{'' if asn is None else asn}"
        places.append(Group(location, network, ips, SPREAD * signed_unit(key)))
    return [*places, Group(*unknown, other or (0 if places else 1), 0.0)]


def stride(total: int) -> int:
    """A step near total x PHI and coprime to total: rank k takes position k x step mod total."""
    step = max(1, round(total * PHI))
    while math.gcd(step, total) != 1:
        step += 1
    return step


def spread_bins(steps: list, counts: list, shares: list[int]) -> list[Counter]:
    """Each share's bins {ms: n} of one histogram (steps and counts as stored): its successes
    take consecutive ranks, and the ranks map to positions across the whole histogram, so every
    share samples all of it and the shares' bins add up to the histogram exactly."""
    total, ms, edges = sum(counts), list(accumulate(steps)), list(accumulate(counts))
    step, rank, found = stride(total), 0, []
    for share in shares:
        own = Counter(ms[bisect_right(edges, k * step % total)] for k in range(rank, rank + share))
        found.append(own)
        rank += share
    return found


def split(bucket: dict, weights: list[int]) -> list[dict]:
    """One bucket's counters per group (weights: IP counts, the all-NULL row last): every
    outcome and byte count by largest remainder, proxy errors to the all-NULL row."""
    other = bucket["completed"] - bucket["proxy_errors"]
    other -= sum(bucket[k] for k in ("successes", "challenges", "blocks", "timeouts"))
    if other < 0 or sum(bucket["counts"]) != bucket["successes"]:
        raise Refused(f"run {bucket['run_key']}: its bucket at {bucket['at']} does not add up")
    shares = {k: apportion(bucket[k] if k != "other" else other, weights) for k in SPLIT}
    bins = spread_bins(bucket["steps"], bucket["counts"], shares["successes"])
    rows = []
    for n in range(len(weights)):
        row = {k: shares[k][n] for k in SPLIT} | {"bins": bins[n]}
        outcomes = ("successes", "challenges", "blocks", "timeouts", "other")
        row["completed"] = sum(row[k] for k in outcomes)
        rows.append(row)
    rows[-1]["completed"] += bucket["proxy_errors"]
    return rows


def scaled(bins: Counter, g: float) -> tuple[list[int], list[int]]:
    """The bins at ceil(ms x g), as stored: steps (ms differences) and counts."""
    found = Counter()
    for ms, n in bins.items():
        found[ms if g == 1 else math.ceil(ms * g - 1e-9)] += n
    ms = sorted(found)
    return [b - a for a, b in zip([0, *ms], ms, strict=False)], [found[m] for m in ms]


def merged(buckets: list[dict]) -> list[dict]:
    """The run's buckets as one, at its largest bucket's start (--largest-bucket)."""
    one = dict(max(buckets, key=lambda b: (b["completed"], -b["at"].timestamp())))
    histogram = Counter()
    for b in buckets:
        histogram.update(dict(zip(accumulate(b["steps"]), b["counts"], strict=True)))
    for k in ("completed", "proxy_errors", *SPLIT[:4], *SPLIT[5:]):
        one[k] = sum(b[k] for b in buckets)
    ms = sorted(histogram)
    one["steps"] = [b - a for a, b in zip([0, *ms], ms, strict=False)]
    one["counts"] = [histogram[m] for m in ms]
    return [one]


def run_rows(run: Run, own: list[Group], buckets: list[dict]) -> list[tuple]:
    """The run's run_geo_buckets rows (COLUMNS): each bucket split over its groups, a place
    group's histogram scaled by its g centred on the run's successes; empty rows left out."""
    weights = [g.ips for g in own]
    parts = [(b, split(b, weights)) for b in buckets]
    successes = [sum(rows[n]["successes"] for _, rows in parts) for n in range(len(own))]
    places = range(len(own) - 1)
    weight = sum(successes[n] for n in places)
    mean = sum(successes[n] * own[n].spread for n in places) / weight if weight else 0.0
    found = []
    for bucket, rows in parts:
        for group, row in zip(own, rows, strict=True):
            if not (row["completed"] or row["bytes_up"] or row["bytes_down"]):
                continue
            g = 1.0 if group is own[-1] else math.exp(group.spread - mean)
            steps, counts = scaled(row["bins"], g)
            counters = [row[k] for k in ("completed", "successes", "challenges", "blocks")]
            counters += [row[k] for k in ("timeouts", "bytes_up", "bytes_down")]
            ids = (group.location_id, group.network_id)
            found.append((bucket["at"], run.run_key, *ids, *counters, steps, counts))
    return found


def unknown_ids(conn) -> tuple:
    """The all-NULL location and network, added (names.resolve()) when missing."""
    joins = names.resolve(conn, UNKNOWN, lambda field: f"u.{field}")
    query = f"SELECT location_id.location_id,network_id.network_id FROM {UNKNOWN} {joins}"
    return conn.execute(query).fetchone()


def bucket(row: tuple) -> dict:
    keys = ("run_key", "at", "completed", "successes", "challenges", "blocks", "timeouts")
    keys += ("proxy_errors", "bytes_up", "bytes_down", "steps", "counts")
    return dict(zip(keys, row, strict=True))


def batch_rows(conn, batch: list[Run], bounds: dict, unknown: tuple, one: bool) -> list[list]:
    """Each run's rows (run_rows()), read in one query per table for the batch."""
    params = bounds | {"runs": [run.run_key for run in batch]}
    found, buckets = {}, {}
    for run_key, *group in conn.execute(GROUPS, params).fetchall():
        found.setdefault(run_key, []).append(group)
    for row in conn.execute(BUCKETS, params).fetchall():
        buckets.setdefault(row[0], []).append(bucket(row))
    rows = []
    for run in batch:
        own = buckets.get(run.run_key, [])
        own = merged(own) if one and own else own
        rows.append(run_rows(run, groups(run, found.get(run.run_key, []), unknown), own))
    return rows


def write(conn, batch: list[Run], rows: list[list]) -> list[datetime]:
    """COPY the batch's rows, mark its runs (artifacts.geo) and delete the geo rollup marks of
    the hours written; returns those hours."""
    flat = [row for own in rows for row in own]
    for day in sorted({row[0].replace(hour=0, minute=0, second=0, microsecond=0) for row in flat}):
        partitions.partition(conn, "run_geo_buckets", day)
    with conn.cursor().copy(f"COPY run_geo_buckets ({COLUMNS}) FROM STDIN") as copy:
        for row in flat:
            copy.write_row(row)
    keys, counts = [run.run_key for run in batch], [len(own) for own in rows]
    conn.execute(MARK, (MODEL, keys, counts))
    hours = sorted({row[0].replace(minute=0, second=0, microsecond=0) for row in flat})
    if conn.execute("SELECT to_regclass('calculated_geo_rollups')").fetchone()[0]:
        conn.execute(STALE, (hours,))
    return hours


def patch(args) -> dict:
    """Patch the daily tests of [--from, --to) a batch per transaction; the report."""
    started, lo, hi = time.monotonic(), args.since, args.until
    bounds = {"lo": lo, "hi": hi, "end": hi + MARGIN, "marker": MARKER, "daily": DAILY}
    with connect(args.database) as conn:
        if found := target_refusal(conn, args.owner_go, args.i_know_this_is_production):
            raise Refused(found)
        stored = [Run(*row) for row in conn.execute(RUNS, bounds).fetchall()]
        todo = [run for run in stored if not run.patched]
        unknown = (None, None) if args.dry_run else unknown_ids(conn)
        conn.commit()
        per_run, hours = [], set()
        for n in range(0, len(todo), BATCH):
            batch = todo[n : n + BATCH]
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))  # no calculation reads
            rows = batch_rows(conn, batch, bounds, unknown, args.largest_bucket)
            per_run += [len(own) for own in rows]
            if args.dry_run:
                conn.rollback()
                continue
            hours.update(write(conn, batch, rows))
            conn.commit()
    return report(args, stored, per_run, len(hours), time.monotonic() - started)


def report(args, stored: list[Run], per_run: list[int], hours: int, seconds: float) -> dict:
    rows = sum(per_run)
    return {
        "model": MODEL,
        "dry_run": args.dry_run,
        "largest_bucket": args.largest_bucket,
        "from": args.since.isoformat(),
        "to": args.until.isoformat(),
        "daily_runs": len(stored),
        "skipped": sum(run.patched for run in stored),  # already patched: nothing written
        "patched": len(per_run),
        "rows": rows,
        "rows_per_run": {"mean": round(rows / len(per_run), 1) if per_run else None}
        | {"max": max(per_run, default=None)},
        "rollup_hours_reset": hours,
        "minutes": round(seconds / 60, 2),
    }


def moment(value: str) -> datetime:
    found = datetime.fromisoformat(value)
    if found.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{value!r} has no time zone")
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", required=True, help="credentials TOML of the database")
    parser.add_argument("--from", dest="since", required=True, type=moment, help="ISO, UTC")
    parser.add_argument("--to", dest="until", required=True, type=moment, help="ISO, UTC")
    parser.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    parser.add_argument("--largest-bucket", action="store_true", help="one bucket per run")
    parser.add_argument("--owner-go", help=f"production only: {PRODUCTION_GO_FORM}")
    parser.add_argument(ACKNOWLEDGE, action="store_true", help="the database is production")
    args = parser.parse_args(argv)
    if args.until <= args.since:
        parser.error("--to must be after --from")
    try:
        found = patch(args)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(found, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
