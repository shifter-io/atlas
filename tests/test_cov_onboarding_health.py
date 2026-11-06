"""COV-3: the read-only campaign health watcher (`watch`, `main`), offline.

The fleet collection is faked; the audit, its files and the alert throttle are real.
"""

import sys
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_onboarding import START
from test_onboarding_health_backup import snapshot
from test_onboarding_sequential import ORDER, make

from proxy_index.onboarding import health
from proxy_index.onboarding import manifest as campaign
from proxy_index.onboarding.files import lock, read_json

UNREACHABLE = {"region": "nyc3", "issue": "unreachable_or_missing_state"}


class Stop(Exception):
    """Ends a `--watch` loop from the fake sleep."""


@pytest.fixture
def campaign_files(tmp_path, monkeypatch):
    """A sequential campaign, a fake fleet collection and a controlled health clock."""
    path = make(tmp_path / "campaign", execution_mode="sequential", region_order=ORDER)
    manifest = campaign.load(path)
    clock = SimpleNamespace(now=START.timestamp() - 60)
    monkeypatch.setattr(health, "time", SimpleNamespace(time=lambda: clock.now))
    calls = []
    fleet = SimpleNamespace(snapshot=snapshot(manifest, clock.now))

    async def collect(inventory, output):
        calls.append((inventory, output))
        return deepcopy(fleet.snapshot)

    monkeypatch.setattr(health, "collect", collect)
    args = SimpleNamespace(
        manifest=path, inventory=tmp_path / "inventory.json", output=tmp_path / "out", watch=False
    )
    return SimpleNamespace(args=args, manifest=manifest, clock=clock, calls=calls, fleet=fleet)


async def test_watch_once_writes_the_audit_and_an_alert(campaign_files):
    files = campaign_files
    files.fleet.snapshot["servers"][0] = {"region": "nyc3", "error": "SSH inspection failed"}
    result = await health.watch(files.args)
    assert files.calls == [(files.args.inventory, files.args.output)]
    assert result["alerts"] == [UNREACHABLE]
    assert result["campaign_id"] == files.manifest["campaign_id"]
    assert sorted(r["region"] for r in result["regions"]) == sorted(ORDER[1:])
    assert read_json(files.args.output / "campaign-audit.json") == result
    alert = files.args.output / "alerts" / f"{int(files.clock.now)}.json"
    assert read_json(alert) == result


async def test_watch_without_alerts_writes_only_the_audit(campaign_files):
    result = await health.watch(campaign_files.args)
    assert result["alerts"] == [] and not result["measurement_complete"]
    assert (campaign_files.args.output / "campaign-audit.json").exists()
    assert not (campaign_files.args.output / "alerts").exists()


async def test_watch_repeats_every_ten_seconds_and_files_an_alert_at_most_each_minute(
    campaign_files, monkeypatch
):
    files = campaign_files
    files.fleet.snapshot["servers"][0] = {"region": "nyc3", "error": "SSH inspection failed"}
    files.args.watch = True
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 7:
            raise Stop
        files.clock.now += seconds

    monkeypatch.setattr(health.asyncio, "sleep", sleep)
    first = int(files.clock.now)
    with pytest.raises(Stop):
        await health.watch(files.args)
    assert waits == [10] * 7 and len(files.calls) == 7
    alerts = sorted(p.name for p in (files.args.output / "alerts").iterdir())
    assert alerts == [f"{first}.json", f"{first + 60}.json"]
    audit = read_json(files.args.output / "campaign-audit.json")
    assert audit["checked_at"] == campaign.timestamp(first + 60)


async def test_a_second_watcher_is_refused_while_one_holds_the_lock(campaign_files):
    files = campaign_files
    with lock(files.args.output / ".health.lock"):
        with pytest.raises(ValueError, match="already running"):
            await health.watch(files.args)
    assert files.calls == []


def test_main_parses_its_options_and_runs_one_check(campaign_files, monkeypatch):
    files = campaign_files
    files.clock.now = (START + timedelta(hours=1)).timestamp()
    options = ["--manifest", str(files.args.manifest), "--inventory", str(files.args.inventory)]
    monkeypatch.setattr(sys, "argv", ["health", *options, "--output", str(files.args.output)])
    assert health.main() is None
    audit = read_json(files.args.output / "campaign-audit.json")
    assert audit["checked_at"] == campaign.timestamp(files.clock.now)
    assert files.calls == [(files.args.inventory, files.args.output)]
