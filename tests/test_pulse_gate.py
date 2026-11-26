"""PULSE-GATE-1 (#1414): the HH:30 pulse gate counts a daily cell whose worker's round ended (a
budget stop at HH:27) as ended, even while its results are not pulled yet, so a slow or failed
first pull raises no `pulse_budget_overlap`; a cell whose round still runs does. Offline ledgers;
the hour tests use the test's own database."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from campaign_hour_cases import ended, harvest_setup, harvested
from orchestrator_cases import query
from pulse_fleet_cases import FakeClock, world
from test_pulse_budget import _daily
from test_pulse_late_start import _budgeted
from test_pulse_ondemand import HourClock, hour, ready

from atlas.campaign import pulse_late
from atlas.campaign.contracts import DropletRecord, HourRecord, hour_path

pytest_plugins = ["test_storage", "orchestrator_cases"]

AT = datetime(2026, 11, 20, 8, tzinfo=UTC)


def _worker(network: str, region: str, *, ended: bool) -> DropletRecord:
    """A daily worker: its round stopped at HH:27 and its first pull failed (`ended`), else
    still running its round."""
    worker = DropletRecord(network, region, 0, f"atlas-{network}-{region}", droplet_id="1")
    worker.status, worker.started_at = "running", "2026-11-20T08:00:40+00:00"
    if ended:
        worker.status, worker.exit_code = "collect_failed", 130
        worker.finished_at, worker.error_class = "2026-11-20T08:27:10+00:00", "collect_failed"
    return worker


def _ledger(network: str, ended: dict[str, bool]) -> dict:
    """The hour's ledger as the harvest leaves it: each cell `running` until its pull succeeds,
    whether or not its worker's round has ended (`ended`, by region)."""
    record = HourRecord("c", 0, AT.isoformat(), state="harvesting")
    for region, over in ended.items():
        record.droplets.append(_worker(network, region, ended=over))
        record.cells[f"{network}/{region}"] = "running"
    return record.to_dict()


def test_an_ended_cell_whose_pull_has_not_completed_is_not_running(tmp_path):
    campaigns, nets = tmp_path / "campaigns", {"net"}
    campaigns.mkdir()
    record = _ledger("net", {"sgp1": True})
    _daily(campaigns, f"{AT:%Y%m%dt%H}", networks=("net",), record=record)
    assert pulse_late.daily_waiting(campaigns, AT, nets) == ([], ["net/sgp1"])


def test_a_cell_whose_round_still_runs_is_still_running(tmp_path):
    campaigns, nets = tmp_path / "campaigns", {"net"}
    campaigns.mkdir()
    record = _ledger("net", {"sgp1": True, "lon1": False})
    record["droplets"].append("not a worker")  # an unreadable entry ends no cell
    _daily(campaigns, f"{AT:%Y%m%dt%H}", networks=("net",), record=record)
    assert pulse_late.daily_waiting(campaigns, AT, nets) == (["net/lon1"], ["net/sgp1"])


async def test_the_gate_at_minute_30_lists_the_ended_cell_as_not_stored(tmp_path):
    campaigns = tmp_path / "campaigns"
    campaigns.mkdir()
    _daily(campaigns, f"{AT:%Y%m%dt%H}", networks=("net",), record=_ledger("net", {"sgp1": True}))
    clock = FakeClock()
    clock.t = (AT + timedelta(minutes=29)).timestamp()
    gate = await pulse_late.wait_for_daily(clock, campaigns, AT, {"net"})
    latest = (AT + pulse_late.GATE_LATEST).isoformat()
    assert gate == {"gate": "latest", "at": latest, "daily_not_stored": ["net/sgp1"]}


async def test_an_ended_cell_not_yet_pulled_raises_no_overlap_alert(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)
    _budgeted(monkeypatch, tmp_path, _ledger("shifter", {"lon1": True}))
    _, outcome = await hour(tmp_path, world(), db, HourClock(database))
    late = outcome["late"]
    assert late["gate"] == "latest" and "daily_still_running" not in late
    assert late["daily_not_stored"] == ["shifter/lon1"]
    assert query(database, "SELECT kind FROM alerts") == []
    assert outcome["state"] == "done"


async def test_a_real_overlap_alerts_and_names_the_ended_cells_apart(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)
    _budgeted(monkeypatch, tmp_path, _ledger("shifter", {"lon1": False, "fra1": True}))
    _, outcome = await hour(tmp_path, world(), db, HourClock(database))
    late = outcome["late"]
    assert late["daily_still_running"] == ["shifter/lon1"]
    assert late["daily_not_stored"] == ["shifter/fra1"]
    rows = query(database, "SELECT kind,severity,message,data FROM alerts")
    assert [(kind, severity) for kind, severity, _, _ in rows] == [
        ("pulse_budget_overlap", "major")
    ]
    message, data = rows[0][2], rows[0][3]
    data = json.loads(data) if isinstance(data, str) else data
    assert "still going: shifter/lon1" in message
    assert "ended, results not stored yet: shifter/fra1" in message
    assert data["cells"] == ["shifter/lon1"] and data["ended_not_stored"] == ["shifter/fra1"]


async def test_the_harvest_saves_an_ended_round_before_its_pull(tmp_path):
    """A slow first pull (minutes) must not hide the ended round from the gate: the ledger on
    disk shows the worker's `finished_at` while the pull still runs."""
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    remote.inspected[record.droplets[0].name] = ended(130, "stopped")
    seen, real = [], remote.pull

    async def pull(droplet, remote_path, local):
        seen.append(pulse_late._rounds_ended(json.loads(hour_path(root, 0).read_text())))
        return await real(droplet, remote_path, local)

    remote.pull = pull
    await harvested(spec, record, root, fake, remote, clock)
    assert seen == [{"net00/tor1"}] and record.droplets[0].status == "destroyed"
