#!/usr/bin/env python3
"""Offline preview of SUCCESS-MIN-IPS-1 (#1214): each network's success score and rank with the
rule off and on, from the stored data, in one read-only transaction. Nothing is written.

    python scripts/success_min_ips_preview.py --database-file <toml> [--minimum 500]

It takes the published build's cutoff and recounts, with the calculator's own slot code
(underload.time_slots(), slot_rate()), the time-slot success rate of each network's HTTP targets
in its 24h, 7d and 30d windows: once as published, once without the requests of countries whose
verdict found fewer than --minimum unique IPs. The HTTP success scores are ranked again
(target_scores.metric_scores()); the browser targets keep their published scores and weights,
so each network's success score and rank are the published overall with only the HTTP part
changed. "published" is the build's own success score, to check the "off" recount against.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atlas import verdicts  # noqa: E402
from atlas.calculated import network, scheduled, target_scores  # noqa: E402
from atlas.calculated.aggregate import floor_time  # noqa: E402
from atlas.calculated.policy import HTTP_DIFFICULTY  # noqa: E402
from atlas.calculated.underload import slot_rate, time_slots  # noqa: E402
from atlas.calculated.window_scoring import slot_length  # noqa: E402

WINDOWS = {"24h": 24, "7d": 168, "30d": 720}
HTTP = tuple(HTTP_DIFFICULTY)
PUBLISHED = (
    "SELECT n->>'id' AS id,(n->>'product_id')::int AS product_id,n->>'name' AS name,"
    "n->>'kind' AS kind,w.key AS label,w.value->'success_overall' AS overall "
    "FROM calculated_snapshots s JOIN current_calculation USING(build_id),"
    "jsonb_array_elements(s.payload->'networks') n,jsonb_each(n->'windows') w"
)
AS_OF = "SELECT b.as_of FROM current_calculation JOIN calculation_builds b USING(build_id)"
# The window's HTTP runs that counted, with their verdict's unique IPs, and their requests per
# slot of the bucket's start and window class (0: 24h, 1: 7d, 2: 30d).
RUNS = (
    f"{verdicts.WITH_DAYS}SELECT r.run_key,s.product_id,s.target,b.kind AS test,b.scheduled_for "
    "AS slot_at,cv.unique_ips AS verdict_ips FROM test_runs r JOIN scope_stats s USING(scope_id) "
    "JOIN pool_stats ps ON ps.pool_id=s.pool_id LEFT JOIN test_batches b ON b.batch_id=r.batch_id "
    f"{verdicts.counted('s.product_id', scheduled.COUNTRY, 'r.started_at')}"
    "WHERE cv.day IS NOT NULL AND s.target=ANY(%(http)s) AND r.run_key IN "
    "(SELECT run_key FROM run_buckets WHERE bucket_start>=%(start)s AND bucket_start<%(end)s)"
)
BUCKETS = (
    "SELECT run_key,to_timestamp(floor(extract(epoch FROM bucket_start)/%(secs)s)*%(secs)s) AS "
    "slot,CASE WHEN bucket_start>=%(d1)s THEN 0 WHEN bucket_start>=%(d7)s THEN 1 ELSE 2 END AS "
    "class,sum(completed)::bigint AS completed,sum(successes)::bigint AS successes "
    "FROM run_buckets WHERE bucket_start>=%(start)s AND bucket_start<%(end)s GROUP BY 1,2,3"
)


def read(conn):
    """The cutoff, the published overalls, slot seconds, the bucket rows and pulse runs."""
    as_of = conn.execute(AS_OF).fetchone()["as_of"]
    start = as_of - timedelta(hours=720)
    bounds = {"start": start, "end": as_of, "margin": start - timedelta(days=1)}
    bounds |= {"d1": as_of - timedelta(hours=24), "d7": as_of - timedelta(hours=168)}
    bounds |= verdicts.bounds(bounds["margin"], as_of) | {"http": list(HTTP)}
    secs = slot_length(scheduled.pulse_minutes(conn, start, as_of)) * 60
    published = conn.execute(PUBLISHED).fetchall()
    runs = {r["run_key"]: r for r in conn.execute(RUNS, bounds)}
    overlaps = scheduled.overlap_runs(conn, bounds)
    flagged_runs = {o["run_key"] for o in overlaps if o["run_key"] is not None}
    flagged = {(o["batch_id"], o["product_id"]): o["census"] for o in overlaps}
    rows = []
    for b in conn.execute(BUCKETS, bounds | {"secs": secs}):
        r = runs.get(b["run_key"])
        if r is not None:
            pulse = r["test"] in ("pulse", "load") and r["slot_at"] is not None
            slot = floor_time(r["slot_at"], secs) if pulse else b["slot"]
            rows.append(r | b | {"slot": slot, "during_census": r["run_key"] in flagged_runs})
    pulses = [
        d | {"product_id": p["product_id"], "verdict_ips": p["verdict_ips"]}
        for p in scheduled.pulse_runs(conn, start, as_of, True)
        for d in network.slot_runs([p], secs, flagged)
        if p["target_id"] in HTTP
    ]
    return as_of, published, rows, pulses


def rate(rows, pulses, minimum):
    """The time-slot success rate of rows and pulses, leaving out (minimum) the few-IP ones."""
    for r in [*rows, *pulses]:
        r["few_ips"] = verdicts.few_ips(r.get("verdict_ips"), minimum)
    slots = time_slots(rows, pulses)
    votes = [v for v in map(slot_rate, slots.values()) if v is not None]
    return sum(votes) / len(votes) if votes else None


def http_rates(rows, pulses, as_of, minimum):
    """{(label, product_id, target): (rate, completed)} of every network's HTTP targets."""
    found = {}
    groups, runs = defaultdict(list), defaultdict(list)
    for r in rows:
        groups[r["product_id"], r["target"]].append(r)
    for d in pulses:
        runs[d["product_id"], d["target"]].append(d)
    for label, hours in WINDOWS.items():
        since, top = as_of - timedelta(hours=hours), list(WINDOWS).index(label)
        for key in set(groups) | set(runs):
            own = [{**r, "counters": counters(r)} for r in groups[key] if r["class"] <= top]
            slots = [d for d in runs[key] if since <= d["scheduled_for"] < as_of]
            done = sum(r["completed"] for r in own)
            found[(label, *key)] = (rate(own, slots, minimum), done)
    return found


def counters(r):
    """A bucket row's counters as time_slots() reads them (no latency here)."""
    bins = {"bins": ()}
    return {"completed": r["completed"], "successes": r["successes"], "latency_histogram": bins}


def scores(published, rates):
    """{(label, network id): (score, rank, published score, published rank)}: each network's
    success overall with its HTTP targets ranked from rates, its browser targets as published."""
    kinds = defaultdict(list)
    for n in published:
        kinds[n["label"], n["kind"]].append(n)
    found = {}
    for (label, _), peers in kinds.items():
        http = {}
        for target in HTTP:
            rated = [rates.get((label, n["product_id"], target), (None, 0)) for n in peers]
            for n, s in zip(peers, target_scores.metric_scores(rated, True), strict=True):
                if s["status"] == target_scores.RANKED:
                    http[n["id"], target] = s["score"]
        values = {n["id"]: overall(n, http) for n in peers}
        ranked = [k for k, v in values.items() if v is not None]
        found_ranks = target_scores.average_ranks([values[k] for k in ranked], True)
        ranks = dict(zip(ranked, found_ranks, strict=True))
        for n in peers:
            old = n["overall"] or {}
            found[label, n["id"]] = (values[n["id"]], ranks.get(n["id"]), old.get("score"))
            found[label, n["id"]] += (old.get("rank"),)
    return found


def overall(n, http):
    """The difficulty-weighted mean of a network's ranked HTTP scores (http) and its published
    browser target scores (the published overall's targets and weights)."""
    old = n["overall"] or {}
    weights = {t: w for t, w in (old.get("weights") or {}).items() if t not in HTTP}
    total = {t: old["targets"][t] for t in weights}
    for target in HTTP:
        if (n["id"], target) in http:
            weights[target], total[target] = HTTP_DIFFICULTY[target], http[n["id"], target]
    used = sum(weights.values())
    return sum(total[t] * weights[t] for t in weights) / used if used else None


def table(published, rates, minimum):
    """The preview as Markdown: per window, each network's generic_http success rate, success
    score and rank with the rule off and on."""
    off, on = scores(published, rates[0]), scores(published, rates[minimum])
    head = "| Network | Kind | generic_http success off → on | Score off → on (published) "
    head += "| Rank off → on (published) |\n|---|---|---|---|---|"
    lines = []
    for label in WINDOWS:
        lines += [f"\n### {label}\n", head]
        peers = sorted((n for n in published if n["label"] == label), key=lambda n: n["name"])
        for n in peers:
            key = (label, n["product_id"], "generic_http")
            a, b = (r.get(key, (None,))[0] for r in (rates[0], rates[minimum]))
            (s0, r0, ps, pr), (s1, r1, *_) = off[label, n["id"]], on[label, n["id"]]
            lines.append(
                f"| {n['name']} | {n['kind']} | {pct(a)} → {pct(b)} | {num(s0)} → {num(s1)} "
                f"({num(ps)}) | {num(r0)} → {num(r1)} ({num(pr)}) |"
            )
    return "\n".join(lines)


def pct(value):
    return "n/a" if value is None else f"{100 * value:.2f}%"


def num(value):
    return "n/a" if value is None else f"{value:.2f}".rstrip("0").rstrip(".")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-file", type=Path, required=True)
    parser.add_argument("--minimum", type=int, default=500)
    args = parser.parse_args(argv)
    with args.database_file.open("rb") as handle:
        credentials = tomllib.load(handle)["postgres"]
    with psycopg.connect(**credentials, row_factory=dict_row) as conn:
        conn.read_only = True
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        as_of, published, rows, pulses = read(conn)
    rates = {m: http_rates(rows, pulses, as_of, m) for m in (0, args.minimum)}
    print(f"Cutoff {as_of.isoformat()}; success_min_country_ips off → {args.minimum}.")
    print(table(published, rates, args.minimum))
    return 0


if __name__ == "__main__":
    sys.exit(main())
