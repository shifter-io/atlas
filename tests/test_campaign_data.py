"""Campaign merge: offline, RFC 5737 addresses only.

The built campaign and its ledger helpers are in `campaign_data_cases.py`; completeness is
tested in `test_campaign_completeness.py` and verified collect in `test_campaign_collect.py`.
"""

import shutil
from datetime import timedelta

import pytest
from campaign_data_cases import (
    NETWORKS,
    REGIONS,
    _cells,
    _droplet,
    finish_day,
    kill_round,
    unpull,
    write_hour,
)
from test_onboarding import ROOT, START

from atlas.campaign import completeness, merge
from atlas.campaign.contracts import CampaignSpec, spec_path
from atlas.cli import main
from atlas.onboarding import manifest as campaign
from atlas.onboarding.export import export_campaign
from atlas.onboarding.files import atomic_json, read_json
from atlas.onboarding.rounds import round_directory
from atlas.onboarding.scheduler import validate_state

pytest_plugins = ["campaign_data_cases"]


def test_merge_refuses_unfinished_cells_and_writes_nothing(root):
    with pytest.raises(merge.CampaignNotFinished, match="geonode ams3 h03 is pending"):
        merge.merge_network(root, "geonode")
    finish_day(root)
    write_hour(root, 23, _cells("create_failed") | {("shifter", "tor1"): ["boot_failed", "armed"]})
    with pytest.raises(merge.CampaignNotFinished, match="shifter tor1 h23 is running"):
        merge.merge_network(root, "shifter")
    assert not list(root.glob("networks/*/residential/*/regions/*/state.json"))
    with pytest.raises(ValueError, match="not part of this campaign"):
        merge.merge_network(root, "decodo")


def test_merge_then_validate_and_export_pass(root):
    finish_day(root)
    for network in NETWORKS:
        path = merge.manifest_path(root, network)
        manifest = campaign.load(path)
        summaries = merge.merge_network(root, network)
        assert [s["region"] for s in summaries] == list(REGIONS)
        for region in REGIONS:
            state = read_json(path.parent / "regions" / region / "state.json")
            validate_state(state, manifest, region)
            assert state["status"] == "closed" and len(state["jobs"]) == 24 * len(
                manifest["regions"][region]
            )
            assert read_json(path.parent / "regions" / region / "summary.json")["status"] == (
                "closed"
            )
        exported = export_campaign(path)
        rows = 3 if network == "geonode" else 2
        jobs = sum(len(manifest["regions"][r]) for r in REGIONS) * 3
        if network == "geonode":
            jobs -= len(manifest["regions"]["ams3"])  # the failed_ours cell
        else:
            jobs -= 2  # GL interrupted with no rows, PM never started
        assert exported["rows"] == rows * jobs and not exported["complete"]
    state = read_json(merge.manifest_path(root, "geonode").parent / "regions/ams3/state.json")
    failed = [j for j in state["jobs"] if j["round"] == 2]
    assert {j["status"] for j in failed} == {"not_started_deadline"}
    assert failed[0]["scheduled_at"] == campaign.timestamp(START + timedelta(hours=2))
    assert main(["campaign", "merge", "--root", str(root), "--network", "shifter"]) == 0


def test_merge_refuses_a_running_round_and_a_foreign_manifest(root):
    finish_day(root)
    path = round_directory(merge.manifest_path(root, "shifter"), "tor1", 0) / "round.json"
    state = read_json(path)
    atomic_json(path, state | {"status": "running"})
    write_hour(root, 0, _cells("destroyed") | {("shifter", "tor1"): "running"})
    with pytest.raises(merge.CampaignNotFinished, match="shifter tor1 h00 is running"):
        merge.merge_network(root, "shifter")
    atomic_json(path, state | {"campaign_id": "20270101t220000z-ffffffffffff"})
    with pytest.raises(ValueError, match="does not match"):
        merge.merge_network(root, "shifter")
    shutil.rmtree(root / "networks" / "geonode")
    with pytest.raises(ValueError, match="exactly one campaign manifest"):
        merge.merge_network(root, "geonode")
    campaign.create(
        ROOT / "configs/geonode.toml",
        "residential",
        campaign.timestamp(START),
        root / "networks",
        now=START - timedelta(hours=1),
    )
    with pytest.raises(ValueError, match="version 3"):
        merge.merge_network(root, "geonode")


def test_b1_a_collect_failed_cell_refuses_the_merge(root):
    finish_day(root)
    path = unpull(root, "geonode", "tor1", 1)
    write_hour(root, 1, _cells("destroyed") | {("geonode", "tor1"): "collect_failed"})
    with pytest.raises(
        merge.CampaignNotFinished,
        match=r"geonode tor1 h01: data waits on a kept worker \(collect_failed\)",
    ):
        merge.merge_network(root, "geonode")
    assert not list(path.parent.glob("regions/*/state.json"))


@pytest.mark.parametrize("status", ["destroyed", "collected", "destroy_refused"])
def test_b1_a_started_worker_without_its_round_refuses_the_merge(root, status):
    finish_day(root)
    path = unpull(root, "geonode", "tor1", 1)
    write_hour(root, 1, _cells("destroyed") | {("geonode", "tor1"): status})
    with pytest.raises(
        merge.RoundDataMissing, match="geonode tor1 h01: round ran; data not collected"
    ):
        merge.merge_network(root, "geonode")
    assert not list(path.parent.glob("regions/*/state.json"))


@pytest.mark.parametrize(
    "status",
    [
        "create_failed",
        "assign_failed",
        "boot_failed",
        "start_failed",
        *[
            (s, {"armed_at": None, "started_at": None})
            for s in ("destroyed", "destroy_refused", "destroy_failed")
        ],
    ],
)
def test_b1_never_started_workers_still_merge_as_not_started(root, status):
    finish_day(root)
    path = unpull(root, "geonode", "tor1", 1)
    write_hour(root, 1, _cells("destroyed") | {("geonode", "tor1"): ["boot_failed", status]})
    merge.merge_network(root, "geonode")
    state = read_json(path.parent / "regions/tor1/state.json")
    assert {j["status"] for j in state["jobs"] if j["round"] == 1} == {"not_started_deadline"}


def test_b2_a_killed_round_merges_with_its_jobs_closed(root):
    finish_day(root)
    kill_round(root, "shifter", "tor1", 0)
    merge.merge_network(root, "shifter")
    path = merge.manifest_path(root, "shifter")
    state = read_json(path.parent / "regions/tor1/state.json")
    assert [j["status"] for j in state["jobs"] if j["round"] == 0] == [
        "completed",
        "completed",
        "failed",
    ]
    assert export_campaign(path)["rows"] > 0


def test_b2_a_killed_round_with_a_live_worker_is_still_running(root):
    finish_day(root)
    kill_round(root, "shifter", "tor1", 0)
    write_hour(root, 0, _cells("destroyed") | {("shifter", "tor1"): "running"})
    with pytest.raises(merge.CampaignNotFinished, match="shifter tor1 h00 is running"):
        merge.merge_network(root, "shifter")
    rows = {(r["net"], r["region"], r["round"]): r for r in completeness.build(root)["rows"]}
    assert rows["shifter", "tor1", 0]["cell"] == "running"


def test_m2_m3_merge_cli_names_the_cell_and_writes_no_network(root, capsys):
    finish_day(root)
    write_hour(root, 23, _cells("create_failed") | {("shifter", "tor1"): "collect_failed"})
    assert main(["campaign", "merge", "--root", str(root)]) == 3
    assert capsys.readouterr().err.strip() == (
        "Not merged: shifter tor1 h23: data waits on a kept worker (collect_failed)"
    )
    assert not list(root.glob("networks/*/residential/*/regions/*/state.json"))
    finish_day(root)
    assert main(["campaign", "merge", "--root", str(root)]) == 0
    assert len(list(root.glob("networks/*/residential/*/regions/*/state.json"))) == 4


def test_a_compressed_spec_checks_and_merges_only_its_slots(root):
    """CAMP-1n: a 3-slot spec has 3 cells per network and region; its later rounds were
    never launched and close unstarted, so no day has to be finished first."""
    from dataclasses import replace

    spec = CampaignSpec.from_json(spec_path(root).read_text())
    spec_path(root).write_text(replace(spec, hours=3).to_json())
    result = completeness.build(root)
    assert result["cells"] == 2 * 2 * 3 and result["totals"]["pending"] == 0
    tables = (root / "completeness" / "per-hour-tables.md").read_text()
    assert "| Region | h00 | h01 | h02 |\n|---|---|---|---|" in tables
    merge.merge_network(root, "shifter")
    state = read_json(
        merge.manifest_path(root, "shifter").parent / "regions" / "ams3" / "state.json"
    )
    assert {j["status"] for j in state["jobs"] if j["round"] >= 3} == {"not_started_deadline"}
    assert {j["round"] for j in state["jobs"]} == set(range(24))


def test_a_network_paused_for_an_hour_merges_with_paused_cells(root, capsys):
    """CAMP-1p: geonode was paused when hour 1 launched, so it has no worker and no round
    there. Its cells are final `paused` cells: merge proceeds and they bring no data."""
    finish_day(root)
    for region in REGIONS:
        unpull(root, "geonode", region, 1)
    write_hour(
        root, 1, {("shifter", region): "destroyed" for region in REGIONS}, paused=("geonode",)
    )
    result = completeness.build(root)
    rows = {(r["net"], r["region"], r["round"]): r for r in result["rows"]}
    assert rows["geonode", "ams3", 1]["cell"] == rows["geonode", "tor1", 1]["cell"] == "paused"
    assert rows["shifter", "ams3", 1]["cell"] == "complete"
    assert result["totals"]["paused"] == 2 and result["networks"]["shifter"]["paused"] == 0
    assert not [issue for issue in result["issues"] if issue.startswith("geonode")]
    tables = (root / "completeness" / "per-hour-tables.md").read_text()
    assert "| ams3 | ✓ 25 | ‖ paused | ✗ ours |" in tables
    assert main(["campaign", "completeness", "--root", str(root)]) == 3
    capsys.readouterr()
    summaries = merge.merge_network(root, "geonode")
    assert len(summaries) == len(REGIONS)
    path = merge.manifest_path(root, "geonode")
    for region in REGIONS:
        state = read_json(path.parent / "regions" / region / "state.json")
        jobs = [j for j in state["jobs"] if j["round"] == 1]
        assert {j["status"] for j in jobs} == {"not_started_deadline"}
        assert not any(j.get("started_at") or j.get("requests") for j in jobs)


def test_paused_applies_only_to_a_cell_without_workers():
    worker = _droplet("geonode", "ams3", 1, "running")
    assert merge.cell_status(None, None, paused=True) == ("paused", None)
    assert merge.cell_status(None, [worker], paused=True) == ("running", None)
    assert merge.cell_status(None, None) == ("pending", None)
