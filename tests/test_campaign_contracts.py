"""The frozen campaign contracts: names, tags, accounts, the spec and the ledger records."""

from __future__ import annotations

import json
import re
import typing
from dataclasses import fields, replace
from pathlib import Path

import pytest

from proxy_index.campaign import contracts
from proxy_index.campaign.contracts import ACCOUNTS, CampaignSpec, DropletRecord, HourRecord

CID = "20270101t220000z-0123456789ab"
SID = "01234567"


def spec(**changes) -> CampaignSpec:
    base = CampaignSpec(
        campaign_id=CID,
        created_at="2027-01-01T21:00:00+00:00",
        account="leftclick",
        project_id="22222222-2222-4222-8222-222222222222",
        starts_at="2027-01-01T22:00:00+00:00",
        grace_hours=48,
        regions=("lon1", "nyc3"),
        networks=("shifter", "geonode"),
        manifests={"shifter": "a.json", "geonode": "b.json"},
        size_by_region={"lon1": "c-2", "nyc3": "c-2"},
        image_by_region={"lon1": "11", "nyc3": "12"},
        image_name="pi-camp-worker-0.9.5",
        wheel_sha256="a" * 64,
        framework_version="0.9.5",
        ssh_key_id="9",
        manager_droplet_id="500001",
    )
    return replace(base, **changes)


def test_names_tags_and_ids_follow_the_frozen_patterns():
    assert contracts.short_id(CID) == SID
    assert contracts.campaign_tag(CID) == f"pi-camp-{SID}"
    assert contracts.hour_tag(CID, 3) == f"pi-camp-{SID}-h03"
    name = contracts.droplet_name(CID, 3, "lon1", "proxy-cheap")
    assert name == f"pic-{SID}-h03-lon1-proxy-cheap"
    assert re.fullmatch(contracts.WORKER_NAME_RE, name)
    for bad in ("camp-1", "20270101T220000Z-0123456789ab", "20270101t220000z-xyz"):
        with pytest.raises(ValueError):
            contracts.short_id(bad)
    with pytest.raises(ValueError):
        contracts.droplet_name(CID, 3, "LON1", "shifter")
    assert set(ACCOUNTS) == {"leftclick", "proxy-index"}
    assert {a.project_name for a in ACCOUNTS.values()} == {"Proxy Index"}


def test_spec_round_trips_and_validates():
    good = spec().validate()
    assert CampaignSpec.from_json(good.to_json()) == good
    for changes in (
        {"account": "other"},
        {"starts_at": "2027-01-01T22:30:00+00:00"},
        {"starts_at": "2027-01-01T22:00:00"},
        {"created_at": "2027-01-01T21:00:00"},
        {"regions": ("nyc3", "lon1")},
        {"regions": ("lon1", "xyz1")},
        {"networks": ("shifter", "shifter")},
        {"networks": ("Shifter",)},
        {"manifests": {}},
        {"size_by_region": {}},
        {"image_by_region": {}},
        {"wheel_sha256": "x"},
        {"grace_hours": 0},
        {"hours": 0},
        {"hours": 25},
        {"project_id": ""},
        {"slot_minutes": 7},
        {"slot_minutes": 45},
        {"slot_minutes": 15.0},
        {"slot_minutes": 10},
        {"slot_minutes": 5, "create_lead_seconds": 300},
        {"create_lead_seconds": 0},
        {"schema_version": 2},
        {"concurrency": 0},
        {"concurrency": 5001},
        {"concurrency": True},
        {"hours": 24.0},
        {"grace_hours": True},
        {"create_lead_seconds": 90.5},
    ):
        with pytest.raises(ValueError):
            spec(**changes).validate()
    with pytest.raises(ValueError):
        CampaignSpec.from_json(json.dumps({"campaign_id": CID, "surprise": 1}))


def test_records_and_paths():
    hour = HourRecord(
        CID,
        3,
        "2027-01-02T01:00:00+00:00",
        droplets=[DropletRecord("shifter", "lon1", 3, "pic-x", droplet_id="7")],
    )
    assert HourRecord.from_dict(json.loads(json.dumps(hour.to_dict()))) == hour
    root = Path("/c")
    assert contracts.hour_path(root, 3) == root / "ledger" / "03" / "hour.json"
    assert contracts.audit_path(root) == root / "ledger" / "delete-audit.jsonl"
    assert contracts.network_root(root, "n", "m") == root / "networks" / "n" / "residential" / "m"
    assert [
        p(root).name
        for p in (
            contracts.spec_path,
            contracts.runtime_dir,
            contracts.reports_dir,
            contracts.status_path,
            contracts.alerts_path,
        )
    ] == ["campaign-spec.json", ".runtime", "reports", "status.json", "alerts.jsonl"]
    assert contracts.logs_dir(root, 4) == root / "logs" / "04"


def test_v2_1_load_and_cpu_fields():
    """M1: concurrency on the spec and the records, CPU samples, and assign_failed."""
    assert spec().concurrency == 2000
    assert CampaignSpec.from_json(spec(concurrency=2500).to_json()).concurrency == 2500
    sampled = {
        "concurrency": 2000,
        "cpu_p50_pct": 41.0,
        "cpu_p95_pct": 77.5,
        "cpu_max_pct": 93.0,
        "cpu_core_max_pct": 99.0,
        "steal_p95_pct": 0.4,
        "load1_p95": 1.9,
        "mem_used_max_pct": 38.0,
    }
    record = DropletRecord("shifter", "lon1", 3, "pic-x", **sampled)
    hour = HourRecord(CID, 3, "2027-01-02T01:00:00+00:00", droplets=[record])
    assert HourRecord.from_dict(json.loads(json.dumps(hour.to_dict()))).droplets[0] == record
    assert set(sampled) <= {item.name for item in fields(DropletRecord)}
    assert "assign_failed" in typing.get_args(contracts.DropletStatus)
    assert contracts.PROJECT_NAME == "Proxy Index"


def test_worker_sizes_must_be_droplet_slugs():
    """CAMP-1p: the spec refuses a size the plan command would refuse."""
    for size in ("c-2", "c2-4", "c-4-intel", "s-2vcpu-4gb", "s-4vcpu-8gb-amd"):
        assert spec(size_by_region={"lon1": size, "nyc3": "c-2"}).validate()
    for size in ("", "C-2", "g-2vcpu-8gb", "c-2 ", "s-2vcpu", 2):
        with pytest.raises(ValueError, match="size_by_region"):
            spec(size_by_region={"lon1": size, "nyc3": "c-2"}).validate()


def test_hour_records_carry_paused_networks_and_old_records_still_load():
    hour = HourRecord(CID, 3, "2027-01-02T01:00:00+00:00", paused_networks=["geonode"])
    assert HourRecord.from_dict(json.loads(json.dumps(hour.to_dict()))) == hour
    old = {key: value for key, value in hour.to_dict().items() if key != "paused_networks"}
    assert HourRecord.from_dict(old).paused_networks == []
    assert "paused" in typing.get_args(contracts.CellStatus)
