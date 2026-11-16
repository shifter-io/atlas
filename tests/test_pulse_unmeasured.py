"""OUTAGE-NOT-MEASURED-1 (#1277): a network outage is never extended by our not measuring.

A window without the network's pulse data ends its open outage there (end_reason
'not_measured', no tape event), so the silence counts no downtime; another down window keeps it
open and a healthy one closes it, as before; the next down window after the silence opens a new
one. A country the network was not pulsed in by its last pulse interval gets no row. A window
mostly refused at login still leaves the outage as it is (PROXY-AUTH-ALERT-1)."""

from datetime import timedelta

from orchestrator_cases import prepare, query
from pulse_cases import W0, evaluated, products, run, tape, window
from test_proxy_login import pulse_run

from atlas.calculated.outage_time import outage_uptime
from atlas.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]

REGIONS = ("DE", "GB", "NL", "US")  # fra1, lon1, ams3, nyc3
OUTAGES = "SELECT country_code,started_at,ended_at,end_reason FROM outages ORDER BY outage_id"
SPANS = "SELECT DISTINCT started_at,ended_at,end_reason FROM outages ORDER BY 1"


async def pulses(tmp_path, database, network, k, errors, countries=REGIONS):
    """A 100-request pulse run of the network in each country, `errors` of them failed, in
    window k (its batch's slot)."""
    for country in countries:
        rows = [(window(k), 100, errors, 300)]
        await run(tmp_path, database, "pulse", window(k), network, country, rows)


async def test_a_down_window_then_silence_ends_the_outage_where_measuring_ended(
    database, db, tmp_path
):
    """The 16 Nov 17:50 case: the last window measured is down, the rest of the hour has no
    pulse data (the pulses stopped at their last start)."""
    prepare(database, REGIONS)
    (alpha,) = products(database, "alpha")
    await pulses(tmp_path, database, alpha, 0, errors=100)
    assert (await evaluated(db, 0))["opened"] == 1
    quiet = await evaluated(db, 11)
    assert quiet == {"windows": 11, "opened": 0, "closed": 1, "latency_rise": 0}
    found = query(database, OUTAGES)
    assert found == [(c, window(0), window(1), "not_measured") for c in sorted(REGIONS)]
    assert len(tape(database, "pulse_down")) == 1 and tape(database, "pulse_recovered") == []
    # The public figures count the measured window only: 5 minutes in each country, not the hour.
    outages = [{"country": c, "started_at": a, "ended_at": b} for c, a, b, _ in found]
    tested = {country: window(12) for country in REGIONS}
    uptime = outage_uptime(outages, tested, W0, window(12))
    assert (uptime["downtime_s"], uptime["countries"]) == (4 * 300.0, 4)
    assert uptime["value"] == 1 - 300 / 3600


async def test_a_down_window_extends_it_a_silent_one_ends_it_the_next_down_one_reopens(
    database, db, tmp_path
):
    prepare(database, REGIONS)
    (alpha,) = products(database, "alpha")
    for k, errors in ((0, 100), (1, 100), (3, 100), (4, 0)):  # window 2: no pulse data
        await pulses(tmp_path, database, alpha, k, errors)
    assert (await evaluated(db, 0))["opened"] == 1
    assert (await evaluated(db, 1))["opened"] == 0
    assert query(database, SPANS) == [(window(0), None, None)]  # down again: still open
    assert await evaluated(db, 4) == {"windows": 3, "opened": 1, "closed": 2, "latency_rise": 0}
    assert query(database, SPANS) == [
        (window(0), window(2), "not_measured"),
        (window(3), window(4), None),  # the healthy window 4 closes the reopened one
    ]
    assert len(tape(database, "pulse_down")) == 2
    ((_, up),) = tape(database, "pulse_recovered")
    assert up["down_since"] == window(3).isoformat()


async def test_a_country_not_pulsed_in_the_last_interval_is_not_down(database, db, tmp_path):
    """AT's last pulse is 31 minutes before the window's end (the pulse interval is 30), CH's
    pulse of the slot failed on our side and PL was never opened: none of them is down."""
    prepare(database, (*REGIONS, "AT", "CH", "PL"))  # AT, CH, PL: fra1
    (alpha,) = products(database, "alpha")
    await pulses(tmp_path, database, alpha, 0, errors=100)
    earlier = W0 - timedelta(minutes=26)
    rows = [(W0 - timedelta(minutes=25), 100, 0, 300)]
    await run(tmp_path, database, "pulse", earlier, alpha, "AT", rows)
    key = await run(tmp_path, database, "pulse", W0, alpha, "CH", [])
    with connect(database) as conn:
        conn.execute("UPDATE batch_runs SET status='failed' WHERE run_key=%s", (key,))
    assert (await evaluated(db, 0))["opened"] == 1
    assert [row[0] for row in query(database, OUTAGES)] == sorted(REGIONS)


async def test_a_window_refused_at_login_keeps_the_outage_and_silence_then_ends_it(
    database, db, tmp_path
):
    prepare(database, REGIONS)
    (alpha,) = products(database, "alpha")
    for country in REGIONS:
        await pulse_run(tmp_path, database, alpha, country, 0, refused=0, other=100)
        await pulse_run(tmp_path, database, alpha, country, 1, refused=100)
    assert (await evaluated(db, 0))["opened"] == 1
    assert (await evaluated(db, 1))["closed"] == 0
    assert query(database, SPANS) == [(window(0), None, None)]  # refused: as it was
    assert (await evaluated(db, 2))["closed"] == 1
    assert query(database, SPANS) == [(window(0), window(2), "not_measured")]

