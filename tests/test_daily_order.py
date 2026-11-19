"""DAILY-ORDER-1: a daily cell runs its small countries before its big one.

On 18 Nov 2026 at 21:00 Massive's London cell ran GB first (about 80,000 requests in 39 s); the
gateway then refused the tunnels of GG, IM, JE, IE, FO, AD, GI and PT run after it. A cell now
runs its countries that are not low availability one at a time, the smallest pace first.
"""

from __future__ import annotations

from onboarding_round_cases import make, measurement
from test_onboarding import Clock, Proxy

from atlas.daily.pace import LOW_REQUESTS, LOW_SECONDS
from atlas.onboarding import manifest as campaign
from atlas.onboarding.country_pace import smallest_first
from atlas.onboarding.rounds import execute_round, verify_round

DUE = ["GB", "GG", "IM", "JE", "IE", "FO", "AD", "GI", "PT"]
LOW = {"concurrency": 5, "low_requests": LOW_REQUESTS, "low_seconds": LOW_SECONDS}
PACE = {"GB": 2000, "GG": 200, "IM": 200, "JE": 200, "IE": 900, "AD": 200, "GI": 200, "PT": 600}


def london(tmp_path):
    pace = {code: {"concurrency": n, "seconds": 300} for code, n in PACE.items()}
    return make(
        tmp_path,
        run_settings={"concurrency": 2000},
        due_countries={"lon1": DUE},
        country_pace=pace | {"FO": dict(LOW)},
    )


def jobs(*countries):
    return [{"country": code} for code in countries]


def test_the_smallest_pace_runs_first_and_equal_paces_keep_the_manifest_order():
    manifest = {
        "settings": {"concurrency": 1000},
        "country_pace": {code: {"concurrency": n} for code, n in PACE.items()},
    }
    ordered = smallest_first(manifest, jobs(*DUE))
    assert [j["country"] for j in ordered] == [
        "GG", "IM", "JE", "AD", "GI", "PT", "IE", "FO", "GB"
    ]  # fmt: skip
    # A country without a pace runs at the manifest's concurrency (FO: 1000, after IE's 900).
    without = {"settings": {"concurrency": 1000}}
    assert smallest_first(without, jobs(*DUE)) == jobs(*DUE)  # no paces: the manifest's order


async def test_a_daily_cell_runs_its_big_country_last(tmp_path):
    path = london(tmp_path)
    scheduled = campaign.moment(campaign.load(path)["rounds"][3]).timestamp()
    clock, ran = Clock(scheduled), []

    async def country(config, sink, options):
        ran.append(config.requested_country.upper())
        await options.observation_consumer("203.0.113.1", options.wall_clock())
        clock.value += 60
        return measurement(1)

    state = await execute_round(path, "lon1", 3, Proxy, clock=clock, run_country=country)
    # The low country first, then the others smallest first; GB, the biggest, last.
    assert ran == ["FO", "GG", "IM", "JE", "AD", "GI", "PT", "IE", "GB"]
    # The same countries and runs: the checkpoint keeps the manifest's order.
    assert [j["country"] for j in state["jobs"]] == DUE
    assert [j["status"] for j in state["jobs"]] == ["completed"] * len(DUE)
    report = verify_round(path, "lon1", 3)
    assert report["complete"], report["issues"]
    assert [c["country"] for c in report["countries"]] == DUE
    assert report["rows"] == len(DUE)
