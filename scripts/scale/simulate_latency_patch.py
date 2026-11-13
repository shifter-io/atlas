#!/usr/bin/env python3
"""LATENCY-TRUTH-1 (#873): the stored test days' latency from each network's real
successes-only median (test data, model real-median-v1).

Usage:
  simulate_latency_patch.py --database <toml> --state <the load's state.json> --medians <json>
      [--dry-run]
      [--owner-go "owner-go PRODUCTION-AS-STAGING <YYYY-MM-DD>" --i-know-this-is-production]
      (production: with ATLAS_ALLOW_PRODUCTION_TARGET=1 in the environment)

The generated days drew each network's latency from a hash of its name (simulate_model.Model.bins
before #873), so its light-load and under-load order was chance. This rescales the stored runs of
the load (state.json: its replay_id, seed, first day and days) so it follows the real medians
(--medians: network -> ms, simulate_real.read_medians; read where it is, never copied). For each
network, one transaction (SIMULATE.md, "Real latency medians on the stored days"):
- s = its real median / its current one: the p50 of its stored pulses of the latency target
  (generic_http; its daily tests' when it has no pulse), which the light-load latency is. A
  network the file does not name keeps s = 1 (the report lists it);
- every latency bin of its daily tests (run_buckets and #870's run_geo_buckets) and its pulses
  moves to ceil(ms x s); a generic_https pulse's to ceil(ms x s x h), its HTTPS overhead
  h = 1.1 + 0.3 u, u in [0, 1] from md5(network:https) (#874, https_overhead()); a load run's
  to ceil(ms x s x new / old) when score-inputs-1 made it: its load factor of the day replaced by
  simulate_scored.load_factor()'s (1.4 +- 0.05) from score-inputs-1's (previous_load_factor());
  a later version's load run already has the new one (s only).
  Bins that meet merge; every count is kept, so successes, rates and every other figure stay;
- each run's summary gets latency_ms and latency_mean_ms of its new bins and the mark
  latency_model: real-median-v1 (and an HTTPS pulse's https_overhead: h); the latency figures
  of its pulse events' data (p50, a latency spike's baseline and value, a pulse's previous p50)
  are scaled as its pulses' bins, so the tape keeps its events;
- the rollup marks of every hour of the days are deleted, so the next calculation counts them
  again; the mark changes each run's list signature and the build's fingerprint
  (lists.patches()).
A network whose runs all have the mark is skipped, so a second run writes nothing; one with some
marked is refused. --dry-run writes nothing and prints the factors and the rows it would change.
Production's proxy_reviews needs #817's three (daily/audits.py target_refusal).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from pathlib import Path

from simulate_real import read_medians
from simulate_scored import load_factor
from synthetic import signed_unit, unit

from atlas.calculated.policy import LATENCY_TARGET
from atlas.calculated.rollups import LOCK
from atlas.daily.audits import target_refusal
from atlas.production_guard import ACKNOWLEDGE, PRODUCTION_GO_FORM
from atlas.storage import connect

MODEL = "real-median-v1"
HTTPS = "generic_https"  # its pulses also get the network's HTTPS overhead (#874)
OLD = "score-inputs-1"  # its load runs have previous_load_factor()
PREVIOUS = (1.1, 1.8)  # score-inputs-1's load factors, per network (simulate_scored before #873)
MARGIN = timedelta(days=1)  # a run's buckets end at most this long after its start
RUNS = (
    "SELECT r.run_key,p.provider,coalesce(b.kind,'daily'),s.target,"
    "coalesce(b.scheduled_for,r.started_at),r.summary ? 'latency_model',"
    "r.summary->>'score_inputs' FROM test_runs r "
    "JOIN scope_stats s ON s.scope_id=r.scope_id JOIN products p ON p.product_id=s.product_id "
    "LEFT JOIN test_batches b ON b.batch_id=r.batch_id "
    "WHERE r.summary->>'scale_replay'=%(replay)s AND r.started_at>=%(lo)s AND r.started_at<%(hi)s "
    "AND (r.batch_id IS NULL AND r.summary->>'test_kind'='daily' OR b.kind IN ('pulse','load')) "
    "ORDER BY r.run_key"
)
BUCKETS = "b.run_key=ANY(%(runs)s) AND b.bucket_start>=%(lo)s AND b.bucket_start<%(end)s"
# The p50 of the runs' merged histograms (stats.Histogram.percentiles: the ceil(n / 2)th).
MEDIAN = (
    "WITH h AS (SELECT x.ms,sum(x.n) AS n FROM run_buckets b CROSS JOIN LATERAL "
    f"histogram(b.histogram_steps,b.histogram_counts::bigint[]) x WHERE {BUCKETS} GROUP BY 1) "
    "SELECT min(ms) FROM (SELECT ms,sum(n) OVER (ORDER BY ms) AS seen,sum(n) OVER () AS total "
    "FROM h) c WHERE seen>=ceil(total*0.5)"
)
SCALES = (
    "CREATE TEMP TABLE latency_scales(run_key bigint PRIMARY KEY,scale float8 NOT NULL,"
    "https float8) "
    "ON COMMIT DROP"
)
# A row's bins at ceil(ms x scale), as stored: steps (ms differences) and counts.
SCALED = (
    "(SELECT coalesce(array_agg(ms-prev ORDER BY ms),'{{}}'),coalesce(array_agg(n ORDER BY ms),"
    "'{{}}') FROM (SELECT ms,n,coalesce(lag(ms) OVER (ORDER BY ms),0) AS prev FROM (SELECT "
    "ceil(x.ms*m.scale-1e-9)::integer AS ms,sum(x.n)::integer AS n FROM histogram("
    "{t}.histogram_steps,{t}.histogram_counts::bigint[]) x GROUP BY 1) y) z)"
)
SPAN = "m.scale<>1 AND {t}.bucket_start>=%(lo)s AND {t}.bucket_start<%(end)s"
UPDATE = (
    "UPDATE {table} {t} SET (histogram_steps,histogram_counts)=" + SCALED + " "
    "FROM latency_scales m WHERE {t}.run_key=m.run_key AND " + SPAN
)
COUNT = "SELECT count(*) FROM {table} {t} JOIN latency_scales m USING(run_key) WHERE " + SPAN
NUMBER = "jsonb_typeof(v)='number'"
SUMMARY = (
    "UPDATE test_runs t SET summary=t.summary||jsonb_build_object('latency_model',%(model)s::text)"
    "||CASE WHEN m.https IS NULL THEN '{}'::jsonb "
    "ELSE jsonb_build_object('https_overhead',m.https) END"
    "||CASE WHEN m.scale=1 OR jsonb_typeof(t.summary->'latency_ms')<>'object' THEN '{}'::jsonb "
    "ELSE jsonb_build_object('latency_ms',(SELECT jsonb_object_agg(k,CASE WHEN "
    f"{NUMBER} THEN to_jsonb(ceil(v::text::float8*m.scale-1e-9)::bigint) ELSE v END) FROM "
    "jsonb_each(t.summary->'latency_ms') e(k,v)),'latency_mean_ms',(SELECT round(sum(x.ms*x.n)"
    "::numeric/nullif(sum(x.n),0),1) FROM run_buckets b CROSS JOIN LATERAL histogram("
    "b.histogram_steps,b.histogram_counts::bigint[]) x WHERE b.run_key=t.run_key "
    "AND b.bucket_start>=%(lo)s AND b.bucket_start<%(end)s)) END "
    "FROM latency_scales m WHERE t.run_key=m.run_key"
)
EVENTS = (
    "SELECT e.event_id,e.at,e.kind,e.data,b.target_id FROM test_events e "
    "JOIN test_batches b USING(batch_id) "
    "JOIN products p ON p.product_id=e.product_id WHERE b.kind='pulse' AND p.provider=%(network)s "
    "AND b.scheduled_for>=%(lo)s AND b.scheduled_for<%(hi)s AND e.data ? 'p50'"
)
SET_EVENTS = (
    "UPDATE test_events e SET data=v.data FROM unnest(%s::bigint[],%s::timestamptz[],%s::jsonb[]) "
    "v(event_id,at,data) WHERE e.event_id=v.event_id AND e.at=v.at"
)
TABLES = (("buckets", "run_buckets", "b"), ("geo_rows", "run_geo_buckets", "g"))
MARKS = ("calculated_geo_rollups", "calculated_pulse_rollups")


class Refused(Exception):
    """The database, the state or the medians do not allow the patch; nothing is written."""


@dataclass(frozen=True)
class Run:
    run_key: int
    network: str
    kind: str  # daily, pulse or load
    target: str
    at: datetime  # its batch's slot; a daily test's start
    patched: bool
    version: str | None = None  # the score inputs' version (simulate_scored.VERSION)


@dataclass(frozen=True)
class Load:
    """The patched load (its state.json) and its span: runs that started in [lo, hi)."""

    replay: str
    seed: str
    first: date
    lo: datetime
    hi: datetime

    @classmethod
    def read(cls, path) -> Load:
        try:
            state = json.loads(Path(path).read_text())
            first = date.fromisoformat(state["first_day"])
            lo = datetime.combine(first, clock(), UTC)
            given = (state["replay_id"], state["seed"], first, lo)
            return cls(*given, lo + timedelta(days=int(state["days"])))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise Refused(f"--state: {exc!r}") from None

    def day(self, at: datetime) -> int:
        return (at.astimezone(UTC).date() - self.first).days + 1

    def bounds(self, **more) -> dict:
        return {"lo": self.lo, "hi": self.hi, "end": self.hi + MARGIN, "replay": self.replay} | more


def previous_load_factor(seed, network: str, day: int) -> float:
    """score-inputs-1's load factor (simulate_scored.load_factor before #873): per network within
    PREVIOUS, moved by up to 0.2 on each day."""
    low, high = PREVIOUS
    found = low + (high - low) * unit(seed, "load", network)
    return min(high, max(low, found + 0.2 * (2 * unit(seed, "load", network, day) - 1)))


def https_overhead(network: str) -> float:
    """The network's HTTPS overhead (#874): 1.1 + 0.3 u, u in [0, 1] from md5(network:https)."""
    return 1.1 + 0.3 * (signed_unit(f"{network}:https") + 1) / 2


def scales(load: Load, runs: list[Run], s: float) -> dict[int, tuple[float, float | None]]:
    """Each run's (scale, HTTPS overhead): s; an HTTPS pulse's s x h, with h; a load run of
    score-inputs-1 s x its new load factor / its old one."""
    found = {}
    for run in runs:
        found[run.run_key] = (s, None)
        if run.kind == "pulse" and run.target == HTTPS:
            h = https_overhead(run.network)
            found[run.run_key] = (s * h, h)
        elif run.kind == "load" and run.version == OLD:
            day = load.day(run.at)
            new = load_factor(load.seed, run.network, day)
            found[run.run_key] = (s * new / previous_load_factor(load.seed, run.network, day), None)
    return found


def scaled_ms(value, s: float):
    """A latency figure (ms) at ceil(ms x s), as its bins move; anything else as it is."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return value
    return math.ceil(value * s - 1e-9)


def scaled_event(kind: str, data: dict, s: float) -> dict:
    """A pulse event's data with its latency figures scaled: p50; a latency spike's baseline
    and value (both p50s); a pulse's previous p50. Success and block figures stay."""
    keys = ("p50", "baseline", "value") if kind == "latency_spike" else ("p50",)
    found = data | {k: scaled_ms(data[k], s) for k in keys if k in data}
    if isinstance(data.get("previous"), dict) and "p50" in data["previous"]:
        found["previous"] = data["previous"] | {"p50": scaled_ms(data["previous"]["p50"], s)}
    return found


def reference(conn, load: Load, runs: list[Run]) -> int | None:
    """The network's current median: the p50 of its stored pulses of the latency target, or of
    its daily tests when it has none; None without a success."""
    for kind in ("pulse", "daily"):
        keys = [r.run_key for r in runs if r.kind == kind and r.target == LATENCY_TARGET]
        if keys:
            found = conn.execute(MEDIAN, load.bounds(runs=keys)).fetchone()[0]
            if found is not None:
                return int(found)
    return None


class Patch:
    """The patch of one load's networks on an open connection (dry: count, write nothing)."""

    def __init__(self, conn, load: Load, dry: bool):
        self.conn, self.load, self.dry = conn, load, dry

    def network(self, runs: list[Run], real: float | None) -> dict:
        """One network in one transaction under the calculation's lock: its factors and counts."""
        load, network, marked = self.load, runs[0].network, sum(run.patched for run in runs)
        if marked == len(runs):
            return {"skipped": True, "runs": len(runs)}
        if marked:
            raise Refused(f"{network}: {marked} of its {len(runs)} runs are patched already")
        self.conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))
        current = reference(self.conn, load, runs)
        s = real / current if real and current else 1.0
        factors = {
            day: [round(f(load.seed, network, day), 4) for f in (previous_load_factor, load_factor)]
            for day in sorted({load.day(run.at) for run in runs if run.kind == "load"})
        }
        counts = self.write(network, scales(load, runs, s), s)
        if self.dry:
            self.conn.rollback()
        else:
            self.conn.commit()
        found = {"real_median": real, "current_median": current, "scale": round(s, 6)}
        return found | {"load_factors": factors, "skipped": False} | counts

    def write(self, network: str, found: dict, s: float) -> dict:
        """Scale the runs' bins (found: scales()), summaries and pulse events (s, an HTTPS
        pulse's s x h); the counts. The rollup marks of every hour of the days are deleted."""
        conn, params = self.conn, self.load.bounds(model=MODEL)
        conn.execute(SCALES)
        with conn.cursor().copy("COPY latency_scales (run_key,scale,https) FROM STDIN") as copy:
            for run_key, (scale, h) in found.items():
                copy.write_row((run_key, scale, h))
        counts = {"runs": len(found)}
        for name, table, t in TABLES:
            cur = conn.execute((COUNT if self.dry else UPDATE).format(table=table, t=t), params)
            counts[name] = cur.fetchone()[0] if self.dry else cur.rowcount
        rows = []
        for i, at, kind, data, target in conn.execute(EVENTS, self.load.bounds(network=network)):
            own = s * https_overhead(network) if target == HTTPS else s
            if own != 1:
                rows.append((i, at, json.dumps(scaled_event(kind, data, own))))
        counts["events"] = len(rows)
        if self.dry:
            return counts
        conn.execute(SUMMARY, params)
        if rows:
            conn.execute(SET_EVENTS, [list(column) for column in zip(*rows, strict=True)])
        for table in MARKS:
            if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]:
                conn.execute(f"DELETE FROM {table} WHERE hour>=%(lo)s AND hour<%(end)s", params)
        return counts


def patch(args) -> dict:
    """Every network of the load, one transaction each; the report."""
    started, load = time.monotonic(), Load.read(args.state)
    with connect(args.database) as conn:
        if found := target_refusal(conn, args.owner_go, args.i_know_this_is_production):
            raise Refused(found)
        runs = [Run(*row) for row in conn.execute(RUNS, load.bounds()).fetchall()]
        conn.commit()
        networks = sorted({run.network for run in runs})
        try:
            medians = read_medians(args.medians, networks)
        except (OSError, ValueError) as exc:
            raise Refused(f"--medians: {exc!r}") from None
        job, report = Patch(conn, load, args.dry_run), {}
        for network in networks:
            own = [run for run in runs if run.network == network]
            report[network] = job.network(own, medians.get(network))
    return {
        "model": MODEL,
        "dry_run": args.dry_run,
        "replay_id": load.replay,
        "from": load.lo.isoformat(),
        "to": load.hi.isoformat(),
        "networks": report,
        "without_real_median": sorted(set(networks) - set(medians)),
        "minutes": round((time.monotonic() - started) / 60, 2),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", required=True, help="credentials TOML of the database")
    parser.add_argument("--state", required=True, help="the load's state.json (read only)")
    parser.add_argument("--medians", required=True, help="JSON: network -> real median ms")
    parser.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    parser.add_argument("--owner-go", help=f"production only: {PRODUCTION_GO_FORM}")
    parser.add_argument(ACKNOWLEDGE, action="store_true", help="the database is production")
    args = parser.parse_args(argv)
    try:
        found = patch(args)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(found, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
