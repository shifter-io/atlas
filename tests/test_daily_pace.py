"""LOWAVAIL-1 (#1181) step 3: a country's daily test pace follows its size.

Half of the unique IPs its latest daily test found, at least 5 and at most 2,000 at once, never
above the network's concurrency; the October census before its first test; a 100-request probe
at 20 at once for a country neither tested nor in the census. The pace goes into the hour's
manifest (`country_pace`) and the worker's run config for the country.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from onboarding_round_cases import make
from test_country_verdicts import daily_test

from atlas.daily import pace
from atlas.daily.pace import History, country_pace, start
from atlas.models import ProxyConfig
from atlas.onboarding import manifest as campaign
from atlas.onboarding.scheduler import COUNTRY_CAP_SECONDS, _census_config
from atlas.storage import ingest

pytest_plugins = ["test_storage"]


@pytest.mark.parametrize(
    ("latest", "census", "cap", "expected"),
    [
        (10, None, 2000, {"concurrency": 5}),
        (400, 9, 2000, {"concurrency": 200}),  # the latest test, not the census
        (4000, None, 2000, {"concurrency": 2000}),
        (90000, None, 2000, {"concurrency": 2000}),
        (3, None, 2000, {"concurrency": 5}),
        (0, 500, 2000, {"concurrency": 5}),  # its 300-answer stop ends it: runner.ZERO_IPS_STOP
        (None, 11, 2000, {"concurrency": 5}),  # Tonga's census peak
        (None, 212191, 2000, {"concurrency": 2000}),
        (None, 212191, 500, {"concurrency": 500}),  # a network's own ceiling (Proxy-Cheap)
        (None, None, 2000, {"concurrency": 20, "requests": 100}),  # the probe
        (None, None, 10, {"concurrency": 10, "requests": 100}),  # a budget's share
        (400, None, 30, {"concurrency": 30}),
    ],
)
def test_the_pace_is_half_the_ips_between_5_and_2000(latest, census, cap, expected):
    assert start(latest, census, cap) == expected


def test_the_countries_due_get_their_pace():
    history = History({"shifter": {"GB": 4000, "TV": 0}}, {"shifter": {"TV": 2, "WF": 1}})
    due = {"lon1": ["GB"], "syd1": ["TV", "WF", "XK"]}
    assert country_pace("shifter", due, history, 2000) == {
        "GB": {"concurrency": 2000},
        "TV": {"concurrency": 5},
        "WF": {"concurrency": 5},
        "XK": {"concurrency": 20, "requests": 100},
    }
    assert country_pace("evomi", {"lon1": ["GB"]}, history, 2000) == {
        "GB": {"concurrency": 20, "requests": 100}
    }


def formula(directory, network, countries):
    body = {"network": network, "schedule": {c: 0 for c in countries}}
    body["countries"] = {c: {"unique_ips": hours} for c, hours in countries.items()}
    (directory / f"{network}.json").write_text(json.dumps(body))


async def test_the_history_reads_the_latest_daily_tests(database, tmp_path):
    folder = tmp_path / "formulas"
    folder.mkdir()
    formula(folder, "fixture", {"DE": [400, 900], "FR": [3]})
    (folder / "geonode.json").write_text(json.dumps({"network": "geonode", "schedule": {"GB": 1}}))
    for ips in (12, 30):  # the second test of DE is the latest
        assert ingest(await daily_test(tmp_path, ips, "de"), database)["imported"]
    assert ingest(await daily_test(tmp_path, 6, "fr"), database)["imported"]
    found = pace.history(folder, database, datetime(2999, 1, 1, tzinfo=UTC))
    assert found.latest == {"fixture": {"DE": 30, "FR": 6}}
    assert found.census == {"fixture": {"DE": 900, "FR": 3}, "geonode": {}}
    early = pace.history(folder, database, datetime(2000, 1, 1, tzinfo=UTC))
    assert early.latest == {} and early.census == found.census
    assert pace.history(folder, None, datetime(2999, 1, 1, tzinfo=UTC)).latest == {}


PACE = {"CA": {"concurrency": 5}, "PM": {"concurrency": 20, "requests": 100}}


def test_the_manifest_keeps_the_pace_of_the_countries_due(tmp_path):
    data = campaign.load(make(tmp_path, due_countries={"tor1": ["CA", "PM"]}, country_pace=PACE))
    assert data["country_pace"] == PACE
    plain = campaign.load(make(tmp_path / "plain", due_countries={"tor1": ["CA"]}))
    assert "country_pace" not in plain


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"DE": {"concurrency": 5}},  # not due
        {"CA": {"requests": 100}},  # no concurrency
        {"CA": {"concurrency": 5, "stall_seconds": 5}},
        {"CA": {"concurrency": 0}},
        {"CA": {"concurrency": 5.0}},
        {"CA": {"concurrency": 2001}},  # above the manifest's 2000
        {"CA": 5},
        [],
    ],
)
def test_a_bad_pace_is_refused(tmp_path, bad):
    data = campaign.load(make(tmp_path, due_countries={"tor1": ["CA", "PM"]}))
    with pytest.raises(ValueError, match="country_pace"):
        campaign.validate(deepcopy(data) | {"country_pace": bad})
    whole = campaign.load(make(tmp_path / "whole"))
    with pytest.raises(ValueError, match="country_pace"):
        campaign.validate(deepcopy(whole) | {"country_pace": {"CA": {"concurrency": 5}}})


def test_the_worker_runs_each_country_at_its_pace(tmp_path):
    data = campaign.load(make(tmp_path, due_countries={"tor1": ["CA", "PM"]}, country_pace=PACE))
    effective = dict(data["settings"])
    effective.pop("answer_classes", None)
    context = SimpleNamespace(manifest=data, effective=effective, region="tor1")
    proxy = ProxyConfig("http", "localhost", 8080)
    small, probe = (_census_config(context, proxy, c) for c in ("CA", "PM"))
    # The manifest's concurrency stays the run's (and its scope's); the pace limits it.
    assert (small.concurrency, small.start_concurrency, small.requests) == (2000, 5, None)
    assert (probe.concurrency, probe.start_concurrency, probe.requests) == (2000, 20, 100)
    assert small.stop_rule == "unique_ratio" and small.cap_seconds == COUNTRY_CAP_SECONDS
    assert small.public()["concurrency"] == 2000 and small.public()["start_concurrency"] == 5
    plain = campaign.load(make(tmp_path / "plain", due_countries={"tor1": ["CA"]}))
    context = SimpleNamespace(manifest=plain, effective=effective, region="tor1")
    config = _census_config(context, proxy, "CA")
    assert (config.concurrency, config.start_concurrency, config.requests) == (2000, None, None)
    assert "start_concurrency" not in config.public()  # as before: the same scope
