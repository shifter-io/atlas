"""LOWAVAIL-PACE-3 (#1290): the daily pace no longer slows real (not tiny) countries.

On 16 Nov at 21:00 iproyal blr1's 15 countries took 2,170 s (227 s the day before): at half their
IPs its countries with 13 to 44 IPs ran at 6 to 22 at once, and each needed its minimum sample
(1,000 answers) at about 3 s per answer. Now a country with 10 IPs or more starts at least at
200 at once and ramps by doubling; every country has a time budget (its run's cap_seconds); a
daily round starts no country from HH:25 on (those are not reached, never failed). Countries
under 10 IPs keep LOWAVAIL-PACE-2's pace, cap and growth.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from campaign_hour_cases import make_spec
from helpers import FakeTransport
from onboarding_round_cases import make, measurement
from runner_cases import config
from test_onboarding import START, Clock, Proxy

from atlas import RunConfig, run
from atlas.campaign.contracts import spec_path
from atlas.daily import ingest as daily_ingest
from atlas.daily.pace import (
    BUDGET_BASE,
    LOW_SECONDS,
    MIN_SAMPLE,
    REAL_FLOOR,
    History,
    budget,
    country_pace,
    start,
)
from atlas.models import ProxyConfig
from atlas.onboarding import manifest as campaign
from atlas.onboarding.rounds import SOFT_LIMIT, execute_round, verify_round
from atlas.onboarding.scheduler import COUNTRY_CAP_SECONDS, _census_config
from atlas.storage import connect, seed

pytest_plugins = ["test_storage", "test_enrichment_cli"]

# The fixture: iproyal blr1's 15 countries at 21:00, {country: (unique IPs, median answer s)},
# from their 15 Nov daily tests (production, read only): the IPs the 16 Nov tests' pace started
# from, and each country's latency (TM had no answer: no latency, 0 IPs).
BLR1 = {
    "AF": (26, 2.891),
    "BT": (5, 2.921),
    "KW": (264, 1.733),
    "OM": (16, 1.738),
    "SA": (10390, 2.252),
    "YE": (122, 4.043),
    "TM": (0, 2.9),
    "KM": (13, 3.840),
    "DJ": (44, 2.541),
    "TZ": (1009, 3.534),
    "BI": (24, 3.731),
    "SD": (22, 2.798),
    "SS": (20, 2.876),
    "YT": (29, 3.680),
    "MZ": (445, 3.513),
}


def before(ips: int) -> dict:
    """LOWAVAIL-1's pace as on 16 Nov (half the IPs, 5 to 2,000; the low cap under 10 IPs)."""
    pace = {"concurrency": min(max(int(ips * 0.5), 5), 2000)}
    return pace | ({"low_seconds": LOW_SECONDS} if ips < 10 else {})


def seconds(ips: int, latency: float, pace: dict) -> float:
    """About how long a country's test takes: a low one its low cap, the others the answers
    their unique-ratio stop needs at their pace, plus the last answers in flight."""
    if "low_seconds" in pace:
        return pace["low_seconds"] + latency
    return max(MIN_SAMPLE, 2 * ips) * latency / pace["concurrency"] + latency


def cell(rule) -> float:
    """The cell's seconds: its low countries side by side, then the others one after another."""
    paces = {code: (ips, latency, rule(ips)) for code, (ips, latency) in BLR1.items()}
    low = [seconds(*p) for p in paces.values() if "low_seconds" in p[2]]
    rest = [seconds(*p) for p in paces.values() if "low_seconds" not in p[2]]
    return max(low, default=0) + sum(rest)


def test_the_21h_iproyal_blr1_cell_takes_a_few_minutes_again():
    old = cell(before)
    assert 2000 < old < 2600  # 2,170 s measured on 16 Nov: the model holds
    new = cell(lambda ips: start(ips, None, 2000))
    assert new < 300  # 227 s at 2,000 at once on 15 Nov; about 4.6 minutes now
    for ips, latency in BLR1.values():  # no country of the cell reaches its budget
        pace = start(ips, None, 2000)
        assert seconds(ips, latency, pace) < pace["seconds"] / 2


@pytest.mark.parametrize(
    ("ips", "cap", "concurrency"),
    [
        (9, 2000, 5),  # under 10 IPs: as before (LOWAVAIL-PACE-2)
        (10, 2000, REAL_FLOOR),
        (16, 2000, REAL_FLOOR),  # OM: 8 before
        (264, 2000, REAL_FLOOR),  # KW: 132 before
        (445, 2000, 222),  # MZ: half its IPs, above the floor
        (10390, 2000, 2000),
        (16, 30, 30),  # a network's own concurrency (Proxy-Cheap) stays the ceiling
    ],
)
def test_a_real_country_starts_at_least_at_the_floor(ips, cap, concurrency):
    pace = start(ips, None, cap)
    assert pace["concurrency"] == concurrency
    assert ("low_requests" in pace) == (ips < 10)


def test_a_tiny_country_keeps_its_pace_and_cap():
    for ips in (0, 3, 9):
        pace = start(ips, None, 2000)
        assert pace.pop("seconds") == 1720  # longer than its low cap: that still ends it
        assert pace == {"concurrency": 5, "low_requests": 300, "low_seconds": 30}
    assert start(None, 4, 2000)["concurrency"] == 5  # a census peak under 10 too


@pytest.mark.parametrize(
    ("requests", "concurrency", "expected"),
    [
        (MIN_SAMPLE, REAL_FLOOR, 160),  # a small country: its minimum sample at 200
        (2 * 10390, 2000, 204),  # SA
        (2 * 441013, 2000, 3600),  # fleetproxy US (665 s on 15 Nov): the 60-minute cap
        (MIN_SAMPLE, 30, 387),  # Proxy-Cheap at 30: its 335 s of 15 Nov fit
        (MIN_SAMPLE, 5, 1720),  # a tiny one, whose low cap ends it at 30 s anyway
        (100, 20, 160),  # the probe
    ],
)
def test_the_time_budget_is_about_twice_what_the_rule_needs(requests, concurrency, expected):
    assert budget(requests, concurrency) == {"seconds": expected}
    assert BUDGET_BASE <= expected <= COUNTRY_CAP_SECONDS


def test_every_country_due_gets_its_budget_and_the_worker_its_cap(tmp_path):
    history = History({"iproyal": {"OM": 16, "BT": 5}})
    paces = country_pace("iproyal", {"blr1": ["OM", "BT", "XK"]}, history, 2000)
    assert {code: pace["seconds"] for code, pace in paces.items()} == {
        "OM": 160,
        "BT": 1720,
        "XK": 160,
    }
    pace = {
        "CA": {"concurrency": 200, "seconds": 160},
        "PM": {"concurrency": 200, "seconds": 99999},
    }
    path = make(tmp_path, due_countries={"tor1": ["CA", "PM"]}, country_pace=pace)
    data = campaign.load(path)
    effective = dict(data["settings"])
    effective.pop("answer_classes", None)
    context = SimpleNamespace(manifest=data, effective=effective, region="tor1")
    proxy = ProxyConfig("http", "localhost", 8080)
    assert _census_config(context, proxy, "CA").cap_seconds == 160.0
    assert _census_config(context, proxy, "PM").cap_seconds == COUNTRY_CAP_SECONDS  # at most
    with pytest.raises(ValueError, match="country_pace seconds"):
        bad = {"CA": {"concurrency": 200, "seconds": 0}}
        campaign.validate(dict(data) | {"country_pace": bad})


def state_of(**settings):
    pace = config(concurrency=2000, start_concurrency=200, **settings)
    return SimpleNamespace(
        gate=asyncio.Semaphore(200),
        allowed=200,
        grown_at=100.0,
        unique={f"10.0.{n // 256}.{n % 256}" for n in range(3000)},
        config=pace,
    )


def test_a_real_country_doubles_every_few_seconds_up_to_half_its_ips():
    import atlas.runner as runner

    state = state_of()
    runner._grow(state, 100.0 + runner.RAMP_SECONDS - 0.1)
    assert state.allowed == 200  # too soon
    for step, expected in enumerate((400, 800, 1500, 1500), start=1):
        runner._grow(state, 100.0 + step * runner.RAMP_SECONDS)
        assert state.allowed == expected and state.gate._value == expected  # half of 3,000
    assert runner.RAMP_SECONDS < runner.GROW_SECONDS


def test_a_tiny_country_still_grows_straight_to_half_its_ips():
    import atlas.runner as runner

    state = state_of(stop_rule="unique_ratio", requests=None, low_seconds=30)
    runner._grow(state, 100.0 + runner.RAMP_SECONDS)
    assert state.allowed == 200  # its own GROW_SECONDS, as before
    runner._grow(state, 100.0 + runner.GROW_SECONDS)
    assert state.allowed == 1500  # no doubling limit


async def daily_round(tmp_path, seconds, *, daily=True):
    """A tor1 round of CA, GL, PM whose countries take `seconds` each (the fake clock)."""
    due = {"due_countries": {"tor1": ["CA", "GL", "PM"]}} if daily else {}
    path = make(tmp_path, **due)
    scheduled = campaign.moment(campaign.load(path)["rounds"][0]).timestamp()
    clock, ran = Clock(scheduled), []

    async def country(config, sink, options):
        ran.append(config.requested_country.upper())
        await options.observation_consumer("203.0.113.1", options.wall_clock())
        clock.value += seconds
        return measurement(1)

    state = await execute_round(path, "tor1", 0, Proxy, clock=clock, run_country=country)
    return path, state, ran


async def test_a_daily_round_starts_no_country_after_hh25(tmp_path):
    path, state, ran = await daily_round(tmp_path, SOFT_LIMIT - 60)
    jobs = {j["country"]: j["status"] for j in state["jobs"]}
    regional = [c for c in campaign.region_countries(campaign.load(path), "tor1") if c in jobs]
    assert ran == regional[:2] and state["status"] == "completed"  # the 2nd started at HH:24
    assert list(jobs.values()).count("not_reached") == 1
    verified = verify_round(path, "tor1", 0)
    assert verified["complete"] and verified["issues"] == []  # not reached, never failed


async def test_a_round_that_is_not_a_daily_test_has_no_soft_limit(tmp_path):
    path, state, ran = await daily_round(tmp_path, SOFT_LIMIT, daily=False)
    assert len(ran) == len(state["jobs"]) > 1
    assert {j["status"] for j in state["jobs"]} == {"completed"}


async def test_the_import_names_the_countries_the_soft_limit_left(
    tmp_path, monkeypatch, database, fake_database
):
    import atlas.runner as runner

    seed(database)
    monkeypatch.setattr(runner.time, "time", lambda: START.timestamp() + 1)
    root = tmp_path / "daily"
    path = make(root, due_countries={"tor1": ["CA", "GL", "PM"]})
    spec = make_spec(
        networks=("shifter",),
        regions=("tor1",),
        manifests={"shifter": path.relative_to(root).as_posix()},
        starts_at=campaign.timestamp(START),
    )
    spec_path(root).parent.mkdir(parents=True, exist_ok=True)
    spec_path(root).write_text(spec.to_json())
    clock = Clock(START.timestamp())

    async def measure(config, sink, options):
        config = RunConfig("generic_http", config.proxy, requests=3, concurrency=1)
        options = replace(options, deadline=None, transport_factory=FakeTransport)
        result = await run(config, sink, options)
        clock.value += SOFT_LIMIT  # the first country runs to HH:25
        return result

    await execute_round(path, "tor1", 0, Proxy, clock=clock, run_country=measure)
    mmdb, _ = fake_database
    report = await daily_ingest.ingest_campaign(root, database, mmdb)
    tor1 = report["cells"]["shifter/tor1"]
    assert (tor1["status"], tor1["issues"], tor1["ingested"]) == ("done", [], ["CA"])
    assert tor1["not_reached"] == ["GL", "PM"] and report["complete"] is True
    with connect(database) as conn:
        finished = "SELECT data FROM test_events WHERE kind='daily_test_finished'"
        (data,) = conn.execute(finished).fetchone()
    assert data["status"] == "done" and data["not_reached"] == ["GL", "PM"]
