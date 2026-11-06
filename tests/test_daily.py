"""DAILY-1: the hourly daily test, offline: the planner and the manifest's `due_countries`.

The planner, the manifest's `due_countries` and the launcher's skip of regions with nothing due;
the runner is in `test_daily_runner.py`, the ingest in `test_daily_ingest.py`. No droplet,
request or database outside the test is touched.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fakes import FakeDigitalOcean
from campaign_hour_cases import launched, make_root, make_spec
from daily_cases import formulas, hours, template
from onboarding_round_cases import TOR1, make

from proxy_index.cli import main
from proxy_index.daily.planner import Cell, load_schedules, plan_hour
from proxy_index.daily.runner import RunError, start_hour
from proxy_index.onboarding import manifest as campaign
from proxy_index.onboarding.rounds import new_round

# --- planner -------------------------------------------------------------------------------


def test_the_hour_groups_due_countries_into_one_droplet_per_network_and_region(tmp_path):
    plan = plan_hour(load_schedules(formulas(tmp_path)), 5)
    assert plan.cells == (
        Cell("geonode", "lon1", ("GB", "FR")),
        Cell("shifter", "fra1", ("DE",)),
        Cell("shifter", "tor1", ("CA", "PM")),
    )
    assert [c.region_id for c in plan.cells] == [1, 2, 5]
    assert plan.networks() == ("geonode", "shifter")
    assert plan.regions() == ("lon1", "fra1", "tor1")
    assert plan.due("shifter") == {"fra1": ["DE"], "tor1": ["CA", "PM"]}
    assert plan.to_dict()["droplets"] == 3 and plan.to_dict()["skipped"] == []
    assert [c.region for c in plan_hour(load_schedules(formulas(tmp_path)), 6).cells] == ["nyc3"]


def test_networks_the_runner_cannot_test_are_skipped_and_an_empty_hour_has_no_droplet(tmp_path):
    schedules = load_schedules(formulas(tmp_path))
    plan = plan_hour(schedules, 5, networks=("shifter",))
    assert plan.networks() == ("shifter",) and plan.skipped == ("geonode",)
    empty = plan_hour(schedules, 12)
    assert empty.cells == () and empty.to_dict()["droplets"] == 0


@pytest.mark.parametrize(
    "body, message",
    [
        ({"network": "x"}, "needs a network and a schedule"),
        ({"network": "x", "schedule": {}}, "needs a network and a schedule"),
        ({"network": "x", "schedule": {"de": 1}}, "invalid country code"),
        ({"network": "x", "schedule": {"DE": 24}}, "test hour 0..23"),
        ({"network": "x", "schedule": {"DE": True}}, "test hour 0..23"),
    ],
)
def test_bad_formulas_are_refused(tmp_path, body, message):
    (tmp_path / "x.json").write_text(json.dumps(body))
    with pytest.raises(ValueError, match=message):
        load_schedules(tmp_path)


def test_a_network_with_two_formulas_or_an_unknown_country_is_refused(tmp_path):
    for name in ("a", "b"):
        body = {"network": "x", "schedule": {"DE": 1}}
        (tmp_path / f"{name}.json").write_text(json.dumps(body))
    with pytest.raises(ValueError, match="two formulas"):
        load_schedules(tmp_path)
    with pytest.raises(ValueError, match="countries in no region: ZZ"):
        plan_hour({"x": {"ZZ": 1}}, 1)
    with pytest.raises(ValueError, match="hour must be 0..23"):
        plan_hour({}, 24)


def test_start_hour():
    now = datetime(2027, 1, 1, 21, 58, 30, tzinfo=UTC)
    hour = datetime(2027, 1, 1, 21, tzinfo=UTC)
    assert start_hour("now-hour", now) == (hour, True)
    assert start_hour("next-hour", now) == (hour + timedelta(hours=1), False)
    assert start_hour("2027-01-02T01:00:00+04:00", now) == (hour, True)
    assert start_hour("2027-01-01T22:00:00Z", now) == (hour + timedelta(hours=1), False)
    with pytest.raises(RunError, match="explicit UTC offset"):
        start_hour("2027-01-01T22:00:00", now)


def test_plan_cli_prints_the_droplet_plan_and_writes_nothing(tmp_path, capsys):
    folder = formulas(tmp_path)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    code = main(["daily", "plan", "--formulas", str(folder), "--start", "2027-01-01T05:00:00Z"])
    plan = json.loads(capsys.readouterr().out)
    assert code == 0 and plan["droplets"] == 3 and plan["starts_at"].startswith("2027-01-01T05")
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


# --- the manifest's due_countries ----------------------------------------------------------


def test_due_countries_limit_the_round_to_the_countries_due(tmp_path):
    path = make(tmp_path, due_countries={"tor1": ["CA", "PM"]})
    data = campaign.load(path)
    assert campaign.region_countries(data, "tor1") == ["CA", "PM"]
    assert campaign.region_countries(data, "fra1") == []
    assert [job["country"] for job in new_round(data, "tor1", 0)["jobs"]] == ["CA", "PM"]
    whole = campaign.load(make(tmp_path / "whole"))
    assert campaign.region_countries(whole, "tor1") == TOR1


@pytest.mark.parametrize(
    "bad",
    [{}, {"tor1": []}, {"xyz1": ["CA"]}, {"tor1": ["PM", "CA"]}, {"tor1": ["DE"]}, {"tor1": "CA"}],
)
def test_bad_due_countries_are_refused(tmp_path, bad):
    data = campaign.load(make(tmp_path))
    with pytest.raises(ValueError, match="due_countries"):
        campaign.validate(deepcopy(data) | {"due_countries": bad})


def test_due_countries_are_ephemeral_only(tmp_path):
    with pytest.raises(ValueError, match="due_countries"):
        make(tmp_path, execution_mode="parallel", due_countries={"tor1": ["CA"]})


async def test_the_launch_creates_no_worker_for_a_region_with_nothing_due(tmp_path):
    spec = make_spec()  # net00, net01 in lon1, fra1
    root = make_root(tmp_path, spec)
    path = root / spec.manifests["net00"]
    path.write_text(json.dumps(json.loads(path.read_text()) | {"due_countries": {"lon1": ["GB"]}}))
    fake = FakeDigitalOcean()
    record, _, remote, _ = await launched(tmp_path, fake, spec)
    workers = sorted((d.network, d.region) for d in record.droplets)
    assert workers == [("net00", "lon1"), ("net01", "fra1"), ("net01", "lon1")]
    assert record.traffic["net00"]["countries_total"] == 1
    assert record.traffic["net01"]["countries_total"] == 4
    assert len(remote.pushes) == 3


# --- the plan CLI with a template ----------------------------------------------------------


def test_plan_cli_with_a_template_names_the_networks_it_cannot_test(tmp_path, capsys):
    root = template(tmp_path)
    folder = formulas(tmp_path, {"shifter": {"GB": 5}, "geonode": {"DE": 5}})
    args = ["daily", "plan", "--formulas", str(folder), "--template", str(root)]
    assert main(args + ["--start", "2027-01-01T05:00:00+00:00"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["droplets"] == 1 and plan["skipped"] == ["geonode"]
    assert plan["cells"] == [{"network": "shifter", "region": "lon1", "countries": ["GB"]}]
    assert hours(root) == []  # nothing written beside the template
