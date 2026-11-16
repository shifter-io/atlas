"""CALC-PULSE-1a and 1b: the census-pulse geo rows of the incremental calculation come from
per-hour rollups (calculated/rollups.py) and equal the full mode's 30-day query (scheduled.py)
exactly; its batch runs' buckets come from per-hour copies and equal the full mode's rows less the
counters nothing reads, and its census overlaps, pulse slots and tested spans come from one read of
the batch runs and equal the full mode's three.

The 3-day fixture (test_calculated_hourly_reads.History, on a real PostgreSQL) holds daily tests,
census and pulse batches, a pulse that ran during a census (the census overlap), a browser batch and
outages, each run with seeded random run_geo_buckets rows (known and unknown exit locations, several
networks, latency histograms), an older browser bucket, and batch runs without an artifact: a
skipped pulse slot, a failed pulse, a load wave and a census still running. Swept over every hour of
the 3 days and at cutoffs inside an hour, the inputs read through the rollups equal the full mode's,
for the latency target and another one, and so do the batch runs' buckets read through their copies;
halfway a late run lands in hours already rolled up and copied, and a run outside a batch is linked
to one. Each later cutoff counts and copies only the hours that changed.
"""

import random
from datetime import UTC, datetime, timedelta

import pytest
from calculated_cases import lenient_policy
from census_pulse_cases import batch, batch_run
from psycopg.rows import dict_row
from test_calculated_hourly_reads import DAY, HOUR, History, floor_hour
from test_calculated_incremental import assert_equal

from atlas.calculated import rollups, scheduled, store
from atlas.calculated.incremental import BUCKETS
from atlas.partitions import partition
from atlas.storage import connect, migrate

pytest_plugins = ["test_storage"]

ROLLUPS = ("calculated_geo_hours", "calculated_geo_rollups")
COPIES = ("calculated_pulse_buckets", "calculated_pulse_rollups")
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
        unrecorded(conn, alpha[0], end)
        conn.execute(  # an older browser bucket: its outcome counters and label stay in extra
            "UPDATE run_buckets SET outcome_counters=false,"
            """extra='{"challenges":2,"bucket_start":"label"}' WHERE run_key=%s""",
            (keys[6],),
        )
    return h, rng, keys[0]


def unrecorded(conn, product_id, end):
    """Batch runs without an artifact: a skipped pulse slot, a failed pulse, a load wave during
    the census, and a census of another country still running."""
    runs = (("pulse", 40, "skipped", "done"), ("pulse", 20, "done", "failed"))
    runs += (("load", 30, "done", "done"), ("census", 5, "running", "running"))
    for kind, back, status, run_status in runs:
        at = end - back * HOUR + 7 * MINUTES
        batch_id = batch(conn, kind, at, country="FR", status=status)
        batch_run(conn, batch_id, product_id, at + MINUTES)
        conn.execute(
            "UPDATE batch_runs SET status=%s,finished_at=CASE WHEN %s='running' THEN NULL "
            "ELSE finished_at END WHERE batch_id=%s",
            (run_status, run_status, batch_id),
        )


def compare(database, as_of):
    """The inputs through the rollups equal the full mode's, for two latency targets, and so do
    the batch runs' buckets through their copies; the hours the rollups counted (the copies the
    same ones), and the latency target's inputs."""
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
        start = as_of - 720 * HOUR
        copied = rollups.refresh_buckets(conn, rollups.spans((start, as_of))[0])
        assert rollups.scheduled_rows(conn, start, as_of) == batch_rows(conn, start, as_of), as_of
        assert copied == counted, (as_of, copied, counted)
        return counted, found[0]


def batch_rows(conn, start, as_of):
    """The full mode's buckets of the batch runs (incremental.BUCKETS), less the bucket label, the
    status codes and the histogram format the copies leave out."""
    query = BUCKETS.format(
        join=" JOIN test_runs r USING(run_key)", daily=" AND r.batch_id IS NOT NULL"
    )
    rows = conn.execute(query, (start, as_of)).fetchall()
    for row in rows:
        counters = row["counters"]
        assert counters.pop("bucket_start") and counters.pop("status_codes") is not None
        bins = counters.pop("latency_histogram")["bins"]
        counters["latency_histogram"] = {"bins": [tuple(b) for b in bins]}
    return rows


def bounds(as_of):
    start = as_of - 720 * HOUR
    return {"start": start, "d7": as_of - 168 * HOUR, "d1": as_of - 24 * HOUR, "end": as_of}


# The sweep's 73 hourly cutoffs (hours before the end) in parts of 12, one param each: a part
# builds the rollups and copies of the cutoff before its first, then checks its own.
PARTS = [(72, 61), (60, 49), (48, 37), (36, 25), (24, 13), (12, 0)]
LATE = 36  # the cutoff where the late run lands and the loose run is linked to a batch
KINDS = {"overlap", "light", "fallback", "scope", "skipped"}
# What each part's full inputs show (seen_in); together every kind the fixture holds.
SEEN = {
    72: {"light", "scope"},
    60: {"light", "scope"},
    48: {"light", "fallback", "scope", "skipped"},
    36: {"overlap", "light", "fallback", "scope", "skipped"},
    24: {"overlap", "light", "fallback", "scope", "skipped"},
    12: {"overlap", "light", "fallback", "scope", "skipped"},
}


async def land_late(database, h, rng):
    """A late pulse 60 hours back: in an hour rolled up already when the sweep is past it."""
    late = await h.scheduled("pulse", "beta", 60 * HOUR)
    with connect(database) as conn:
        seed_geo(conn, rng, late)


def link(database, run_key):
    """The run outside a batch joins the latest batch."""
    with connect(database) as conn:
        batch = conn.execute("SELECT max(batch_id) FROM test_batches").fetchone()[0]
        conn.execute("UPDATE test_runs SET batch_id=%s WHERE run_key=%s", (batch, run_key))


def seen_in(full):
    """The kinds the full inputs show: a census overlap, light geo rows, a fallback, a scope and
    the skipped pulse slot."""
    shown = {
        "overlap": bool(full["overlaps"]),
        "light": any(g["histogram"] for g in full["geo"]),
        "fallback": any(g["fallback"] for g in full["geo"]),
        "scope": any(g["scope_id"] for g in full["geo"]),
        "skipped": any(s["country"] == "FR" for s in full["tested"]),
    }
    return {kind for kind, found in shown.items() if found}


# No coverage (tests/conftest.py): other tests cover every line and branch it does (CI-90-5).
@pytest.mark.no_cover
@pytest.mark.asyncio
@pytest.mark.parametrize(("first", "last"), PARTS, ids=[f"{a}-{b}" for a, b in PARTS])
async def test_rollups_equal_the_full_mode_over_three_days(database, tmp_path, first, last):
    assert [h for a, b in PARTS for h in range(a, b - 1, -1)] == list(range(72, -1, -1))
    assert set().union(*SEEN.values()) == KINDS
    end = floor_hour(datetime.now(UTC)) + HOUR
    h, rng, loose = await fixture(database, tmp_path, end)
    if first < LATE:  # the earlier parts' late run and link
        await land_late(database, h, rng)
        link(database, loose)
    if first < 72:  # the state the cutoff before this part's first leaves
        compare(database, end - (first + 1) * HOUR)
    seen = set()
    for hours in range(first, last - 1, -1):
        as_of = end - hours * HOUR
        counted, full = compare(database, as_of)
        if hours < 72:  # after the first cutoff only the new hour is counted
            assert counted == [as_of - HOUR], (as_of, counted)
        if hours == LATE:  # a late run in an hour rolled up already, then a link to a batch
            await land_late(database, h, rng)
            assert compare(database, as_of)[0] == [end - 60 * HOUR]
            link(database, loose)
            assert compare(database, as_of)[0] == [end - 70 * HOUR]
        counted, _ = compare(database, as_of + 25 * MINUTES)  # inside an hour: raw edges
        assert counted == [], counted
        seen |= seen_in(full)
        assert full["outages"]
    assert seen == SEEN[first], seen


@pytest.mark.asyncio
async def test_incremental_build_inside_an_hour_equals_the_full_build(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    h, rng, loose = await fixture(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    for as_of in (end - 30 * HOUR, end - 29 * HOUR + 35 * MINUTES):
        payload = assert_equal(database, policy, as_of)
        assert any(p["criteria"].get("schedule") for p in payload["products"])
    with connect(database) as conn:
        for table in ("calculated_geo_rollups", "calculated_pulse_rollups"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] >= 719, table
    # A late pulse lands in an hour copied already, and a run outside a batch is linked to one.
    late = await h.scheduled("pulse", "alpha", 40 * HOUR)
    with connect(database) as conn:
        seed_geo(conn, rng, late)
    link(database, loose)
    assert_equal(database, policy, end - 29 * HOUR)


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
async def test_old_rollups_and_copies_are_collected(database, tmp_path):
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
        conn.execute("INSERT INTO calculated_pulse_rollups VALUES (%s,1,'{}')", (old,))
        conn.execute(
            "INSERT INTO calculated_pulse_buckets SELECT %s,b.* FROM (SELECT "
            f"{rollups.BUCKET} FROM run_buckets b LIMIT 1) b",
            (old,),
        )
    compare(database, end)
    with connect(database) as conn:
        for table in ROLLUPS + COPIES:
            query = f"SELECT count(*) FROM {table} WHERE hour<%s"
            assert conn.execute(query, (end - 31 * DAY,)).fetchone() == (0,), table


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [10, 11])
async def test_v10_and_v11_layers_migrate_to_the_rollups_keeping_their_lists(
    database, tmp_path, version
):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await fixture(database, tmp_path, end)
    assert_equal(database, lenient_policy(tmp_path), end)
    dropped = COPIES + (ROLLUPS if version == 10 else ())
    with connect(database) as conn:
        before = conn.execute("SELECT * FROM calculated_lists ORDER BY list_id").fetchall()
        conn.execute(f"DROP TABLE {','.join(dropped)}")
        checksum = getattr(store, f"V{version}_CHECKSUM")
        conn.execute("UPDATE calculation_schema SET version=%s,checksum=%s", (version, checksum))
    result = migrate(database)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        version,
        18,
    )
    tail = [*store.V11_CHECKS, *store.V12_CHECKS, *store.V13_CHECKS, *store.V14_CHECKS]
    tail += [*store.V15_CHECKS, *store.V16_CHECKS, *store.V17_CHECKS]
    assert result["verified"][-len(tail) :] == tail
    assert set(store.V10_CHECKS) <= set(result["verified"]) or version == 11
    with connect(database) as conn:
        assert conn.execute("SELECT * FROM calculated_lists ORDER BY list_id").fetchall() == before
        for table in dropped:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    # The next incremental build fills them again and still equals the full one.
    assert_equal(database, lenient_policy(tmp_path), end)
