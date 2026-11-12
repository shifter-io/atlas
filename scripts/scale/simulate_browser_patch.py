#!/usr/bin/env python3
"""BROWSER-SPREAD-1 (#875): the stored browser runs of the test days rewritten to each network's
own outcome mix (simulate_browser.mix(), test data, model per-network-v1).

Usage:
  simulate_browser_patch.py --database <toml> --profile <profile.json> --from <ISO> --to <ISO>
      [--browser-settings <toml>] [--bases <json>] [--dry-run]
      [--owner-go "owner-go PRODUCTION-AS-STAGING <YYYY-MM-DD>" --i-know-this-is-production]
      (production: with PROXY_INDEX_ALLOW_PRODUCTION_TARGET=1 in the environment)

For every browser run (batch kind browser) that started in [--from, --to), its completed tries
are split over the mix of its network, target and engine (the settings' engine of the target,
as the generator plans it) by largest remainder (SIMULATE.md, "Browser runs"). Its one bucket
gets the new successes, challenges, blocks, timeouts, proxy errors and error codes, and its page
load histogram is scaled to the new successes; the run's summary and its batch run row get the
same counters and the summary the mark browser_model per-network-v1. Tries, bytes, status codes
and page loads per try are unchanged. A marked run is left as it is, so a second run writes
nothing. BROWSER-AI-SPREAD-1 (#887): --bases names a private JSON file (target -> engine -> the
owner's real success share, never committed) that mix() centres those targets on; their runs
are rewritten again, marked browser_bases with the file's digest, unless they have it. The
copies of the patched hours' batch-run buckets (calculated_pulse_buckets) and their marks are
deleted, so the next calculation copies them again. One transaction under the
calculation's advisory lock. --dry-run writes nothing and prints the same report.
Production's proxy_reviews needs #817's three (daily/audits.py target_refusal).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from itertools import accumulate
from pathlib import Path

from psycopg.types.json import Jsonb
from simulate_browser import CODES, MODEL, _loads, base, mix, page_load
from simulate_model import BROWSER_SETTINGS
from synthetic import apportion

from proxy_index.browser.settings import load_settings
from proxy_index.calculated.rollups import LOCK
from proxy_index.daily.audits import target_refusal
from proxy_index.production_guard import ACKNOWLEDGE, PRODUCTION_GO_FORM
from proxy_index.stats import Histogram
from proxy_index.storage import connect

MARGIN = timedelta(days=1)  # a run's bucket starts at most this long after the run
RUNS = (
    "SELECT r.run_key,p.provider,s.target,r.summary->>'browser_model' IS NOT NULL,"
    "r.summary->>'browser_bases' "
    "FROM test_runs r JOIN test_batches b ON b.batch_id=r.batch_id "
    "JOIN scope_stats s ON s.scope_id=r.scope_id JOIN products p ON p.product_id=s.product_id "
    "WHERE b.kind='browser' AND r.started_at>=%(lo)s AND r.started_at<%(hi)s ORDER BY 1"
)
BUCKETS = (
    "SELECT run_key,bucket_start,completed,successes,challenges,blocks,timeouts,proxy_errors,"
    "errors,histogram_steps,histogram_counts,outcome_counters FROM run_buckets "
    "WHERE run_key=ANY(%(runs)s) AND bucket_start>=%(lo)s AND bucket_start<%(end)s ORDER BY 1,2"
)
# The typed counters of a bucket, a summary and a batch run, by mix() outcome.
TYPED = {"challenge": "challenges", "block": "blocks", "timeout": "timeouts"}
TYPED |= {"proxy_error": "proxy_errors"}
BUCKET = (
    "UPDATE run_buckets SET successes=%s,challenges=%s,blocks=%s,timeouts=%s,proxy_errors=%s,"
    "errors=%s,histogram_steps=%s,histogram_counts=%s WHERE run_key=%s AND bucket_start=%s"
)
SUMMARY = "UPDATE test_runs SET summary=summary||%s WHERE run_key=%s"
BATCH_RUN = (
    "UPDATE batch_runs SET successes=%s,challenges=%s,blocks=%s,timeouts=%s,proxy_errors=%s "
    "WHERE run_key=%s"
)
STALE = ("calculated_pulse_buckets", "calculated_pulse_rollups")


class Refused(Exception):
    """The database or a stored run does not allow the patch; nothing is written."""


def counts(weights: dict[str, int], completed: int) -> dict[str, int]:
    """The completed tries per outcome: the mix's weights by largest remainder."""
    return dict(zip(weights, apportion(completed, list(weights.values())), strict=True))


def scaled(steps: list, counts_: list, successes: int, rng: random.Random, loads) -> tuple:
    """(steps, counts) of a page-load histogram with `successes` loads: the stored one's counts
    by largest remainder, or, when it holds none, loads (p50, p90) drawn as the generator does."""
    if not successes:
        return [], []
    if sum(counts_):
        bins = dict(zip(accumulate(steps), apportion(successes, counts_), strict=True))
    else:
        bins = Counter(_loads(rng, loads[0], loads[1] / loads[0], successes))
    ms = sorted(m for m, n in bins.items() if n)
    return [b - a for a, b in zip([0, *ms], ms, strict=False)], [bins[m] for m in ms]


def rewritten(bucket: dict, weights: dict[str, int], loads: tuple) -> dict:
    """The bucket's new counters: its tries over the mix, its histogram scaled to them."""
    if not bucket["outcome_counters"]:
        raise Refused(f"run {bucket['run_key']}: its bucket has no typed outcome counters")
    found = counts(weights, bucket["completed"])
    rng = random.Random(bucket["run_key"])
    steps, counts_ = scaled(bucket["steps"], bucket["counts"], found["ok"], rng, loads)
    errors = {CODES[name]: n for name, n in found.items() if name != "ok" and n}
    typed = {column: found[name] for name, column in TYPED.items()}
    return {"successes": found["ok"], **typed, "errors": errors, "steps": steps, "counts": counts_}


def summary(new: dict, completed: int) -> dict:
    """The run summary's counters of its one bucket's new counters, and the mark."""
    histogram = Histogram(Counter(dict(zip(accumulate(new["steps"]), new["counts"], strict=True))))
    found = {k: new[k] for k in ("successes", *TYPED.values(), "errors")}
    found |= {"total_errors": sum(new["errors"].values())}
    found |= {"success_rate": new["successes"] / completed if completed else 0.0}
    found |= {"latency_ms": histogram.percentiles(), "latency_mean_ms": histogram.mean()}
    found |= {"browser_bases": new["bases"]} if new.get("bases") else {}
    return found | {"browser_model": MODEL}


def read_bases(path: Path) -> tuple[dict, str]:
    """(target -> engine -> real success share, the digest of the file) of --bases."""
    raw = path.read_bytes()
    try:
        found = json.loads(raw)
    except ValueError:
        found = None
    engines = found.values() if isinstance(found, dict) else [None]
    if not all(isinstance(own, dict) and own and all(map(_share, own.values())) for own in engines):
        raise Refused("--bases: a JSON object of target -> engine -> a share in [0, 1]")
    return found, hashlib.sha256(raw).hexdigest()[:12]


def _share(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= 1


def plan(conn, args, stored: list) -> list[tuple]:
    """(run_key, provider, target, bucket, new counters) of each unmarked run, and of each run
    of a --bases target and engine without the file's digest."""
    settings = load_settings(args.browser_settings)
    targets = {row[2] for row in stored}
    centred = {t for t in targets if base(args.profile, t, settings.engine(t)) is not None}
    todo = [r for r in stored if not r[3] or (r[2] in centred and r[4] != args.digest)]
    bounds = {"lo": args.since, "end": args.until + MARGIN, "runs": [row[0] for row in todo]}
    buckets = defaultdict(list)
    keys = ("run_key", "at", "completed", "successes", *TYPED.values(), "errors", "steps")
    for row in conn.execute(BUCKETS, bounds).fetchall():
        buckets[row[0]].append(dict(zip((*keys, "counts", "outcome_counters"), row, strict=True)))
    found = []
    for run_key, provider, target, *_ in todo:
        if len(own := buckets[run_key]) != 1:
            raise Refused(f"run {run_key}: {len(own)} buckets, the patch takes one")
        engine = settings.engine(target)
        weights = mix(args.profile, provider, target, engine)
        new = rewritten(own[0], weights, page_load(args.profile, provider, target, engine))
        new["bases"] = args.digest if target in centred else None
        found.append((run_key, provider, target, own[0], new))
    return found


def touched(found: list) -> list[datetime]:
    """The hours of the planned runs' buckets."""
    return sorted({b["at"].replace(minute=0, second=0, microsecond=0) for *_, b, _ in found})


def write(conn, found: list) -> list[datetime]:
    """The new counters of every planned run; the hours whose bucket copies were deleted."""
    for run_key, _, _, bucket, new in found:
        typed = [new[column] for column in TYPED.values()]
        steps, counts_ = new["steps"], new["counts"]
        values = (new["successes"], *typed, Jsonb(new["errors"]), steps, counts_)
        conn.execute(BUCKET, (*values, run_key, bucket["at"]))
        conn.execute(SUMMARY, (Jsonb(summary(new, bucket["completed"])), run_key))
        conn.execute(BATCH_RUN, (new["successes"], *typed, run_key))
    hours = touched(found)
    for table in STALE:
        if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]:
            conn.execute(f"DELETE FROM {table} WHERE hour=ANY(%s)", (hours,))
    return hours


def patch(args) -> dict:
    """Patch the browser runs of [--from, --to) in one transaction; the report."""
    with connect(args.database) as conn:
        if found := target_refusal(conn, args.owner_go, args.i_know_this_is_production):
            raise Refused(found)
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))  # no calculation reads
        stored = conn.execute(RUNS, {"lo": args.since, "hi": args.until}).fetchall()
        found = plan(conn, args, stored)
        if args.dry_run:
            conn.rollback()
            hours = touched(found)
        else:
            hours = write(conn, found)
            conn.commit()
    return report(args, stored, found, len(hours))


def report(args, stored: list, found: list, hours: int) -> dict:
    """The counts, and per target the lowest and highest network success share before and
    after (over the patched runs)."""
    shares = defaultdict(lambda: [Counter(), Counter(), Counter()])
    for _, provider, target, bucket, new in found:
        own = shares[target]
        own[0][provider] += bucket["completed"]
        own[1][provider] += bucket["successes"]
        own[2][provider] += new["successes"]
    spread = {}
    for target, (tries, before, after) in sorted(shares.items()):
        spread[target] = {
            name: [round(min(rates), 3), round(max(rates), 3)]
            for name, own in (("before", before), ("after", after))
            if (rates := [own[p] / tries[p] for p in tries if tries[p]])
        }
    return {
        "model": MODEL,
        "dry_run": args.dry_run,
        "from": args.since.isoformat(),
        "to": args.until.isoformat(),
        "browser_runs": len(stored),
        "skipped": len(stored) - len(found),  # already patched: nothing written
        "patched": len(found),
        "hours_reset": hours,
        "success_share": spread,
    }


def moment(value: str) -> datetime:
    found = datetime.fromisoformat(value)
    if found.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{value!r} has no time zone")
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", required=True, help="credentials TOML of the database")
    parser.add_argument("--profile", required=True, type=Path, help="the load's profile.json")
    parser.add_argument("--browser-settings", type=Path, default=BROWSER_SETTINGS)
    parser.add_argument("--from", dest="since", required=True, type=moment, help="ISO, UTC")
    parser.add_argument("--to", dest="until", required=True, type=moment, help="ISO, UTC")
    parser.add_argument("--bases", type=Path, help="private JSON: target -> engine -> share")
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    parser.add_argument("--owner-go", help=f"production only: {PRODUCTION_GO_FORM}")
    parser.add_argument(ACKNOWLEDGE, action="store_true", help="the database is production")
    args = parser.parse_args(argv)
    if args.until <= args.since:
        parser.error("--to must be after --from")
    args.profile, args.digest = json.loads(args.profile.read_text()), None
    try:
        if args.bases:
            args.profile["browser_bases"], args.digest = read_bases(args.bases)
        found = patch(args)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(found, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
