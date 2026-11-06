"""The pulse's latency rises over the daily test's p50 (else the census's), and the
evaluator's reads: one run_buckets partition, test_batches by index."""

import gzip
import json
from datetime import UTC, datetime, timedelta

from daily_cases import chunk, observation
from orchestrator_cases import prepare, query
from pulse_cases import W0, WINDOW, buckets, evaluated, products, run, tape, window
from test_storage import measurement

from proxy_index.daily import DAILY, MARKER
from proxy_index.daily import ingest as daily_ingest
from proxy_index.orchestrator import pulse
from proxy_index.storage import connect, ingest, partition

pytest_plugins = ["test_storage", "orchestrator_cases"]


async def daily(tmp_path, database, name, network, country, p50):
    """A daily-test run ingested the way `daily ingest` does: the scheduler's measurement and
    its observation chunk in a round country folder, convert_country, then storage.ingest
    without a batch. Its network is its provider label, its p50 is `p50` ms."""
    measured = await measurement(tmp_path / name, provider=network, requested_country=country)
    with gzip.open(measured / "ips.jsonl.gz", "rt") as stream:
        seen = [json.loads(line) for line in stream]
    folder = tmp_path / f"{name}-round"
    rows = [observation(row["ip"], row["first_seen"]) for row in seen for _ in range(row["count"])]
    chunk(folder, "observations-000001.jsonl.gz", rows)
    summary = json.loads((measured / "summary.json").read_text())
    (folder / "summary.json").write_text(json.dumps({"measurement": summary}))
    key = ingest(daily_ingest.convert_country(folder, tmp_path / "runs"), database)["run_key"]
    with connect(database) as conn:
        start = conn.execute(
            "SELECT min(bucket_start) FROM run_buckets WHERE run_key=%s", (key,)
        ).fetchone()[0]
        buckets(conn, key, [(start, 100, 0, p50)])
    return key


async def test_the_latency_baseline_is_the_latest_daily_test_run_census_only_before_one(
    database, db, tmp_path
):
    prepare(database, ("DE",))
    alpha, beta, gamma = products(database, "alpha", "beta", "gamma")
    new = W0 - timedelta(hours=3)
    for network, target in ((alpha, "generic_http"), (beta, "generic_https")):
        await run(tmp_path, database, "census", new, network, "DE", [(new, 100, 0, 100)], target)
    # alpha's daily tests: the latest in DE counts (its proxy asked for "de"), FR is not DE.
    await daily(tmp_path, database, "first", "alpha", "de", 300)
    await daily(tmp_path, database, "latest", "alpha", "de", 250)
    await daily(tmp_path, database, "france", "alpha", "FR", 999)
    # Pulses and a batch-less run that is not a daily test never move it.
    await run(tmp_path, database, "pulse", W0, alpha, "DE", [(W0, 100, 0, 900)])
    plain = await measurement(tmp_path / "plain", provider="gamma", requested_country="DE")
    with connect(database) as conn:
        buckets(conn, ingest(plain, database)["run_key"], [(W0, 100, 0, 700)])
    async with db.connection() as conn:
        assert await pulse.latency_baseline(conn, alpha, "DE") == (250, "daily")
        assert await pulse.latency_baseline(conn, alpha, "FR") == (999, "daily")
        assert await pulse.latency_baseline(conn, beta, "DE") == (100, "census")
        assert await pulse.latency_baseline(conn, gamma, "DE") is None
        # Past DAILY_RECENT the bounded lookup finds none; the unbounded one finds the same run.
        later = datetime.now(UTC) + 5 * pulse.DAILY_RECENT
        assert await pulse.latency_baseline(conn, alpha, "DE", later) == (250, "daily")
        assert await pulse.latency_baseline(conn, beta, "DE", later) == (100, "census")


async def test_a_latency_rise_over_the_daily_test_is_not_flagged_as_census(database, db, tmp_path):
    prepare(database, ("DE",))
    (alpha,) = products(database, "alpha")
    census = W0 - timedelta(hours=3)
    await run(tmp_path, database, "census", census, alpha, "DE", [(census, 100, 0, 100)])
    await daily(tmp_path, database, "daily", "alpha", "DE", 40)
    # 70 ms is under 1.5 x the census p50 but over 1.5 x the daily test's.
    for k in range(2):
        await run(tmp_path, database, "pulse", window(k), alpha, "DE", [(window(k), 100, 0, 70)])
    assert (await evaluated(db, 1))["latency_rise"] == 1
    assert tape(database, "latency_rise") == [
        (alpha, {"p50": 70.0, "baseline": 40.0, "baseline_source": "daily"})
    ]


def nodes(plan):
    yield plan
    for child in plan.get("Plans", ()):
        yield from nodes(child)


async def explain(conn, statement, params):
    explained = await conn.execute("EXPLAIN (FORMAT JSON) " + statement, params)
    return list(nodes((await explained.fetchone())["QUERY PLAN"][0]["Plan"]))


async def test_the_evaluator_reads_one_run_buckets_partition_and_test_batches_by_index(
    database, db
):
    with connect(database) as conn:
        for day in range(-6, 7):  # daily partitions around W0
            partition(conn, "run_buckets", W0 + timedelta(days=day))
    async with db.transaction() as conn:
        cfg = await pulse._settings(conn)
        await conn.execute("SET LOCAL enable_seqscan=off")
        statements = (
            (pulse.WINDOW_STATS_SQL, pulse._range(W0, cfg)),
            (pulse.PAIRS_SQL, pulse._range(W0, cfg)),
            (pulse.P50_SINCE_SQL, ([1], W0 - WINDOW, W0 + 2 * WINDOW)),
        )
        for statement, params in statements:
            plan = await explain(conn, statement, params)
            relations = {n["Relation Name"] for n in plan if "Relation Name" in n}
            assert {r for r in relations if r.startswith("run_buckets")} == {
                "run_buckets_2026_10_05"
            }, statement
            if "test_batches" in relations:
                scans = [n["Node Type"] for n in plan if n.get("Relation Name") == "test_batches"]
                assert "Seq Scan" not in scans, statement
                assert any("scheduled_for" in n.get("Index Cond", "") for n in plan), statement
        # The bounded daily-test lookup reads each scope's recent runs through test_runs_scope.
        params = (1, "DE", MARKER, DAILY, W0 - pulse.DAILY_RECENT)
        plan = await explain(conn, pulse.DAILY_BASELINE_SQL, params)
        assert "Seq Scan" not in [n["Node Type"] for n in plan if n.get("Relation Name")]
        assert any(
            n.get("Index Name") == "test_runs_scope" and "finished_at" in n.get("Index Cond", "")
            for n in plan
        )


async def test_latency_rise_fires_once_per_streak_only_above_the_baseline(
    database, db, tmp_path, monkeypatch
):
    prepare(database, ("DE",))
    alpha, beta = products(database, "alpha", "beta")
    daily = W0 - timedelta(hours=3)
    for network, target in ((alpha, "generic_http"), (beta, "generic_https")):
        rows = [(daily, 100, 0, 100)]
        await run(tmp_path, database, "census", daily, network, "DE", rows, target)
    # alpha: 1.2x, 1.6x, 1.7x (fires), 2x (same streak), 0.5x, 1.6x, 1.6x (fires again).
    for k, p50 in enumerate((120, 160, 170, 200, 50, 160, 160)):
        await run(tmp_path, database, "pulse", window(k), alpha, "DE", [(window(k), 100, 0, p50)])
    # beta: always below the baseline, which a lower pulse never moves.
    for k in range(7):
        rows = [(window(k), 100, 0, 60)]
        await run(tmp_path, database, "pulse", window(k), beta, "DE", rows, "generic_https")
    looked_up, lookup = [], pulse.latency_baseline

    async def counted(conn, product, country, now=None):
        looked_up.append((product, country))
        return await lookup(conn, product, country, now)

    monkeypatch.setattr(pulse, "latency_baseline", counted)
    await evaluated(db, 0)
    assert looked_up == []  # one run each, no streak: the baseline is never read
    # Catching up evaluates each window with the runs up to it, reading each baseline once.
    assert (await evaluated(db, 6))["latency_rise"] == 2
    assert sorted(looked_up) == [(alpha, "DE"), (beta, "DE")]
    rises = query(
        database,
        "SELECT product_id,country_code,data FROM test_events WHERE kind='latency_rise' "
        "ORDER BY event_id",
    )
    assert rises == [
        (alpha, "DE", {"p50": 170.0, "baseline": 100.0, "baseline_source": "census"}),
        (alpha, "DE", {"p50": 160.0, "baseline": 100.0, "baseline_source": "census"}),
    ]
    assert query(database, "SELECT count(*) FROM outages") == [(0,)]
