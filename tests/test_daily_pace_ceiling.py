"""DAILY-PACE-1: a country's grammar ceiling paces its daily test under its gateway's limit.

A network whose gateway refuses above a request-rate limit of its plan records the most requests
a country holds at once, `[testing] country_max_concurrency = { CC = n }`. The country's pace
starts at most at n and records it as `ceiling`; the worker runs it as the run's grow_ceiling, so
the run never holds more in flight. The run's concurrency, and so its scope, stays the network's.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from onboarding_round_cases import make
from runner_cases import config
from test_runner_stops import in_flight_counter

from atlas import JsonSink, RunOptions, run
from atlas.daily.pace import History, country_pace
from atlas.models import ProxyConfig
from atlas.onboarding import manifest as campaign
from atlas.onboarding.scheduler import _census_config
from atlas.providers import grammar as grammars
from atlas.providers.grammar import Grammar, load_grammar
from atlas.storage_read import _scope

GRAMMARS = Path(grammars.__file__).parent / "grammars"


def parsed(**testing):
    data = tomllib.loads((GRAMMARS / "soax.toml").read_text())
    return Grammar.parse(data | {"testing": testing})


def test_the_grammar_reads_country_ceilings():
    assert dict(parsed().country_max_concurrency) == {}
    assert dict(parsed(country_max_concurrency={"US": 1, "GB": 5000}).country_max_concurrency) == {
        "US": 1,
        "GB": 5000,
    }
    for bad in ({"US": 0}, {"US": 5001}, {"US": True}, {"us": 350}, {"USA": 350}, [350], 350):
        with pytest.raises(ValueError, match="country_max_concurrency"):
            parsed(country_max_concurrency=bad)


def test_the_networks_record_their_measured_ceilings():
    # The owner's decisions of 2026-11-23: under the gateways' request-rate limits.
    assert dict(load_grammar("soax").country_max_concurrency) == {"US": 350}
    assert dict(load_grammar("massive").country_max_concurrency) == {"GB": 1000}
    assert load_grammar("soax").max_concurrency == 500  # the network's own concurrency stays
    assert load_grammar("massive").max_concurrency == 2000
    assert dict(load_grammar("shifter").country_max_concurrency) == {}


def test_a_country_with_a_ceiling_starts_at_it_and_records_it():
    history = History({"soax": {"US": 90000, "MX": 4000}, "massive": {"GB": 40000, "IE": 6000}})
    paced = country_pace("soax", {"nyc3": ["US", "MX"]}, history, 500)
    assert paced["US"] == {"concurrency": 350, "ceiling": 350, "seconds": 3600}
    assert paced["MX"] == {"concurrency": 500, "seconds": 248}  # no ceiling: as before
    paced = country_pace("massive", {"lon1": ["GB", "IE"]}, history, 2000)
    assert paced["GB"] == {"concurrency": 1000, "ceiling": 1000, "seconds": 760}  # was 440
    assert paced["IE"] == {"concurrency": 2000, "seconds": 168}
    # A network concurrency under the ceiling (a budget's share) is the bound, as before.
    assert country_pace("massive", {"lon1": ["GB"]}, history, 800)["GB"] == {
        "concurrency": 800,
        "seconds": 920,
    }


def manifest(tmp_path, pace, concurrency=500):
    return campaign.load(
        make(
            tmp_path,
            run_settings={"concurrency": concurrency},
            due_countries={"lon1": list(pace)},
            country_pace=pace,
        )
    )


def test_the_manifest_keeps_a_ceiling_from_the_pace_to_its_concurrency(tmp_path):
    data = manifest(tmp_path, {"GB": {"concurrency": 350, "ceiling": 350, "seconds": 900}})
    assert data["country_pace"]["GB"]["ceiling"] == 350
    for wrong in (349, 501):
        with pytest.raises(ValueError, match="ceiling"):
            manifest(tmp_path / str(wrong), {"GB": {"concurrency": 350, "ceiling": wrong}})


def worker_config(data, country):
    effective = dict(data["settings"])
    effective.pop("answer_classes", None)
    context = SimpleNamespace(manifest=data, effective=effective, region="lon1")
    return _census_config(context, ProxyConfig("http", "localhost", 8080), country)


def test_the_worker_runs_a_country_under_its_ceiling_in_the_same_scope(tmp_path):
    pace = {
        "GB": {"concurrency": 350, "ceiling": 350, "seconds": 900},
        "GG": {"concurrency": 5, "low_requests": 300, "low_seconds": 30, "ceiling": 20},
        "IE": {"concurrency": 500, "seconds": 300},
    }  # in the region's order
    data = manifest(tmp_path, pace)
    capped, plain, low = (worker_config(data, c) for c in ("GB", "IE", "GG"))
    assert (capped.concurrency, capped.start_concurrency, capped.grow_ceiling) == (500, 350, 350)
    assert (plain.concurrency, plain.start_concurrency, plain.grow_ceiling) == (500, 500, None)
    assert low.grow_ceiling == 20  # under its low share (500 // 12 = 41)
    labels = {"provider": "shifter", "product": "residential"}
    scope = {
        _scope({"target": "generic_http", "config": config(**labels, **c).public()})[3]
        for c in ({}, {"start_concurrency": 5, "grow_ceiling": 10})
    }
    assert len(scope) == 1  # the ceiling is a run-length setting, out of the scope


async def test_a_run_with_a_ceiling_never_grows_past_it(tmp_path, monkeypatch):
    import atlas.runner as runner

    monkeypatch.setattr(runner, "RAMP_SECONDS", 0.0)  # may grow at every answer
    distinct, peak = in_flight_counter(lambda n: f"10.0.{n // 256}.{n % 256}")
    result = await run(
        config(requests=400, concurrency=40, start_concurrency=2, grow_ceiling=10),
        JsonSink(tmp_path),
        RunOptions(transport_factory=distinct),
    )
    assert result.completed == 400 and result.unique_ips == 400
    assert peak["most"] == 10  # half of its IPs passes 40: the ceiling holds it
