"""The pulse: countries spread over the slot, network outages from the errors of all regions,
latency rises over the daily test's p50 and no per-IP rows."""

import gzip
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_census_pulse import batch, batch_run, product
from test_daily import chunk, observation
from test_orchestrator import prepare, query
from test_storage import measurement

from proxy_index import detect
from proxy_index.daily import ingest as daily_ingest
from proxy_index.orchestrator import batches, pulse
from proxy_index.orchestrator.db import Database, put_setting
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.storage import connect, ingest, partition

pytest_plugins = ["test_storage"]

W0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
WINDOW = pulse.WINDOW
SETTLE = timedelta(seconds=180)
REGIONS = ("DE", "GB", "NL", "US")  # fra1, lon1, ams3, nyc3


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()


def window(k):
    return W0 + WINDOW * k


async def evaluated(db, k):
    """Evaluate up to window k, the moment it has settled."""
    return await pulse.evaluate(db, window(k) + WINDOW + SETTLE)


def buckets(conn, key, rows):
    """Replace the run's buckets with (start, completed, errors, p50 ms) rows."""
    conn.execute("DELETE FROM run_buckets WHERE run_key=%s", (key,))
    for start, completed, errors, p50 in rows:
        partition(conn, "run_buckets", start)
        conn.execute(
            "INSERT INTO run_buckets(bucket_start,run_key,completed,successes,"
            "response_body_bytes,errors,status_codes,histogram_steps,histogram_counts,timeouts) "
            "VALUES (%s,%s,%s,%s,0,'{}','{}',%s,%s,%s)",
            (start, key, completed, completed - errors, [p50], [completed - errors or 1], errors),
        )


def products(database, *names):
    with connect(database) as conn:
        return [product(conn, name) for name in names]


async def run(tmp_path, database, kind, at, network, country, rows, target="generic_http"):
    """A done batch of `kind` with one run of the network whose buckets are `rows`."""
    directory = await measurement(tmp_path / f"{kind}-{country}-{at:%H%M}-{network}")
    key = ingest(directory, database)["run_key"]
    with connect(database) as conn:
        buckets(conn, key, rows)
        batch_id = batch(conn, kind, at, country=country, target=target)
        batch_run(conn, batch_id, network, at + timedelta(minutes=1), run_key=key)
    return key


async def daily(tmp_path, database, name, network, country, p50):
    """A daily-test run ingested the way `daily ingest` does: the scheduler's measurement and
    its observation chunk in a round country folder, convert_country, then storage.ingest
    without a batch. Its network is its provider label, its p50 is `p50` ms."""
    measured = await measurement(tmp_path / name, provider=network, requested_country=country)
    with gzip.open(measured / "ips.jsonl.gz", "rt") as stream:
        seen = [json.loads(line) for line in stream]
    folder = tmp_path / f"{name}-round"
    rows = [observation(row["ip"], row["first_seen"]) for row in seen]
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


def tape(database, kind):
    return query(
        database,
        "SELECT product_id,data FROM test_events WHERE kind=%s ORDER BY event_id",
        (kind,),
    )


def test_spread_spaces_each_regions_countries_window_over_n_apart():
    base = datetime(2026, 10, 5, 12, 0, 30, tzinfo=UTC)
    hour = timedelta(hours=1)
    countries = [("FR", 1), ("DE", 2), ("GB", 1), ("AT", 2), ("CH", 2), ("IE", 1)]
    starts = pulse.spread(countries, base, hour)
    for region, codes in ((1, ("FR", "GB", "IE")), (2, ("AT", "CH", "DE"))):
        assert [starts[(c, region)] - base for c in codes] == [hour / 3 * i for i in range(3)]
    shifted = pulse.spread([("GB", 1), ("FR", 1)], base, hour, offset=0.5)
    assert shifted == {("FR", 1): base + hour / 4, ("GB", 1): base + hour * 3 / 4}


async def test_open_slot_uses_the_rest_of_a_late_slot_but_never_starts_closer_than_a_minute(
    database, db
):
    prepare(database, ("DE", "AT", "CH", "PL"))  # all fra1
    slot, hour = datetime(2026, 10, 5, 12, tzinfo=UTC), timedelta(hours=1)
    lead, gap = timedelta(seconds=15), timedelta(seconds=60)  # pulse.lead_seconds, MIN_GAP
    next_slot = slot + hour
    async with db.transaction() as conn:
        on_time = await pulse.open_slot(conn, "generic_http", slot, hour, slot + lead)
        late = await pulse.open_slot(conn, "generic_https", slot, hour, slot + 30 * lead)
        assert await pulse.open_slot(conn, "generic_http", slot, hour, slot) == []
        # Opened at minute 58: 4 countries at 2 minutes / 4 would overlap, so 2 run a minute
        # apart and the other 2 wait for the next slot.
        very_late = await pulse.open_slot(
            conn, "generic_http", next_slot, hour, next_slot + hour * 58 / 60
        )
    assert (len(on_time), len(late), len(very_late)) == (4, 4, 2)

    def starts(target, scheduled):
        return [
            row[0]
            for row in query(
                database,
                "SELECT start_at FROM test_batches WHERE target_id=%s AND scheduled_for=%s "
                "ORDER BY start_at",
                (target, scheduled),
            )
        ]

    # On time: 60 / 4 = 15 minutes apart from slot + lead, never before the planner's now + lead.
    assert starts("generic_http", slot) == [slot + 2 * lead] + [
        slot + lead + hour / 4 * i for i in (1, 2, 3)
    ]
    # Opened 7.5 minutes late (a restart): the rest of the slot, from now + lead.
    rest = hour - 30 * lead
    assert starts("generic_https", slot) == [slot + 31 * lead + rest / 4 * i for i in range(4)]
    begin = next_slot + hour * 58 / 60 + lead
    assert starts("generic_http", next_slot) == [begin, begin + gap]


def test_spread_never_spaces_a_regions_starts_closer_than_min_step():
    base, window = datetime(2026, 10, 5, 12, tzinfo=UTC), timedelta(minutes=3)
    codes = [("AT", 1), ("CH", 1), ("DE", 1), ("PL", 1), ("FR", 2)]
    got = pulse.spread(codes, base, window, 0.5, timedelta(minutes=1))
    # Region 1: max(45 s, 60 s) steps at 0.5, 1.5, 2.5 minutes; PL (3.5) is past the window.
    minute = timedelta(minutes=1)
    assert got == {
        ("AT", 1): base + minute / 2,
        ("CH", 1): base + minute * 1.5,
        ("DE", 1): base + minute * 2.5,
        ("FR", 2): base + window / 2,
    }


async def test_regions_errors_open_one_network_outage_and_a_healthy_window_closes_it(
    database, db, tmp_path
):
    prepare(database, REGIONS)
    alpha, beta = products(database, "alpha", "beta")
    # alpha: three regions at 80% errors, one healthy: 240 / 400 >= 0.5 from 3 regions.
    # beta: two regions all errors: the share passes but two regions are not enough.
    for country in REGIONS:
        errors = 0 if country == "US" else 80
        rows = [(window(0), 100, errors, 300), (window(1), 100, 0, 300)]
        await run(tmp_path, database, "pulse", W0, alpha, country, rows)
    for country in ("DE", "GB"):
        rows = [(window(0), 100, 100, 300)]
        await run(tmp_path, database, "pulse", W0, beta, country, rows, "generic_https")
    first = await evaluated(db, 0)
    assert first == {"windows": 1, "opened": 1, "closed": 0, "latency_rise": 0}
    outages = query(
        database,
        "SELECT outage_id,country_code,source,batch_id,started_at,ended_at FROM outages "
        "ORDER BY outage_id",
    )
    assert [(o[1], o[2], o[3], o[4], o[5]) for o in outages] == [
        (c, "pulse", None, window(0), None) for c in sorted(REGIONS)
    ]
    ids = [o[0] for o in outages]
    ((product_id, down),) = tape(database, "pulse_down")
    assert product_id == alpha
    assert down == {
        "window_start": window(0).isoformat(),
        "window_minutes": 5,
        "errors": 240,
        "requests": 400,
        "regions": 3,
        "threshold": 0.5,
        "outage_ids": ids,
    }
    # The same window again opens nothing.
    assert (await evaluated(db, 0))["windows"] == 0

    # A healthy per-country pulse in the meantime does not end the network outage.
    with connect(database) as conn:
        later = {"started_at": window(1), "start_at": window(1), "country_code": "DE"}
        assert detect._close_outages(conn.cursor(), later, alpha) == []

    second = await evaluated(db, 1)
    assert second == {"windows": 1, "opened": 0, "closed": 1, "latency_rise": 0}
    assert query(database, "SELECT DISTINCT ended_at FROM outages") == [(window(1),)]
    ((product_id, up),) = tape(database, "pulse_recovered")
    assert product_id == alpha
    duration = (window(1) + WINDOW + SETTLE - window(0)).total_seconds()
    assert up == {"down_since": window(0).isoformat(), "duration_s": duration, "outage_ids": ids}


async def test_recovery_waits_for_the_configured_healthy_windows(database, db, tmp_path):
    prepare(database, REGIONS)
    (alpha,) = products(database, "alpha")
    async with db.transaction() as conn:
        await put_setting(conn, "pulse.recover_windows", 2)
    # Down, healthy, no data (neutral), healthy, healthy: one pulse run per window.
    for k in (0, 1, 3, 4):
        rows = [(window(k), 10, 10 if k == 0 else 0, 300)]
        for country in REGIONS:
            await run(tmp_path, database, "pulse", window(k), alpha, country, rows)
    await evaluated(db, 0)
    totals = await evaluated(db, 3)
    assert (totals["windows"], totals["closed"]) == (3, 0)
    assert (await evaluated(db, 4))["closed"] == 1
    assert query(database, "SELECT DISTINCT ended_at FROM outages") == [(window(3),)]


async def test_a_backlog_is_evaluated_over_several_ticks_without_skipping_a_window(
    database, db, tmp_path
):
    prepare(database, REGIONS)
    (alpha,) = products(database, "alpha")
    for country in REGIONS:
        await run(
            tmp_path, database, "pulse", window(3), alpha, country, [(window(3), 10, 10, 300)]
        )
    await evaluated(db, 0)
    # The evaluator was down for 20 windows: 12 now, from the watermark, then the other 8.
    assert await evaluated(db, 20) == {"windows": 12, "opened": 1, "closed": 0, "latency_rise": 0}
    assert (await evaluated(db, 20))["windows"] == 8
    assert query(database, "SELECT value FROM settings WHERE key='pulse.evaluated_until'") == [
        (window(20).isoformat(),)
    ]


async def test_the_latency_baseline_is_the_latest_daily_test_run_census_only_before_one(
    database, db, tmp_path
):
    prepare(database, ("DE",))
    alpha, beta, gamma = products(database, "alpha", "beta", "gamma")
    new = W0 - timedelta(hours=3)
    for network in (alpha, beta):
        await run(tmp_path, database, "census", new, network, "DE", [(new, 100, 0, 100)])
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


async def test_a_latency_rise_over_the_daily_test_is_not_flagged_as_census(
    database, db, tmp_path
):
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


async def test_the_evaluator_reads_one_run_buckets_partition_and_test_batches_by_index(
    database, db
):
    with connect(database) as conn:
        for month in range(7, 13):
            partition(conn, "run_buckets", datetime(2026, month, 15, tzinfo=UTC))
    async with db.transaction() as conn:
        cfg = await pulse._settings(conn)
        await conn.execute("SET LOCAL enable_seqscan=off")
        statements = (
            (pulse.WINDOW_STATS_SQL, pulse._range(W0, cfg)),
            (pulse.PAIRS_SQL, pulse._range(W0, cfg)),
            (pulse.P50_SINCE_SQL, ([1], W0 - WINDOW, W0 + 2 * WINDOW)),
        )
        for statement, params in statements:
            explained = await conn.execute("EXPLAIN (FORMAT JSON) " + statement, params)
            plan = list(nodes((await explained.fetchone())["QUERY PLAN"][0]["Plan"]))
            relations = {n["Relation Name"] for n in plan if "Relation Name" in n}
            assert {r for r in relations if r.startswith("run_buckets")} == {
                "run_buckets_2026_10"
            }, statement
            if "test_batches" in relations:
                scans = [n["Node Type"] for n in plan if n.get("Relation Name") == "test_batches"]
                assert "Seq Scan" not in scans, statement
                assert any("scheduled_for" in n.get("Index Cond", "") for n in plan), statement


async def test_latency_rise_fires_once_per_streak_only_above_the_baseline(database, db, tmp_path):
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
    await evaluated(db, 0)
    # Catching up evaluates each window with the runs up to it.
    assert (await evaluated(db, 6))["latency_rise"] == 2
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


async def test_a_pulse_run_keeps_its_buckets_and_no_per_ip_rows(database, tmp_path):
    census = ingest(await measurement(tmp_path / "census"), database)["run_key"]
    pulsed = ingest(await measurement(tmp_path / "pulse"), database, ip_rows=False)["run_key"]
    counts = query(
        database,
        "SELECT r.run_key,(SELECT count(*) FROM ip_observations o WHERE o.run_key=r.run_key),"
        "(SELECT count(*) FROM run_buckets b WHERE b.run_key=r.run_key) FROM test_runs r "
        "ORDER BY r.run_key",
    )
    observed = {key: (ips, kept) for key, ips, kept in counts}
    assert observed[census][0] > 0 and observed[census][1] > 0
    assert observed[pulsed][0] == 0 and observed[pulsed][1] > 0


async def test_pulse_assignments_use_the_pulse_timeout(database, db):
    prepare(database, ("DE",))
    node = {
        "node_id": issue_token(database, provider="alpha", product="res", region="fra1")["node_id"]
    }
    now = datetime.now(UTC)
    await batches.pulse_plan(db, now)
    work = await batches.assignments(db, node, now + timedelta(hours=1))
    pulses = [w for w in work["assignments"] if w["kind"] == "pulse"]
    assert pulses and {w["timeout_ms"] for w in pulses} == {10000}
