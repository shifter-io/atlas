"""PULSE-STEP-1: a budgeted network's late pulse slot starts its runs far enough apart that each
finishes at 100 requests within its share, and every country of the hour starts before HH:50.
The database tests use the test's own PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from orchestrator_cases import prepare, query, set_setting

from atlas.campaign import pulse_late, pulse_ondemand
from atlas.daily import budget
from atlas.node_pool import RUNS_AT_ONCE, ConnectionPool
from atlas.orchestrator.nodes import issue_token
from atlas.orchestrator.pulse_hour import Armed
from atlas.regions import load_regions
from atlas.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]

HOUR = datetime(2026, 11, 16, 18, tzinfo=UTC)
RUN = timedelta(seconds=29)  # 100 requests at 5 connections, proxy-cheap, 15 Nov 2026
GATE = timedelta(minutes=20)  # the daily runs of the hour done (they end about HH:15 to HH:20)


def _clock(at: datetime):
    return SimpleNamespace(now=lambda: at.timestamp())


def _hour_cells() -> list[Armed]:
    """8 droplets, one per region, holding about 7 in 10 of its countries (the low-availability
    skip, LOWAVAIL-1: about 175 countries, 350 runs)."""
    cells = []
    for region in load_regions().regions:
        keep = region.countries[: max(1, round(len(region.countries) * 0.7))]
        cells.append(Armed("proxy-cheap", "res", region.slug, keep))
    return cells


async def _ready(database, db, cells: list[Armed]) -> None:
    prepare(database, tuple(code for cell in cells for code in cell.countries))
    with connect(database) as conn:
        conn.execute("UPDATE targets SET pulse_interval_minutes=60 WHERE engine='http'")
    await set_setting(db, "pulse.on_demand", True)
    for cell in cells:
        issue_token(database, provider=cell.provider, product=cell.product, region=cell.region)


def _starts(database) -> dict[str, list[datetime]]:
    """Each droplet's run starts, in order."""
    rows = query(
        database,
        "SELECT r.slug,b.start_at FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "JOIN nodes n USING(node_id) JOIN runner_regions r ON r.region_id=n.region_id "
        "ORDER BY 1,2",
    )
    found: dict[str, list[datetime]] = {}
    for slug, start in rows:
        found.setdefault(slug, []).append(start)
    return found


async def test_350_runs_on_8_budgeted_droplets_all_start_and_finish_in_the_window(database, db):
    cells = _hour_cells()
    countries = sum(len(cell.countries) for cell in cells)
    assert len(cells) == 8 and 170 <= countries <= 180
    await _ready(database, db, cells)
    opened = await pulse_late.open_late(db, _clock(HOUR + GATE), HOUR, cells, {"proxy-cheap"})
    assert opened["step_seconds"] == 30 and opened["runs"] == 2 * countries
    starts = _starts(database)
    every = [start for chain in starts.values() for start in chain]
    assert len(every) == 2 * countries
    assert min(every) == HOUR + GATE + timedelta(seconds=15)  # pulse.lead_seconds
    assert max(every) < HOUR + pulse_late.LAST_START
    assert max(every) + RUN < HOUR + pulse_late.FINISH_BY
    for chain in starts.values():  # one run every 15 s per droplet, the targets alternating
        assert {b - a for a, b in zip(chain, chain[1:], strict=False)} <= {timedelta(seconds=15)}


async def test_connection_use_per_droplet_never_exceeds_the_networks_share(database, db):
    cells = _hour_cells()
    await _ready(database, db, cells)
    await pulse_late.open_late(db, _clock(HOUR + GATE), HOUR, cells, {"proxy-cheap"})
    limit = budget.share("proxy-cheap", len(cells))
    each = ConnectionPool(limit).share(25)
    assert (limit, each) == (11, 5)
    for chain in _starts(database).values():
        for start in chain:
            running = sum(1 for other in chain if other <= start < other + RUN)
            assert running <= RUNS_AT_ONCE and running * each <= limit


async def test_a_network_without_a_pulse_step_keeps_pulse_step_seconds(database, db):
    cells = [Armed("webshare", "res", "fra1", ("AT", "DE"))]
    await _ready(database, db, cells)
    opened = await pulse_late.open_late(db, _clock(HOUR + GATE), HOUR, cells, {"webshare"})
    assert "step_seconds" not in opened and opened["runs"] == 4
    begin = HOUR + GATE + timedelta(seconds=15)
    assert _starts(database)["fra1"] == [begin + timedelta(seconds=s) for s in (0, 6, 12, 18)]
    assert budget.pulse_step(["webshare", "decodo"]) is None
    assert budget.pulse_step(["webshare", "proxy-cheap"]) == timedelta(seconds=30)


async def test_the_step_is_never_faster_than_pulse_step_seconds(database, db):
    cells = [Armed("proxy-cheap", "res", "fra1", ("AT", "DE"))]
    await _ready(database, db, cells)
    await set_setting(db, "pulse.step_seconds", 40)
    await pulse_late.open_late(db, _clock(HOUR + GATE), HOUR, cells, {"proxy-cheap"})
    begin = HOUR + GATE + timedelta(seconds=15)
    assert _starts(database)["fra1"] == [begin + timedelta(seconds=s) for s in (0, 20, 40, 60)]


async def test_a_late_gate_opens_only_the_countries_whose_runs_end_before_hh52(database, db):
    cells = [cell for cell in _hour_cells() if cell.region == "fra1"]
    await _ready(database, db, cells)
    late = HOUR + timedelta(minutes=40)  # pulse_late.GATE_LATEST
    opened = await pulse_late.open_late(db, _clock(late), HOUR, cells, {"proxy-cheap"})
    chain = _starts(database)["fra1"]
    # From HH:40:15 to HH:50 at a country every 30 s: 20 http starts and 19 https; the other of
    # fra1's 41 countries wait for the next hour.
    assert opened["runs"] == len(chain) == 39
    assert chain[-1] + RUN < HOUR + pulse_late.FINISH_BY


def test_the_step_lets_the_largest_regions_last_run_finish_before_hh52():
    assert pulse_late.FINISH_BY == pulse_late.STOP - pulse_ondemand.REMOVE_LEAD
    step = budget.pulse_step(["proxy-cheap"])
    largest = max(len(region.countries) for region in load_regions().regions)  # fra1, 59
    begin = GATE + timedelta(seconds=15)
    assert begin + step * largest + RUN <= pulse_late.FINISH_BY
    assert RUN < step  # a run ends before its droplet's run after next starts (step / 2 apart)
    # The latest start leaves a step's run before FINISH_BY; a slow step starts earlier.
    assert pulse_late.last_start(step) == pulse_late.LAST_START == timedelta(minutes=50)
    assert pulse_late.last_start(timedelta(minutes=5)) == timedelta(minutes=47)
    assert pulse_late.last_start(None) == pulse_late.LAST_START
