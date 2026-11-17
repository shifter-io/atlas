"""DROPLET-CREATE-ERR-1: a refused worker create records DigitalOcean's answer (error id,
message, HTTP status; never the token) in the worker's `error_detail`, its alert, alerts.jsonl,
one journal line and the daily outcome's failed cells.

Offline only: the in-process fake DigitalOcean and fake clocks."""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from campaign_fakes import CID, TOKEN, FakeDigitalOcean, Fault
from campaign_hour_cases import NETWORKS, launched
from campaign_hour_cases import make_spec as hour_spec
from campaign_tick_cases import START, Clock, make_root, pacing, worker

from atlas.campaign import collector, launch
from atlas.campaign.contracts import HourRecord, hour_path
from atlas.campaign.tick import load_hour, load_spec, read_alerts, save_hour
from atlas.campaign.launch_steps import failed_cells

REFUSAL = {"id": "unprocessable_entity", "message": "Size is not available in this region."}
WHY = "POST /v2/droplets failed (422): Size is not available in this region."


async def test_a_refused_create_records_digitaloceans_answer(tmp_path, caplog):
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", "/v2/droplets", 422, times=99, body=REFUSAL))
    with caplog.at_level(logging.WARNING, logger="atlas.campaign.launch_steps"):
        record, root, *_ = await launched(tmp_path, fake)
    assert {d.status for d in record.droplets} == {"create_failed"}
    for droplet in record.droplets:
        detail = droplet.error_detail
        assert (detail["code"], detail["message"]) == ("unprocessable_entity", WHY)
        assert detail["http"][-1] == "POST /v2/droplets 422"
    alerts = [a for a in record.alerts if a["kind"] == "create_failed"]
    assert alerts and {a["message"] for a in alerts} == {WHY}
    lines = [r.getMessage() for r in caplog.records]
    assert any("droplet create failed" in line and WHY in line for line in lines)
    saved = hour_path(root, 0).read_text()
    assert WHY in saved and TOKEN not in saved and all(TOKEN not in line for line in lines)


async def test_a_region_whose_create_raised_says_why_in_its_alert(tmp_path, monkeypatch):
    async def broken(self, workers):
        raise RuntimeError("size c-8 unavailable")

    monkeypatch.setattr(launch._Launch, "_create_call", broken)
    record, *_ = await launched(tmp_path, FakeDigitalOcean(), hour_spec(NETWORKS[:1], ("tor1",)))
    alerts = [a for a in record.alerts if a["kind"] == "launch_error"]
    assert alerts and {a["message"] for a in alerts} == {"size c-8 unavailable"}
    assert {d.error_detail["error"] for d in record.droplets} == {"RuntimeError"}


async def test_the_collector_mirrors_the_alert_message_into_alerts_jsonl(tmp_path):
    root = make_root(tmp_path, synthetic=True)
    clock = Clock(START + timedelta(minutes=1))
    alert = {"kind": "create_failed", "level": "high", "name": "w1", "message": WHY}
    record = HourRecord(CID, 0, START.isoformat(), state="running", alerts=[alert])
    save_hour(root, record)
    work = collector.Collector(root, None, None, None, clock, pacing(clock))
    work._alerts(load_spec(root), {0: record}, clock())
    mirrored = [a for a in read_alerts(root) if a["code"] == "create_failed"]
    assert [(a["name"], a["message"]) for a in mirrored] == [("w1", WHY)]


def test_the_daily_outcome_names_each_failed_cell_and_why(tmp_path):
    root = make_root(tmp_path)
    spec = load_spec(root)
    record = HourRecord(
        spec.campaign_id,
        0,
        START.isoformat(),
        state="running",
        cells={
            "shifter/tor1": "failed_ours",
            "shifter/lon1": "running",
            "soax/nyc3": "failed_ours",
        },
        droplets=[
            worker("shifter", "tor1", 0, status="create_failed"),
            worker("shifter", "tor1", 0, status="create_failed", error_detail={"message": WHY}),
            worker("shifter", "lon1", 0, droplet_id="1", status="running"),
            worker("soax", "nyc3", 0, droplet_id="2", status="boot_failed"),
        ],
    )
    save_hour(root, record)
    found = failed_cells(load_hour(root, spec, 0))
    assert found == {"shifter/tor1": WHY, "soax/nyc3": "boot_failed"}
    assert json.dumps(found)  # plain strings, ready for daily.json
