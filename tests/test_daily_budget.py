"""PROXY-CHEAP-BUDGET-1: a budgeted network's daily droplets share its connections; others stay.

Offline: the planner, the runner's hour campaign against fake services and the CLI.
"""

from __future__ import annotations

import json

import pytest
from daily_cases import NOW, formulas, hours, inputs, template

from atlas.campaign.collector import Pacing
from atlas.campaign.tick import load_spec, save_hour
from atlas.cli import main
from atlas.daily import budget
from atlas.daily.hourly import check
from atlas.daily.planner import plan_hour
from atlas.daily.runner import RunError, run_hour
from atlas.onboarding import manifest
from atlas.regions import load_regions


def _countries(*counts: int) -> dict[str, int]:
    """`counts[i]` countries of the i-th region, all due at hour 5."""
    regions = load_regions().regions
    pairs = zip(regions, counts, strict=False)
    return {code: 5 for region, n in pairs for code in region.countries[:n]}


def test_a_busy_hour_never_exceeds_the_budget_and_leaves_other_networks_out():
    busy = _countries(3, 2, 2, 2)  # 9 countries in 4 regions
    plan = plan_hour({"proxy-cheap": busy, "decodo": _countries(5, 5)}, 5)
    found = budget.daily(plan, {"proxy-cheap": 500})
    assert found == {"proxy-cheap": {"droplets": 4, "concurrency": 22, "total": 88, "budget": 100}}
    assert found["proxy-cheap"]["total"] + budget.BROWSER_RESERVE <= 100


def test_proxy_cheaps_budget_is_100_with_30_per_droplet():
    assert budget.budgets()["proxy-cheap"] == 100 and budget.caps()["proxy-cheap"] == 30


def test_the_16_utc_hour_of_three_droplets_runs_30_each_not_80():
    """PROXY-CHEAP-BUDGET-2: 3 droplets x 80 = 240 answered 19%; now 3 x 30 = 90."""
    plan = plan_hour({"proxy-cheap": _countries(3, 3, 3)}, 5)
    found = budget.daily(plan, {"proxy-cheap": 500})["proxy-cheap"]
    assert found == {"droplets": 3, "concurrency": 30, "total": 90, "budget": 100}


@pytest.mark.parametrize(("runs", "each"), [(1, 30), (3, 30), (4, 22), (6, 15), (8, 11), (18, 5)])
def test_each_run_gets_its_floor_share_less_the_reserve_at_most_its_droplet_cap(runs, each):
    assert budget.share("proxy-cheap", runs) == each and runs * each <= 90


def test_an_hour_with_one_daily_droplet_gets_its_droplet_cap_at_most_the_template():
    plan = plan_hour({"proxy-cheap": _countries(4)}, 5)
    assert budget.daily(plan, {"proxy-cheap": 500})["proxy-cheap"]["concurrency"] == 30
    assert budget.daily(plan, {"proxy-cheap": 20})["proxy-cheap"]["concurrency"] == 20


def test_a_network_without_a_droplet_cap_gets_its_whole_share(monkeypatch):
    monkeypatch.setattr(budget, "CONNECTION_BUDGET", {"shifter": 250})
    assert budget.share("shifter", 1) == 240 and budget.share("proxy-cheap", 1) is None


def test_a_share_below_the_minimum_is_refused_and_unlisted_networks_have_none():
    with pytest.raises(budget.BudgetError, match="19 runs at once get 4 connections"):
        budget.share("proxy-cheap", 19)
    assert budget.share("decodo", 3) is None and budget.share("proxy-cheap", 0) is None


def _finished(hour_root, spec):
    async def launcher(spec, record, root):
        record.state, record.finished_at = "done", NOW.isoformat()
        save_hour(root, record)

    async def harvester(*args, **kwargs):  # pragma: no cover - the hour is done at launch
        raise AssertionError("nothing to harvest")

    return None, None, launcher, harvester


async def _hour(tmp_path, root, **schedule):
    pacing = Pacing(interval=0, clock_synced=lambda: True)
    found = inputs(tmp_path, root, **schedule)
    outcome = await run_hour(found, services=_finished, now=NOW, pacing=pacing)
    spec = load_spec(root.parent / outcome["campaign_id"])
    hour_root = root.parent / outcome["campaign_id"]
    return outcome, manifest.load(hour_root / spec.manifests["shifter"])


async def test_the_hour_manifest_runs_a_budgeted_network_at_its_share(tmp_path, monkeypatch):
    root = template(tmp_path)
    before, unbudgeted = await _hour(tmp_path, root, GB=22, DE=22)
    assert "budget" not in before and unbudgeted["settings"]["concurrency"] == 2000
    monkeypatch.setattr(budget, "CONNECTION_BUDGET", {"shifter": 250})
    outcome, hour = await _hour(tmp_path / "b", template(tmp_path / "b"), GB=22, DE=22)
    entry = {"droplets": 2, "concurrency": 120, "total": 240, "budget": 250}
    assert outcome["state"] == "done" and outcome["budget"] == {"shifter": entry}
    assert hour["settings"] == unbudgeted["settings"] | {"concurrency": 120}
    assert hour["due_countries"] == unbudgeted["due_countries"]


def test_a_budgeted_networks_country_makes_at_most_its_request_cap():
    """DAILY-BUDGET-STOP-1: proxy-cheap's big countries end at 15,000 requests (about 20 minutes
    at its share); a probe keeps its own smaller limit; other networks are unchanged."""
    paced = {"BR": {"concurrency": 30}, "TV": {"concurrency": 20, "requests": 100}}
    assert budget.COUNTRY_REQUESTS == {"proxy-cheap": 15_000}
    assert budget.capped("proxy-cheap", paced) == {
        "BR": {"concurrency": 30, "requests": 15_000},
        "TV": {"concurrency": 20, "requests": 100},
    }
    assert budget.capped("shifter", paced) is paced


async def test_the_hour_manifest_caps_a_budgeted_networks_countries(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "CONNECTION_BUDGET", {"shifter": 250})
    monkeypatch.setattr(budget, "COUNTRY_REQUESTS", {"shifter": 50})  # under the probe's 100
    _, hour = await _hour(tmp_path, template(tmp_path), GB=22, DE=22)
    assert sorted(hour["country_pace"]) == ["DE", "GB"]
    assert {pace["requests"] for pace in hour["country_pace"].values()} == {50}


async def test_a_budget_of_another_network_leaves_the_hour_byte_identical(tmp_path, monkeypatch):
    root = template(tmp_path)
    plain, plain_manifest = await _hour(tmp_path, root, GB=22)
    monkeypatch.setattr(budget, "CONNECTION_BUDGET", {"proxy-cheap": 250, "geonode": 100})
    other, other_manifest = await _hour(tmp_path / "b", template(tmp_path / "b"), GB=22)
    assert json.dumps(other["plan"]) == json.dumps(plain["plan"]) and "budget" not in other
    ids = ("campaign_id", "created_at")
    assert {k: v for k, v in other_manifest.items() if k not in ids} == {
        k: v for k, v in plain_manifest.items() if k not in ids
    }


async def test_a_budget_the_droplets_cannot_share_refuses_the_hour(tmp_path, monkeypatch):
    root = template(tmp_path)
    monkeypatch.setattr(budget, "CONNECTION_BUDGET", {"shifter": 12})  # 2 left: below 5
    with pytest.raises(RunError, match="below 5"):
        await _hour(tmp_path, root, GB=22)
    assert hours(root) == []


def test_check_lists_the_budgeted_concurrency_and_a_budget_that_does_not_fit(tmp_path, monkeypatch):
    root = template(tmp_path)
    folder = formulas(tmp_path, {"shifter": {"GB": 5, "DE": 5, "FR": 7}})
    assert "budget_by_hour" not in check(folder, root)
    monkeypatch.setattr(budget, "CONNECTION_BUDGET", {"shifter": 250})
    assert check(folder, root)["budget_by_hour"] == {"5": {"shifter": 120}, "7": {"shifter": 240}}
    monkeypatch.setattr(budget, "CONNECTION_BUDGET", {"shifter": 12})
    found = check(folder, root)
    assert not found["ok"] and found["issues"][0].startswith("hour 5: shifter: 2 runs")


def test_plan_cli_with_a_template_shows_the_budget(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(budget, "CONNECTION_BUDGET", {"shifter": 250})
    root = template(tmp_path)
    folder = formulas(tmp_path, {"shifter": {"GB": 5}})
    args = ["daily", "plan", "--formulas", str(folder), "--template", str(root)]
    assert main(args + ["--start", "2027-01-01T05:00:00+00:00"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["budget"]["shifter"]["concurrency"] == 240 and plan["droplets"] == 1
