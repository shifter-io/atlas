"""Campaign merge, completeness and verified collect: offline, RFC 5737 addresses only."""

import asyncio
import gzip
import json
import os
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from test_onboarding import ROOT, START, Clock
from test_onboarding_round import census, measurement

from proxy_index.campaign import collect as collector
from proxy_index.campaign import completeness, merge
from proxy_index.campaign.contracts import (
    CampaignSpec,
    DropletRecord,
    HourRecord,
    droplet_name,
    hour_path,
    spec_path,
)
from proxy_index.cli import main
from proxy_index.models import ProxyConfig
from proxy_index.onboarding import manifest as campaign
from proxy_index.onboarding.export import export_campaign
from proxy_index.onboarding.files import atomic_json, read_json
from proxy_index.onboarding.rounds import execute_round, round_directory
from proxy_index.onboarding.scheduler import validate_state

CID = "20270101t220000z-0123456789ab"
NETWORKS = ("geonode", "shifter")
REGIONS = ("ams3", "tor1")
GOLDEN = Path(__file__).parent / "fixtures" / "campaign-data"
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
FAILED = ("geonode", "ams3", 2)  # a worker that never booted, and its replacement
STOPPED = ("shifter", "tor1", 1)  # an operator stop during GL


class Proxy:
    def __init__(self, provider, country):
        self.provider, self.country = provider, country.lower()

    def public(self):
        return {"provider": self.provider, "product": "residential",
                "requested_country": self.country}

    def resolve(self, context):
        return ProxyConfig("http", "localhost", 8080)


def _spec(manifests: dict) -> CampaignSpec:
    return CampaignSpec(
        campaign_id=CID, created_at="2027-01-01T21:00:00+00:00", account="leftclick",
        project_id="project-1", starts_at=campaign.timestamp(START), grace_hours=48,
        regions=REGIONS, networks=NETWORKS, manifests=manifests,
        size_by_region=dict.fromkeys(REGIONS, "c-2"),
        image_by_region=dict.fromkeys(REGIONS, "image-1"), image_name="pi-camp-worker-test",
        wheel_sha256="0" * 64, framework_version="test", ssh_key_id="key-1",
        manager_droplet_id="manager-1")


async def _round(path: Path, network: str, region: str, hour: int) -> None:
    scheduled = campaign.moment(campaign.load(path)["rounds"][hour]).timestamp()
    clock, stop, special = Clock(scheduled), asyncio.Event(), {}
    if (network, region, hour) == STOPPED:
        async def stopped(country_stop):
            stop.set()
            await country_stop.wait()
            return measurement(0, status="stopped", stop_reason="stopped", completed=0,
                               total_errors=0)
        special["GL"] = stopped
    rows = 3 if network == "geonode" else 2
    await execute_round(path, region, hour, lambda c: Proxy(network, c), clock=clock, stop=stop,
                        run_country=census(clock, [], rows=rows, special=special))


def _droplet(network, region, hour, status, **extra) -> DropletRecord:
    scheduled = START + timedelta(hours=hour)
    values = {"droplet_id": f"{hour}{len(network)}{len(region)}", "size": "c-2",
              "status": status,
              "requested_at": campaign.timestamp(scheduled - timedelta(seconds=90)),
              "ssh_ready_at": campaign.timestamp(scheduled - timedelta(seconds=40)),
              "start_lateness_seconds": 0.25} | extra
    if status.endswith("_failed"):  # a failed worker was never ready and never started
        values |= {"ssh_ready_at": None, "start_lateness_seconds": None}
    else:  # a worker that got this far armed and started its round
        values = {"armed_at": values["ssh_ready_at"],
                  "started_at": campaign.timestamp(scheduled)} | values
    return DropletRecord(network, region, hour, droplet_name(CID, hour, region, network),
                         **values)


def write_hour(root: Path, hour: int, statuses: dict) -> None:
    """One ledger record; `statuses` maps (network, region) to a status or a list of them.

    A status may be a `(status, {field: value})` pair to override record fields.
    """
    droplets = []
    for (network, region), status in statuses.items():
        for item in status if isinstance(status, list) else [status]:
            item, extra = item if isinstance(item, tuple) else (item, {})
            overlap = 1 if (network, region, hour) == STOPPED else 0
            droplets.append(_droplet(network, region, hour, item, overlap=overlap, **extra))
    record = HourRecord(CID, hour, campaign.timestamp(START + timedelta(hours=hour)),
                        state="done", droplets=droplets)
    atomic_json(hour_path(root, hour), record.to_dict())


def _cells(status):
    return {(n, r): status for n in NETWORKS for r in REGIONS}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """2 networks × 2 regions × 3 collected rounds, one stopped and one failed_ours cell."""
    root = tmp_path_factory.mktemp("campaign") / CID
    paths = {}
    for network in NETWORKS:
        config = ROOT / "configs" / f"{network}.toml"
        paths[network] = campaign.create(
            config, "residential", campaign.timestamp(START), root / "networks",
            execution_mode="ephemeral", now=START - timedelta(hours=1))
    spec_path(root).write_text(_spec({n: p.parent.name for n, p in paths.items()}).to_json())
    for hour in range(3):
        for network in NETWORKS:
            for region in REGIONS:
                if (network, region, hour) != FAILED:
                    asyncio.run(_round(paths[network], network, region, hour))
        statuses = _cells("destroyed")
        if hour == 2:
            statuses[FAILED[:2]] = ["boot_failed", "create_failed"]
        write_hour(root, hour, statuses)
    return root


@pytest.fixture
def root(built, tmp_path):
    return Path(shutil.copytree(built, tmp_path / CID))


def finish_day(root: Path) -> None:
    """The rest of the day failed on our side, so every cell is finished."""
    for hour in range(3, 24):
        write_hour(root, hour, _cells("create_failed"))


def test_merge_refuses_unfinished_cells_and_writes_nothing(root):
    with pytest.raises(merge.CampaignNotFinished, match="geonode ams3 h03 is pending"):
        merge.merge_network(root, "geonode")
    finish_day(root)
    write_hour(root, 23, _cells("create_failed") | {("shifter", "tor1"): ["boot_failed",
                                                                        "armed"]})
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
                manifest["regions"][region])
            assert read_json(path.parent / "regions" / region / "summary.json")["status"] == (
                "closed")
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
    campaign.create(ROOT / "configs/geonode.toml", "residential", campaign.timestamp(START),
                    root / "networks", now=START - timedelta(hours=1))
    with pytest.raises(ValueError, match="version 3"):
        merge.merge_network(root, "geonode")


def unpull(root: Path, network: str, region: str, hour: int) -> Path:
    """Remove a cell's pulled round, as if the harvest never brought it home."""
    path = merge.manifest_path(root, network)
    shutil.rmtree(round_directory(path, region, hour))
    return path


def kill_round(root: Path, network: str, region: str, hour: int) -> None:
    """A round killed during its last country: the 5-second checkpoint still says running."""
    folder = round_directory(merge.manifest_path(root, network), region, hour)
    state = read_json(folder / "round.json")
    last = state["jobs"][-1]
    for key in ("finished_at", "observations", "duration_seconds", "requests", "errors",
                "stop_reason"):
        last.pop(key)
    last["status"] = "running"
    state.update(status="running", active={"round": hour, "country": last["country"],
                                           "started_at": last["started_at"]})
    atomic_json(folder / "round.json", state)
    (folder / last["country"].lower() / "summary.json").unlink()


def test_b1_a_collect_failed_cell_refuses_the_merge(root):
    finish_day(root)
    path = unpull(root, "geonode", "tor1", 1)
    write_hour(root, 1, _cells("destroyed") | {("geonode", "tor1"): "collect_failed"})
    with pytest.raises(merge.CampaignNotFinished,
                       match=r"geonode tor1 h01: data waits on a kept worker \(collect_failed\)"):
        merge.merge_network(root, "geonode")
    assert not list(path.parent.glob("regions/*/state.json"))


@pytest.mark.parametrize("status", ["destroyed", "collected", "destroy_refused"])
def test_b1_a_started_worker_without_its_round_refuses_the_merge(root, status):
    finish_day(root)
    path = unpull(root, "geonode", "tor1", 1)
    write_hour(root, 1, _cells("destroyed") | {("geonode", "tor1"): status})
    with pytest.raises(merge.RoundDataMissing,
                       match="geonode tor1 h01: round ran; data not collected"):
        merge.merge_network(root, "geonode")
    assert not list(path.parent.glob("regions/*/state.json"))


@pytest.mark.parametrize("status", [
    "create_failed", "assign_failed", "boot_failed", "start_failed",
    *[(s, {"armed_at": None, "started_at": None})
      for s in ("destroyed", "destroy_refused", "destroy_failed")],
])
def test_b1_never_started_workers_still_merge_as_not_started(root, status):
    finish_day(root)
    path = unpull(root, "geonode", "tor1", 1)
    write_hour(root, 1, _cells("destroyed") | {("geonode", "tor1"): ["boot_failed", status]})
    merge.merge_network(root, "geonode")
    state = read_json(path.parent / "regions/tor1/state.json")
    assert {j["status"] for j in state["jobs"] if j["round"] == 1} == {"not_started_deadline"}


def test_b1_completeness_shows_rounds_that_ran_but_were_not_collected(root):
    finish_day(root)
    unpull(root, "geonode", "tor1", 1)
    unpull(root, "geonode", "tor1", 2)
    write_hour(root, 1, _cells("destroyed") | {("geonode", "tor1"): "collect_failed"})
    result = completeness.build(root)
    rows = {(r["net"], r["region"], r["round"]): r["cell"] for r in result["rows"]}
    assert rows["geonode", "tor1", 1] == rows["geonode", "tor1", 2] == "uncollected"
    assert result["totals"]["uncollected"] == 2
    assert "geonode tor1 h01: data waits on a kept worker (collect_failed)" in result["issues"]
    assert "geonode tor1 h02: round ran; data not collected" in result["issues"]
    assert "⚠ data" in (root / "completeness" / "per-hour-tables.md").read_text()


def test_b2_a_killed_round_merges_with_its_jobs_closed(root):
    finish_day(root)
    kill_round(root, "shifter", "tor1", 0)
    merge.merge_network(root, "shifter")
    path = merge.manifest_path(root, "shifter")
    state = read_json(path.parent / "regions/tor1/state.json")
    assert [j["status"] for j in state["jobs"] if j["round"] == 0] == [
        "completed", "completed", "failed"]
    assert export_campaign(path)["rows"] > 0


def test_b2_a_killed_round_with_a_live_worker_is_still_running(root):
    finish_day(root)
    kill_round(root, "shifter", "tor1", 0)
    write_hour(root, 0, _cells("destroyed") | {("shifter", "tor1"): "running"})
    with pytest.raises(merge.CampaignNotFinished, match="shifter tor1 h00 is running"):
        merge.merge_network(root, "shifter")
    rows = {(r["net"], r["region"], r["round"]): r for r in completeness.build(root)["rows"]}
    assert rows["shifter", "tor1", 0]["cell"] == "running"


def test_b2_completeness_verifies_a_killed_round_as_failed_ours(root):
    kill_round(root, "shifter", "tor1", 0)
    result = completeness.build(root)
    row = {(r["net"], r["region"], r["round"]): r for r in result["rows"]}["shifter", "tor1", 0]
    assert (row["cell"], row["jobs_ok"], row["observations"], row["requests"]) == (
        "failed_ours", 2, 4, 6)
    assert "shifter tor1 h00: round ended without closing" in result["issues"]
    assert "shifter tor1 h00: PM: running" in result["issues"]


def test_m1_a_failed_round_is_our_failure_with_its_data_verified(root):
    path = round_directory(merge.manifest_path(root, "shifter"), "ams3", 1) / "round.json"
    atomic_json(path, read_json(path) | {"status": "failed"})
    row = {(r["net"], r["region"], r["round"]): r for r in completeness.build(root)["rows"]}[
        "shifter", "ams3", 1]
    assert (row["cell"], row["jobs_ok"], row["observations"]) == ("failed_ours", 12, 24)


def test_m2_m3_merge_cli_names_the_cell_and_writes_no_network(root, capsys):
    finish_day(root)
    write_hour(root, 23, _cells("create_failed") | {("shifter", "tor1"): "collect_failed"})
    assert main(["campaign", "merge", "--root", str(root)]) == 3
    assert capsys.readouterr().err.strip() == (
        "Not merged: shifter tor1 h23: data waits on a kept worker (collect_failed)")
    assert not list(root.glob("networks/*/residential/*/regions/*/state.json"))
    finish_day(root)
    assert main(["campaign", "merge", "--root", str(root)]) == 0
    assert len(list(root.glob("networks/*/residential/*/regions/*/state.json"))) == 4


def test_completeness_golden_tsv_and_tables(root, capsys):
    finish_day(root)
    path = round_directory(merge.manifest_path(root, "geonode"), "tor1", 0) / "round.json"
    atomic_json(path, read_json(path) | {"status": "running"})
    write_hour(root, 0, _cells("destroyed") | {("geonode", "tor1"): "running"})
    result = completeness.build(root)
    folder = root / "completeness"
    for name in ("per-hour-completeness.tsv", "per-hour-tables.md"):
        assert (folder / name).read_text() == (GOLDEN / name).read_text(), name
    assert result["cells"] == 2 * 2 * 24 and result["networks"]["shifter"]["cells"] == 48
    assert result["totals"] == {"complete": 9, "partial": 1, "failed_ours": 85,
                                "uncollected": 0, "running": 1, "pending": 0, "cells": 96}
    assert result["issues"] == ["shifter tor1 h01: GL: interrupted",
                                "shifter tor1 h01: PM: not started"]
    assert read_json(folder / "completeness.json") == result
    assert main(["campaign", "completeness", "--root", str(root)]) == 3
    assert json.loads(capsys.readouterr().out)["totals"]["complete"] == 9


def test_completeness_reports_pending_hours_and_the_network_protocol(root):
    result = completeness.build(root)
    assert result["totals"]["pending"] == 2 * 2 * 21
    assert {r["protocol"] for r in result["rows"]} == {"default"}
    assert completeness.protocol("byteful") == "https-only"
    assert completeness.protocol("not-a-grammar") == "default"


def test_a_tampered_chunk_is_caught(root):
    folder = round_directory(merge.manifest_path(root, "shifter"), "ams3", 0) / "nl"
    chunk = next(folder.glob("observations-*.jsonl.gz"))
    chunk.write_bytes(gzip.compress(b'{"ip": "198.51.100.7"}\n'))
    path = round_directory(merge.manifest_path(root, "geonode"), "tor1", 1) / "round.json"
    path.write_text("{}")
    rows = {(r["net"], r["region"], r["round"]): r for r in completeness.build(root)["rows"]}
    tampered = rows["shifter", "ams3", 0]
    assert (tampered["cell"], tampered["badchunk"], tampered["completed"]) == (
        "partial", 1, True)
    assert rows["geonode", "tor1", 1]["cell"] == "failed_ours"
    path.write_text(json.dumps({"status": "completed"}))
    result = completeness.build(root)
    assert "geonode tor1 h01: round failed verification (RoundRefused)" in result["issues"]
    assert "shifter ams3 h00: NL: failed verification (ValueError)" in result["issues"]


def test_no_ipv4_in_any_output(root):
    finish_day(root)
    for network in NETWORKS:
        merge.merge_network(root, network)
    completeness.build(root)
    outputs = [*root.glob("completeness/*"), *root.glob("ledger/*/hour.json"),
               *root.glob("networks/*/residential/*/regions/*/*.json")]
    assert len(outputs) == 3 + 24 + 2 * 2 * 2
    assert not [p.name for p in outputs if IPV4.search(p.read_text())]


# Collect: the "manager" is a local directory and commands run in a local shell.

def roomy(path):
    return shutil._ntuple_diskusage(10**12, 10**11, 9 * 10**11)


@pytest.fixture
def manager(root, tmp_path, monkeypatch):
    # The free-space rule is tested on its own below; collect tests never read the real disk.
    monkeypatch.setattr(collector.shutil, "disk_usage", roomy)
    campaigns = tmp_path / "manager" / "campaigns"
    remote = Path(shutil.copytree(root, campaigns / CID))
    (remote / ".runtime").mkdir(mode=0o700)
    (remote / ".runtime" / "hosts-00.json").write_text('{"700001": "203.0.113.10"}')
    inventory = tmp_path / "campaign-manager.json"
    inventory.write_text(json.dumps({"host": "192.0.2.10", "identity_file": "~/.ssh/k",
                                     "campaigns_root": str(campaigns)}))
    return remote, inventory


# macOS tar would add AppleDouble `._*` entries that a Linux manager never sends.
SHELL_ENV = os.environ | {"COPYFILE_DISABLE": "1"}
POPEN = subprocess.Popen  # kept: one test replaces `subprocess.Popen` for the SSH path


def sh(command, **kwargs):
    return POPEN(["sh", "-c", command], env=SHELL_ENV, **kwargs)


def local_shell(before_tar=None):
    def spawn(command):
        if command.startswith("tar ") and before_tar:
            before_tar()
        return sh(command, stdout=subprocess.PIPE)
    return spawn


def test_collect_downloads_and_verifies_every_file(manager, tmp_path):
    remote, inventory = manager
    dest = tmp_path / "archive" / f"campaign-{CID}"
    report = collector.collect(inventory, CID, dest, spawn=local_shell())
    files = [p for p in remote.rglob("*") if p.is_file() and ".runtime" not in p.parts]
    assert report["clean"] and report["files"] == report["verified"] == len(files) - 1
    assert (report["mismatches"], report["missing"], report["extra"]) == ([], [], [])
    assert not (dest / ".runtime").exists() and (dest / collector.MANIFEST).is_file()
    assert read_json(dest / collector.REPORT) == report
    with pytest.raises(ValueError, match="already holds files"):
        collector.collect(inventory, CID, dest, spawn=local_shell())


def test_collect_detects_a_change_a_missing_and_an_extra_file(manager, tmp_path):
    remote, inventory = manager

    def tamper():
        (remote / "status.json").write_text('{"changed": true}')
        (remote / "campaign-spec.json").unlink()
        (remote / "alerts.jsonl").write_text("{}\n")

    (remote / "status.json").write_text("{}")
    report = collector.collect(inventory, CID, tmp_path / "a", spawn=local_shell(tamper))
    assert not report["clean"]
    assert (report["mismatches"], report["missing"], report["extra"]) == (
        ["status.json"], ["campaign-spec.json"], ["alerts.jsonl"])


def test_collect_refuses_unsafe_or_failed_transfers(manager, tmp_path, monkeypatch):
    remote, inventory = manager

    def replace_manifest():
        (remote / collector.MANIFEST).write_text("")

    report = collector.collect(inventory, CID, tmp_path / "a", spawn=local_shell(
        replace_manifest))
    assert report["mismatches"] == [collector.MANIFEST] and not report["clean"]
    (remote / "link").symlink_to("/etc/hosts")
    with pytest.raises(RuntimeError, match="manifest failed"):
        collector.collect(inventory, CID, tmp_path / "b", spawn=local_shell())
    (remote / "link").unlink()
    with pytest.raises(RuntimeError, match="stream failed"):
        collector.collect(inventory, CID, tmp_path / "c", spawn=lambda c: sh(
            c + (" ; exit 2" if c.startswith("tar") else ""), stdout=subprocess.PIPE))
    monkeypatch.setattr(collector.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(
        100, 90, 10))
    with pytest.raises(ValueError, match="disk space"):
        collector.collect(inventory, CID, tmp_path / "d", spawn=local_shell())
    with pytest.raises(ValueError, match="campaign id"):
        collector.collect(inventory, "not-a-campaign", tmp_path / "e", spawn=local_shell())


def test_collect_cli_uses_ssh_from_the_inventory(manager, tmp_path, monkeypatch, capsys):
    _, inventory = manager
    commands = []

    def popen(argv, **kwargs):
        commands.append(argv)
        return sh(argv[-1], **kwargs)

    monkeypatch.setattr(collector.subprocess, "Popen", popen)
    dest = tmp_path / "archive"
    assert main(["campaign", "collect", "--inventory", str(inventory), "--campaign-id", CID,
                 "--dest", str(dest)]) == 0
    assert json.loads(capsys.readouterr().out)["clean"] is True
    assert commands[0][:-1] == [
        "ssh", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=15",
        "-i", str(Path("~/.ssh/k").expanduser()), "root@192.0.2.10"]
    (dest / "campaign-spec.json").write_text("{}")
    report = collector.verify_tree(dest, collector.parse_manifest(
        (dest / collector.MANIFEST).read_text() + "size 1\n")[0])
    assert report["mismatches"] == ["campaign-spec.json"] and not report["clean"]


@pytest.mark.parametrize("free, ok", [(3000, True), (2999, False), (1249, False)])
def test_free_space_keeps_a_fifth_of_the_disk_and_a_quarter_spare(monkeypatch, free, ok):
    # 1,000 bytes on a 10,000-byte disk: 2,000 must stay free afterwards (and 1,250 spare).
    monkeypatch.setattr(collector.shutil, "disk_usage",
                        lambda p: shutil._ntuple_diskusage(10000, 10000 - free, free))
    if ok:
        collector.check_space(Path("."), 1000)
    else:
        with pytest.raises(ValueError, match="disk space"):
            collector.check_space(Path("."), 1000)


@pytest.mark.parametrize("data, message", [
    ({"host": "manager.example"}, "does not appear to be an IPv4 or IPv6 address"),
    ({"host": "192.0.2.10", "user": "Root"}, "SSH user"),
    ({"host": "192.0.2.10", "campaigns_root": "relative"}, "absolute"),
])
def test_inventory_validation(tmp_path, data, message):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=message):
        collector.load_inventory(path)


@pytest.mark.parametrize("text", ["", "x", "abc  ./f\nsize 1", f"{'0' * 64}  ./../f\nsize 1"])
def test_manifest_parsing_rejects_bad_lines(text):
    with pytest.raises(ValueError):
        collector.parse_manifest(text)


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
    state = read_json(merge.manifest_path(root, "shifter").parent / "regions" / "ams3"
                      / "state.json")
    assert {j["status"] for j in state["jobs"] if j["round"] >= 3} == {"not_started_deadline"}
    assert {j["round"] for j in state["jobs"]} == set(range(24))
