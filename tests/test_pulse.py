"""The pulse: countries spread over the slot, network outages from the errors of all regions
and buckets kept without per-IP rows. The latency rises and the evaluator's plans are in
`test_pulse_latency.py`; a pulse upload's IP rows and enrichment in `test_pulse_enrich.py`."""

from datetime import UTC, datetime, timedelta

from orchestrator_cases import prepare, query
from pulse_cases import W0, evaluated, products, run, tape, window
from test_storage import measurement

from atlas import detect
from atlas.orchestrator import batches, nodework, pulse
from atlas.orchestrator.db import put_setting
from atlas.orchestrator.nodes import issue_token
from atlas.storage import connect, ingest

pytest_plugins = ["test_storage", "orchestrator_cases"]

REGIONS = ("DE", "GB", "NL", "US")  # fra1, lon1, ams3, nyc3


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
    # Both events are at the outage's start and end, not at the evaluations (TAPE-TIME-1).
    duration = (window(1) - window(0)).total_seconds()
    assert up == {"down_since": window(0).isoformat(), "duration_s": duration, "outage_ids": ids}
    stamps = "SELECT kind,at FROM test_events WHERE kind LIKE 'pulse_%' ORDER BY event_id"
    assert query(database, stamps) == [("pulse_down", window(0)), ("pulse_recovered", window(1))]


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


async def test_an_hour_sampled_in_its_first_minutes_keeps_its_quiet_rest_neutral(
    database, db, tmp_path
):
    """PULSE-COMPACT-1: on-demand pulses run every country back to back in the first minutes
    of the hour. The windows after them have no pulse data: neither down nor healthy, so no
    outage opens or closes in the quiet rest, and one stays open until the next hour's pulses."""
    prepare(database, REGIONS)
    alpha, beta = products(database, "alpha", "beta")
    for country in REGIONS:  # alpha down, beta healthy, both only in window 0 of the hour
        await run(tmp_path, database, "pulse", W0, alpha, country, [(window(0), 100, 100, 300)])
        rows = [(window(0), 100, 0, 300)]
        await run(tmp_path, database, "pulse", W0, beta, country, rows, "generic_https")
    assert (await evaluated(db, 0))["opened"] == 1
    quiet = await evaluated(db, 11)  # HH:05 to the end of the hour: no pulse data
    assert quiet == {"windows": 11, "opened": 0, "closed": 0, "latency_rise": 0}
    assert query(database, "SELECT DISTINCT product_id,ended_at FROM outages") == [(alpha, None)]
    for country in REGIONS:  # the next hour's pulses, healthy
        at, rows = window(12), [(window(12), 100, 0, 300)]
        await run(tmp_path, database, "pulse", at, alpha, country, rows)
    assert (await evaluated(db, 12))["closed"] == 1
    assert query(database, "SELECT DISTINCT ended_at FROM outages") == [(window(12),)]


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


async def test_a_run_stored_without_ip_rows_keeps_its_buckets(database, tmp_path):
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
    work = await nodework.assignments(db, node, now + timedelta(hours=1))
    pulses = [w for w in work["assignments"] if w["kind"] == "pulse"]
    assert pulses and {w["timeout_ms"] for w in pulses} == {10000}
