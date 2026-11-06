"""CALC-PULSE-1a: the census-pulse geo rows of the incremental calculation come from per-hour
rollups (calculated/rollups.py) and equal the full mode's 30-day query (scheduled.py) exactly.

The 3-day fixture (test_calculated_hourly_reads.History, on a real PostgreSQL) holds daily tests,
census and pulse batches, a pulse that ran during a census (the census overlap), a browser batch
and outages, each run with seeded random run_geo_buckets rows (known and unknown exit locations,
several networks, latency histograms). Swept over every hour of the 3 days and at cutoffs inside
an hour, the inputs read through the rollups equal the full mode's, for the latency target and
another one; halfway a late run lands in hours already rolled up, and a run outside a batch is
linked to one. Each later cutoff counts only the hours that changed.
"""

import random
from datetime import UTC, datetime, timedelta

import pytest
from calculated_cases import lenient_policy
from psycopg.rows import dict_row
from test_calculated_hourly_reads import DAY, HOUR, History, floor_hour
from test_calculated_incremental import assert_equal

from proxy_index.calculated import rollups, scheduled, store
from proxy_index.partitions import partition
from proxy_index.storage import connect, migrate

pytest_plugins = ["test_storage"]

ROLLUPS = ("calculated_geo_hours", "calculated_geo_rollups")
MINUTES = timedelta(minutes=1)


def seed_geo(conn, rng, run_key):
    """Replace the run's geo rows with seeded ones: 1 to 3 (location, network) pairs a bucket."""
    locations = [r[0] for r in conn.execute("SELECT location_id FROM enrichment_locations")]
    networks = [r[0] for r in conn.execute("SELECT network_id FROM enrichment_networks")]
    conn.execute("DELETE FROM run_geo_buckets WHERE run_key=%s", (run_key,))
    buckets = conn.execute(
        "SELECT bucket_start FROM run_buckets WHERE run_key=%s ORDER BY 1", (run_key,)
    ).fetchall()
    for (bucket,) in buckets:
        partition(conn, "run_geo_buckets", bucket)
        pairs = {(rng.choice(locations), rng.choice(networks)) for _ in range(rng.randint(1, 3))}
        for location, network in sorted(pairs):
            ms = sorted(rng.sample(range(20, 400), rng.randint(1, 4)))
            counts = [rng.randint(1, 5) for _ in ms]
            steps = [b - a for a, b in zip([0, *ms], ms, strict=False)]
            successes = sum(counts)
            conn.execute(
                "INSERT INTO run_geo_buckets VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,0,0,%s,%s)",
                (bucket, run_key, location, network, successes + rng.randint(0, 3), successes)
                + (rng.randint(0, 2), rng.randint(0, 2), steps, counts),
            )


async def fixture(database, tmp_path, end):
    """The 3-day history (module docstring); returns it with its loose run for the link."""
    h = History(database, tmp_path, end, seed_value=148)
    rng = random.Random(148)
    keys = [
        await h.daily("alpha", "us", 70 * HOUR),
        await h.daily("beta", "us", 47 * HOUR, split=True),
        await h.daily("alpha", "de", 23 * HOUR + 25 * MINUTES),
        await h.scheduled("pulse", "alpha", 50 * HOUR),
        await h.scheduled("pulse", "beta", 26 * HOUR),
        await h.scheduled("pulse", "alpha", 2 * HOUR),
        await h.browser("alpha", 2),
    ]
    census = await h.scheduled("census", "alpha", 30 * HOUR)
    during = await h.scheduled("pulse", "alpha", 30 * HOUR - 10 * MINUTES)  # the overlap
    keys += [census, during]
    with connect(database) as conn:
        for key in keys:
            seed_geo(conn, rng, key)
        conn.execute(  # the census ran for two hours around the pulse
            "UPDATE test_runs SET started_at=%s,finished_at=%s WHERE run_key=%s",
            (end - 31 * HOUR, end - 29 * HOUR, census),
        )
        alpha = conn.execute("SELECT product_id FROM products WHERE provider='alpha'").fetchone()
        conn.execute(
            "INSERT INTO outages(product_id,country_code,source,started_at,ended_at) "
            "VALUES (%s,'DE','pulse',%s,%s),(%s,'DE','census',%s,NULL)",
            (alpha[0], end - 80 * HOUR, end - 78 * HOUR, alpha[0], end - 3 * HOUR),
        )
    return h, rng, keys[0]


def compare(database, as_of):
    """The inputs through the rollups equal the full mode's, for two latency targets; the hours
    the rollups counted, and the latency target's inputs."""
    with connect(database) as conn:
        conn.row_factory = dict_row
        counted, found = [], []
        for target in ("generic_http", "no_such_target"):
            full = scheduled.scheduled_inputs(conn, as_of, target)
            hours, _ = rollups.spans(tuple(bounds(as_of).values()))
            counted += rollups.refresh(conn, hours)
            assert rollups.scheduled_inputs(conn, as_of, target) == full, (as_of, target)
            found.append(full)
        assert not any(g["histogram"] for g in found[1]["geo"])  # no request of that target
        return counted, found[0]


def bounds(as_of):
    start = as_of - 720 * HOUR
    return {"start": start, "d7": as_of - 168 * HOUR, "d1": as_of - 24 * HOUR, "end": as_of}


@pytest.mark.asyncio
async def test_rollups_equal_the_full_mode_over_three_days(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    h, rng, loose = await fixture(database, tmp_path, end)
    seen = {"overlap": False, "light": False, "fallback": False, "scope": False}
    for hours in range(72, -1, -1):
        as_of = end - hours * HOUR
        counted, full = compare(database, as_of)
        if hours < 72:  # after the first cutoff only the new hour is counted
            assert counted == [as_of - HOUR], (as_of, counted)
        if hours == 36:  # a late run in an hour rolled up already, then a link to a batch
            late = await h.scheduled("pulse", "beta", 60 * HOUR)
            with connect(database) as conn:
                seed_geo(conn, rng, late)
            assert compare(database, as_of)[0] == [end - 60 * HOUR]
            with connect(database) as conn:
                batch = conn.execute("SELECT max(batch_id) FROM test_batches").fetchone()[0]
                conn.execute("UPDATE test_runs SET batch_id=%s WHERE run_key=%s", (batch, loose))
            assert compare(database, as_of)[0] == [end - 70 * HOUR]
        counted, _ = compare(database, as_of + 25 * MINUTES)  # inside an hour: raw edges
        assert counted == [], counted
        seen["overlap"] |= bool(full["overlaps"])
        seen["light"] |= any(g["histogram"] for g in full["geo"])
        seen["fallback"] |= any(g["fallback"] for g in full["geo"])
        seen["scope"] |= any(g["scope_id"] for g in full["geo"])
        assert full["outages"]
    assert all(seen.values()), seen


@pytest.mark.asyncio
async def test_incremental_build_inside_an_hour_equals_the_full_build(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await fixture(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    for as_of in (end - 30 * HOUR, end - 29 * HOUR + 35 * MINUTES):
        payload = assert_equal(database, policy, as_of)
        assert any(p["criteria"].get("schedule") for p in payload["products"])
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM calculated_geo_rollups").fetchone()[0] >= 719


def test_spans_cut_the_window_at_its_edges():
    end = datetime(2026, 11, 6, 12, tzinfo=UTC)
    hours, edges = rollups.spans(tuple(bounds(end).values()))
    assert (len(hours), edges) == (720, [])
    assert hours[0] == end - 720 * HOUR and hours[-1] == end - HOUR
    inside = end + 25 * MINUTES
    hours, edges = rollups.spans(tuple(bounds(inside).values()))
    # The partial first and last hours and the hours 7 days and 1 day back are read raw: 721
    # hours touched, 717 whole.
    assert len(hours) == 717 and hours[0] == end - 719 * HOUR
    assert [(lo - inside, hi - inside) for lo, hi in edges] == [
        (-720 * HOUR, -720 * HOUR + 35 * MINUTES),
        (-168 * HOUR - 25 * MINUTES, -168 * HOUR),
        (-168 * HOUR, -168 * HOUR + 35 * MINUTES),
        (-24 * HOUR - 25 * MINUTES, -24 * HOUR),
        (-24 * HOUR, -24 * HOUR + 35 * MINUTES),
        (-25 * MINUTES, timedelta(0)),
    ]
    covered = set(hours) | {floor_hour(lo) for lo, _ in edges}
    assert len(covered) == 721 and min(covered) == floor_hour(inside - 720 * HOUR)
    assert rollups._ranges([end, end + HOUR, end + 3 * HOUR]) == [
        [end, end + 2 * HOUR],
        [end + 3 * HOUR, end + 4 * HOUR],
    ]


@pytest.mark.asyncio
async def test_old_rollups_are_collected(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await fixture(database, tmp_path, end)
    old = end - 40 * DAY
    with connect(database) as conn:
        conn.execute("INSERT INTO calculated_geo_rollups VALUES (%s,1)", (old,))
        conn.execute(
            "INSERT INTO calculated_geo_hours(hour,product_id,location_id,network_id,target,"
            "completed,successes,challenges,blocks,light_ms,light_n) "
            "VALUES (%s,1,1,1,'t',1,1,0,0,'{}','{}')",
            (old,),
        )
    compare(database, end)
    with connect(database) as conn:
        for table in ROLLUPS:
            query = f"SELECT count(*) FROM {table} WHERE hour<%s"
            assert conn.execute(query, (end - 31 * DAY,)).fetchone() == (0,), table


@pytest.mark.asyncio
async def test_v10_layer_migrates_to_the_geo_rollups_keeping_its_lists(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await fixture(database, tmp_path, end)
    assert_equal(database, lenient_policy(tmp_path), end)
    with connect(database) as conn:
        before = conn.execute("SELECT * FROM calculated_lists ORDER BY list_id").fetchall()
        conn.execute("DROP TABLE calculated_geo_hours,calculated_geo_rollups")
        conn.execute("UPDATE calculation_schema SET version=10,checksum=%s", (store.V10_CHECKSUM,))
    result = migrate(database)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        10,
        11,
    )
    assert set(store.V10_CHECKS) <= set(result["verified"])
    with connect(database) as conn:
        assert conn.execute("SELECT * FROM calculated_lists ORDER BY list_id").fetchall() == before
        for table in ROLLUPS:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    # The next incremental build fills them again and still equals the full one.
    assert_equal(database, lenient_policy(tmp_path), end)
