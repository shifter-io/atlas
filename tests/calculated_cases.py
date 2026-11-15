"""Shared helpers of the calculated-snapshot tests: the shipped methodology, fixed-clock runs,
synthetic build inputs, the benchmark scenario and the typed-row payload check."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from helpers import FakeTransport
from test_storage import ALL_ATTRIBUTES, enrichment, measurement

from atlas.calculated import store
from atlas.calculated.aggregate import pool_lists
from atlas.calculated.calculator import calculate
from atlas.calculated.incremental import Inputs
from atlas.calculated.policy import load
from atlas.models import Outcome
from atlas.storage import connect, ingest

POLICY = Path("configs/methodology.toml")
# A 5-minute bucket edge for runs on fixed clocks (spread()).
EDGE = datetime(2026, 10, 1, 12, 5, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def build_inputs(runs, buckets, observations, evidence, as_of):
    """build()'s Inputs of these rows at as_of: no release and no scheduled inputs."""
    return Inputs(
        runs=runs,
        buckets=buckets,
        source=observations,
        evidence=evidence,
        as_of=as_of,
        release=None,
        scheduled={},
    )


def ready_inputs(products=(1, 2)):
    """Per product one pulse run per target (US, one time slot) and a load run on generic_http
    in the same slot with the same latency (factor 1: latency under load = light-load latency),
    plus product-wide ("*") uptime and price evidence."""
    config, _ = load(POLICY)
    config["minimum_requests"] = 1
    config["minimum_coverage"] = 0
    config["slots"] = {"min_latency_successes": 1}
    # These buckets count no wire bytes, so value keeps the ratio to the cheapest list price
    # (test_value_rank.py scores it by place).
    config["value"] = {"basis": "ratio"}
    now = datetime(2026, 9, 25, 12, tzinfo=UTC)
    slot = now - timedelta(minutes=5)
    runs = []
    buckets = []
    observations = []
    evidence = []
    for product_id in products:
        for i, (target, test) in enumerate(
            (("generic_http", "pulse"), ("generic_https", "pulse"), ("generic_http", "load"))
        ):
            key = product_id * 10 + i
            scheme = "https" if target == "generic_https" else "http"
            scope = {
                "target": {
                    "id": target,
                    "url": scheme + "://ip-info.com/myip",
                    "version": "1",
                },
                "config": {
                    "concurrency": 1000 if test == "load" else 100,
                    "timeout_seconds": 15,
                    "requested_country": "us",
                    "runner_location": "test",
                },
            }
            run = {
                "run_key": key,
                "run_id": str(key),
                "product_id": product_id,
                "provider": f"p{product_id}",
                "name": "residential",
                "role": "monitor",
                "batch_id": key,
                "test": test,
                "slot_at": slot,
                "batch_country": "US",
                "scope": scope,
                "started_at": now - timedelta(minutes=2),
                "finished_at": now - timedelta(minutes=1),
                "summary": {},
            }
            runs.append(run)
            buckets.append(
                {
                    "run_key": key,
                    "bucket_start": now - timedelta(minutes=5),
                    "counters": {
                        "completed": 1,
                        "successes": 1,
                        "errors": {},
                        "response_body_bytes": 10,
                        "latency_histogram": {"bins": [[100 * product_id, 1]]},
                    },
                }
            )
            if i == 0:
                observations.append(
                    {
                        "run_key": key,
                        "bucket_start": now - timedelta(minutes=5),
                        "ip_id": 1,
                        "first_seen": now - timedelta(minutes=2),
                        "last_seen": now - timedelta(minutes=1),
                        "count": 1,
                        "product_first_seen": now - timedelta(minutes=2),
                        "matched": True,
                        "country_code": "US",
                        "asn": 1,
                        "usage_type": "residential",
                        "ip": "1.1.1.1",
                        "city": "Test",
                        "region": "Test",
                    }
                )
        for metric, value in {"uptime_pct": 100, "price_per_gb_lowest": 5.0}.items():
            evidence.append(
                {
                    "product_id": product_id,
                    "cohort": "*",
                    "metric": metric,
                    "value": value,
                    "period_start": now - timedelta(days=40),
                    "period_end": now,
                    "valid_until": now + timedelta(hours=12),
                    "source": "fixture",
                }
            )
    return config, now, runs, buckets, observations, evidence


BREAKDOWN_COUNTERS = (
    "ips",
    "observations",
    "completed",
    "successes",
    "success_rate",
    "challenges",
    "blocks",
    "p50",
    "p90",
    "p99",
)
VIEW_NAMES = {"city": "geography"}


def payload_mismatches(database, build_id):
    """Payload fragments that the typed calculated rows of a build do not rebuild exactly."""
    canon = lambda value: json.dumps(value, sort_keys=True)  # noqa: E731
    payload = store.read(database, str(build_id))
    with connect(database) as conn:
        windows = {
            tuple(r[1:]): r[0]
            for r in conn.execute(
                "SELECT result_id,product_id,cohort,target,window_hours,readiness "
                "FROM calculated_windows WHERE build_id=%s",
                (build_id,),
            )
        }
        scores = {
            tuple(r[:2]): canon(r[2:])
            for r in conn.execute(
                "SELECT result_id,profile,components,reasons,weights FROM calculated_score_details"
            )
        }
        breakdowns = {}
        for r in conn.execute(
            "SELECT result_id,dimension,label,ips,observations,completed,successes,success_rate,"
            "challenges,blocks,p50,p90,p99 FROM calculated_breakdown_details "
            "ORDER BY result_id,dimension,position"
        ):
            counters = dict(zip(BREAKDOWN_COUNTERS, r[3:], strict=True))
            if payload["schema_version"] == 1:
                counters = {k: counters[k] for k in ("ips", "observations")}
            breakdowns.setdefault(r[:2], []).append(r[2] | counters)
        criteria = dict(conn.execute("SELECT cohort,criteria FROM cohort_criteria"))
    missing = []
    for p in payload["products"]:
        if canon(criteria[p["cohort"]]) != canon(p["criteria"]):
            missing.append(("criteria", p["cohort"]))
        for w in p["windows"].values():
            key = (p["product_id"], p["cohort"], "", w["period"]["hours"], w["readiness"])
            for target, profile, s in [("", k, v) for k, v in w["scores"].items()] + [
                (t, "balanced", v["score"]) for t, v in w["targets"].items()
            ]:
                result = windows[key[:2] + (target,) + key[3:]]
                if scores[(result, profile)] != canon(
                    [s["components"], s["reasons"], s.get("weights")]
                ):
                    missing.append(("score", result, profile))
            if payload["schema_version"] == 1:
                expected = [
                    ("geography", w["pool"]["geo"]),
                    ("country", w["pool"]["countries_breakdown"]),
                    ("asn", w["pool"]["asns_breakdown"]),
                ]
            else:
                # v2: every breakdown row is typed; the v1 pool lists derive from them. The
                # payload keeps the top rows of each list and its full row count (slim.py);
                # payloads without counts kept every row.
                expected = [(VIEW_NAMES.get(n, n), rows) for n, rows in w["breakdowns"].items()]
                full = {
                    n: breakdowns.get((windows[key], VIEW_NAMES.get(n, n)), [])
                    for n in w["breakdowns"]
                }
                counts = w.get("breakdown_entries", {n: len(r) for n, r in w["breakdowns"].items()})
                if counts != {n: len(r) for n, r in full.items()}:
                    missing.append(("breakdown entries", windows[key]))
                lists = w["pool"].get("list_entries", {})
                if any(
                    w["pool"][k] != v[: len(w["pool"][k])]
                    or lists.get(k, len(w["pool"][k])) != len(v)
                    for k, v in pool_lists(full).items()
                ):
                    missing.append(("pool lists", windows[key]))
            for dimension, rows in expected:
                typed = breakdowns.get((windows[key], dimension), [])
                if payload["schema_version"] >= 2:
                    typed = typed[: len(rows)]
                if canon(typed) != canon(rows):
                    missing.append((dimension, windows[key]))
    return missing


def http_value(text):
    """A methodology text whose value ranks the HTTP cost per 1,000 successes ([value] basis
    "rank"): for fixtures without browser runs, where the all-targets cost has no browser target
    to price and every network would be unpriced (test_value_all.py scores "rank_all_targets")."""
    return text.replace('basis = "rank_all_targets"', 'basis = "rank"')


def lenient_policy(tmp_path):
    policy = tmp_path / "methodology.toml"
    policy.write_text(
        http_value(POLICY.read_text())
        .replace("minimum_requests = 4000", "minimum_requests = 1")
        .replace("minimum_coverage = 0.90", "minimum_coverage = 0")
        .replace("min_latency_successes = 30", "min_latency_successes = 1")
        .replace("browser_min_requests = 500", "browser_min_requests = 100")
    )
    return policy


class OtherIP(FakeTransport):
    async def execute(self, target, *, context=None):
        return Outcome(30.2, ip="9.9.9.9", status=200, response_body_bytes=8)


async def spread(tmp_path, monkeypatch, start, offsets, **options):
    """A run that starts at `start` and completes one request at each of `offsets` (seconds
    after it) on the runner's own clocks, so it has observations in every 5-minute bucket the
    offsets reach and finishes at the last one. Fixed clocks: the run's buckets never depend on
    when the test runs."""
    from atlas import runner

    clock = [0.0]

    class Stepping(FakeTransport):
        async def execute(self, target, *, context=None):
            clock[0] = offsets[self.count]
            self.count += 1
            return Outcome(10.1, ip="8.8.8.8", status=200, response_body_bytes=8)

    with monkeypatch.context() as fixed:
        fixed.setattr(runner.time, "time", lambda: start.timestamp())
        fixed.setattr(runner.time, "perf_counter", lambda: clock[0])
        return await measurement(
            tmp_path, requests=len(offsets), concurrency=1, transport_factory=Stepping, **options
        )


async def benchmark_scenario(database, tmp_path):
    """Two providers with pulses on both targets and a load run in the same US slot, and
    evidence: ready, ranked, then expired scores."""
    from atlas.storage import seed

    store.initialize(database)
    seed(database)
    slot = datetime.fromtimestamp(datetime.now(UTC).timestamp() // 1800 * 1800, UTC)
    with connect(database) as conn:
        batches = {
            (kind, target): conn.execute(
                "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
                "start_at,status) SELECT %s,'US',region_id,%s,%s,%s,'done' FROM countries "
                "WHERE country_code='US' RETURNING batch_id",
                (kind, target, slot, slot),
            ).fetchone()[0]
            for kind, target in (
                ("pulse", "generic_http"),
                ("pulse", "generic_https"),
                ("load", "generic_http"),
            )
        }
    last = None
    for provider in ("alpha", "beta"):
        for (kind, target), batch_id in batches.items():
            d = await measurement(
                tmp_path,
                provider=provider,
                target=target,
                requested_country="us",
                concurrency=1000 if kind == "load" else 100,
                transport_factory=OtherIP if provider == "beta" else FakeTransport,
            )
            if target == "generic_http":
                attributes = ALL_ATTRIBUTES | ({"city": "San Jose"} if provider == "beta" else {})
                enrichment(d, attributes=attributes)
            ingest(d, database, batch_id=batch_id)
            raw = json.loads((d / "summary.json").read_text())
            last = max(last or raw["finished_at"], raw["finished_at"])
    policy = lenient_policy(tmp_path)
    last = datetime.fromisoformat(last)
    first = calculate(database, policy, as_of=last + timedelta(minutes=5), publish=True)
    with connect(database) as conn:
        ids = [r[0] for r in conn.execute("SELECT product_id FROM products ORDER BY 1")]
    for product_id in ids:
        for metric, value in {
            "uptime_pct": 99.5,
            "price_per_gb_lowest": 4.0,
            "price_per_gb_basis": 4.0,
        }.items():
            store.evidence(
                database,
                {
                    "product_id": product_id,
                    "cohort": first["products"][0]["cohort"],
                    "metric": metric,
                    "value": value,
                    "period_start": (last - timedelta(days=60)).isoformat(),
                    "period_end": (last + timedelta(days=10)).isoformat(),
                    "valid_until": (last + timedelta(days=20)).isoformat(),
                    "source": "fixture",
                },
            )
    return policy, last, first
