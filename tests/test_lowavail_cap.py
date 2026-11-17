"""LOWAVAIL-PACE-2 (#1247): a low-availability country's daily test is capped and runs beside
the cell's other low ones.

Under 10 unique IPs (its latest test, or its census peak) a country stops after 300 requests or
30 s, whichever comes first, while it has found fewer than 10 IPs; one that finds more counts on
under today's rules. A cell runs its low countries side by side, at most 12 at once, so a dozen
tiny territories take about one country's time, not twelve.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest
from helpers import FakeTransport
from onboarding_round_cases import make, measurement
from runner_cases import config
from test_onboarding import Clock, Proxy
from test_runner_stops import in_flight_counter

from atlas import JsonSink, RunOptions, run
from atlas.daily.pace import LOW_REQUESTS, LOW_SECONDS, start
from atlas.models import Outcome, ProxyConfig
from atlas.onboarding import manifest as campaign
from atlas.onboarding.country_pace import LOW_PARALLEL
from atlas.onboarding.rounds import execute_round, round_directory, verify_round
from atlas.onboarding.scheduler import _census_config

# The 12 tiny territories of infatica lon1 on 16 Nov, 13:00 (in the region's order), and GB.
TINY = ["GG", "IM", "FO", "AD", "GI", "MR", "GM", "GW", "GN", "SL", "LR", "SH"]
LOW = {"concurrency": 5, "low_requests": LOW_REQUESTS, "low_seconds": LOW_SECONDS}


def census(**kwargs):
    return config(**{"requests": None, "stop_rule": "unique_ratio", "concurrency": 5} | kwargs)


def exits(address, pause=0.0):
    class Exits(FakeTransport):
        async def execute(self, target, *, context=None):
            await asyncio.sleep(pause)
            self.count += 1
            return Outcome(1, ip=address(self.count), status=200)

    return Exits


def test_a_country_under_10_ips_gets_the_low_cap():
    # Every pace also has its time budget (`seconds`, LOWAVAIL-PACE-3: test_lowavail_pace3.py).
    paces = [start(9, None, 2000), start(None, 9, 2000), start(0, 500, 2000)]
    assert all(pace.pop("seconds") for pace in paces) and paces == [LOW] * 3
    assert (LOW_REQUESTS, LOW_SECONDS) == (300, 30)
    # 10 IPs or more: no low cap, and at least 200 at once since LOWAVAIL-PACE-3 (5 before).
    assert start(10, None, 2000) == start(None, 11, 2000) == {"concurrency": 200, "seconds": 160}
    probe = {"concurrency": 20, "requests": 100, "seconds": 160}
    assert start(None, None, 2000) == probe


async def test_a_low_country_stops_at_300_requests(tmp_path):
    three = exits(lambda n: f"10.0.0.{n % 3}")  # 3 exits: the unique ratio stops at 1,000
    result = await run(
        census(low_requests=300, low_seconds=30),
        JsonSink(tmp_path),
        RunOptions(transport_factory=three),
    )
    assert result.stop_reason == "cap" and 300 <= result.completed < 305
    assert result.status == "completed" and result.unique_ips == 3


async def test_a_low_country_stops_at_its_time(tmp_path):
    slow = exits(lambda n: f"10.0.0.{n % 3}", pause=0.01)
    result = await run(
        census(low_requests=300, low_seconds=0.2),
        JsonSink(tmp_path),
        RunOptions(transport_factory=slow),
    )
    assert result.stop_reason == "cap" and result.completed < 300


async def test_a_low_country_that_finds_10_ips_still_counts(tmp_path):
    new = exits(lambda n: f"10.0.{n // 256}.{n % 256}")  # every answer a new exit
    result = await run(
        census(requests=600, low_requests=300, low_seconds=30),
        JsonSink(tmp_path),
        RunOptions(transport_factory=new),
    )
    assert result.stop_reason == "requests" and result.completed == result.unique_ips == 600


def test_the_low_cap_is_a_run_length_setting():
    from atlas.storage_read import _scope

    labels = {"provider": "shifter", "product": "residential"}
    plain, low = census(**labels), census(**labels, low_requests=300, low_seconds=30)
    assert "low_requests" not in plain.public() and low.public()["low_seconds"] == 30
    scope = {_scope({"target": "generic_http", "config": c.public()})[3] for c in (plain, low)}
    assert len(scope) == 1
    with pytest.raises(ValueError, match="low_requests"):
        config(requests=10, low_requests=300)
    with pytest.raises(ValueError, match="low_requests"):
        census(low_requests=0)
    with pytest.raises(ValueError, match="low_seconds"):
        census(low_seconds=-1)


def cell(tmp_path, concurrency=2000):
    pace = {c: dict(LOW) for c in TINY} | {"GB": {"concurrency": 5}}
    return make(
        tmp_path,
        run_settings={"concurrency": concurrency},
        due_countries={"lon1": ["GB", *TINY]},
        country_pace=pace,
    )


def test_the_worker_gives_a_low_country_its_cap_in_one_process(tmp_path):
    data = campaign.load(cell(tmp_path))
    effective = dict(data["settings"])
    effective.pop("answer_classes", None)
    context = SimpleNamespace(manifest=data, effective=effective, region="lon1")
    proxy = ProxyConfig("http", "localhost", 8080)
    low, big = (_census_config(context, proxy, c) for c in ("GM", "GB"))
    assert (low.low_requests, low.low_seconds, low.processes, low.requests) == (300, 30, 1, None)
    assert low.grow_ceiling == 2000 // LOW_PARALLEL  # its share beside 11 others
    assert (big.low_requests, big.low_seconds, big.processes, big.grow_ceiling) == (
        None,
        None,
        0,
        None,
    )


async def test_a_low_country_that_grows_stays_within_its_share(tmp_path, monkeypatch):
    import atlas.runner as runner

    monkeypatch.setattr(runner, "GROW_SECONDS", 0.0)
    distinct, peak = in_flight_counter(lambda n: f"10.0.{n // 256}.{n % 256}")
    # A low country's run has its low cap (LOWAVAIL-PACE-3 ramps only the others faster).
    low = census(
        requests=400, concurrency=30, start_concurrency=5, grow_ceiling=10, low_seconds=600
    )
    result = await run(low, JsonSink(tmp_path), RunOptions(transport_factory=distinct))
    assert result.unique_ips == 400 and peak["most"] == 10  # not the round's 30
    with pytest.raises(ValueError, match="grow_ceiling"):
        census(concurrency=30, start_concurrency=5, grow_ceiling=31)
    with pytest.raises(ValueError, match="grow_ceiling"):
        census(concurrency=30, start_concurrency=5, grow_ceiling=4)


@pytest.mark.parametrize(("concurrency", "width"), [(2000, LOW_PARALLEL), (10, 2)])
async def test_a_cell_of_12_tiny_countries_runs_them_side_by_side(tmp_path, concurrency, width):
    path = cell(tmp_path, concurrency)
    scheduled = campaign.moment(campaign.load(path)["rounds"][3]).timestamp()
    clock, runs, running = Clock(scheduled), [], {"now": 0, "most": 0}
    tiny_started, started = [], asyncio.Condition()

    async def country(config, sink, options):
        code, began = config.requested_country.upper(), clock.now()
        running["now"] += 1
        running["most"] = max(running["most"], running["now"])
        await options.observation_consumer("203.0.113.1", options.wall_clock())
        if code in TINY:
            # Every country of a wave (`width` tiny ones in start order) starts before the first
            # ends: each waits for its wave, not a 0.1 s sleep a loaded runner outlasted (7
            # waves of 30 s, not 6). A wave that never fills ends at the limit and fails `most`.
            wave_end = min((len(tiny_started) // width + 1) * width, len(TINY))
            async with started:
                tiny_started.append(code)
                started.notify_all()
                with contextlib.suppress(TimeoutError):
                    full = started.wait_for(lambda: len(tiny_started) >= wave_end)
                    await asyncio.wait_for(full, 30)
        running["now"] -= 1
        clock.value = max(clock.value, began + (LOW_SECONDS if code in TINY else 600))
        runs.append((code, began, clock.value))
        return measurement(1)

    state = await execute_round(path, "lon1", 3, Proxy, clock=clock, run_country=country)
    assert [j["status"] for j in state["jobs"]] == ["completed"] * 13
    assert running["most"] == width
    tiny = [r for r in runs if r[0] in TINY]
    # 12 one after another took 12 x 30 s; side by side, 12 / width rounds of 30 s.
    assert max(r[2] for r in tiny) - scheduled == LOW_SECONDS * (len(TINY) // width)
    assert runs[-1][0] == "GB" and runs[-1][1] == max(r[2] for r in tiny)  # then the others
    assert verify_round(path, "lon1", 3)["complete"]


async def test_verify_still_flags_overlapping_countries_that_are_not_low(tmp_path):
    pace = {"GB": {"concurrency": 5}, "IE": {"concurrency": 5}, "AD": dict(LOW)}
    path = make(tmp_path, due_countries={"lon1": ["GB", "IE", "AD"]}, country_pace=pace)
    clock = Clock(campaign.moment(campaign.load(path)["rounds"][4]).timestamp())

    async def country(config, sink, options):
        await options.observation_consumer("203.0.113.1", options.wall_clock())
        clock.value += 600
        return measurement(1)

    state = await execute_round(path, "lon1", 4, Proxy, clock=clock, run_country=country)
    assert [j["country"] for j in state["jobs"]] == ["GB", "IE", "AD"]
    assert state["jobs"][2]["finished_at"] == state["jobs"][0]["started_at"]  # AD ran first
    assert verify_round(path, "lon1", 4)["complete"]
    saved = round_directory(path, "lon1", 4) / "round.json"
    state["jobs"][1]["started_at"] = state["jobs"][0]["started_at"]
    saved.write_text(json.dumps(state))
    assert verify_round(path, "lon1", 4)["issues"][0] == "IE: overlaps the previous country"


async def wave(tmp_path, index, *, fail=False):
    """A cell of 12 tiny countries, 2 at once (concurrency 10), stopped (or one failing) once the
    first two run; returns the round's jobs' statuses and what it raised."""
    path, stop, both, started = cell(tmp_path, 10), asyncio.Event(), asyncio.Event(), []
    scheduled = campaign.moment(campaign.load(path)["rounds"][index]).timestamp()

    async def country(config, sink, options):
        started.append(config.requested_country.upper())
        if len(started) == 2:
            both.set()
        await both.wait()
        if fail and started[0] == config.requested_country.upper():
            raise RuntimeError("provider library failure")
        if not fail:
            stop.set()  # a stop while the wave runs (the budget or cell stop)
        await options.stop.wait()
        return measurement(0, status="stopped", completed=0, total_errors=0, cancelled=0)

    raised = None
    try:
        await execute_round(
            path, "lon1", index, Proxy, clock=Clock(scheduled), stop=stop, run_country=country
        )
    except RuntimeError as error:
        raised = error
    saved = json.loads((round_directory(path, "lon1", index) / "round.json").read_text())
    return saved, raised


async def test_a_stop_during_a_wave_interrupts_its_countries_and_skips_the_rest(tmp_path):
    saved, raised = await wave(tmp_path, 5)
    assert raised is None and saved["status"] == "stopped"
    jobs = {j["country"]: j["status"] for j in saved["jobs"]}
    assert [c for c, s in jobs.items() if s == "interrupted"] == TINY[:2]
    assert {s for c, s in jobs.items() if c not in TINY[:2]} == {"not_started_deadline"}


async def test_an_error_in_a_wave_interrupts_the_others_and_is_raised(tmp_path):
    saved, raised = await wave(tmp_path, 6, fail=True)
    assert isinstance(raised, RuntimeError) and saved["status"] == "failed"
    jobs = {j["country"]: j["status"] for j in saved["jobs"]}
    assert (jobs[TINY[0]], jobs[TINY[1]]) == ("failed", "interrupted")
    # The failed country's slot may start the next one before the others are cancelled.
    rest = {s for c, s in jobs.items() if c not in TINY[:2]}
    assert "not_started_deadline" in rest and rest <= {"interrupted", "not_started_deadline"}
