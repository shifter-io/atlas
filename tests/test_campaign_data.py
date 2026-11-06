"""Campaign merge and completeness: offline, RFC 5737 addresses only.

The built campaign and its ledger helpers are in `campaign_data_cases.py`; verified collect is
tested in `test_campaign_collect.py`.
"""

import gzip
import json
import re
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from campaign_data_cases import NETWORKS, REGIONS, _cells, _droplet, finish_day, write_hour
from test_onboarding import ROOT, START

from proxy_index.campaign import completeness, merge
from proxy_index.campaign.contracts import CampaignSpec, spec_path
from proxy_index.cli import main
from proxy_index.onboarding import manifest as campaign
from proxy_index.onboarding.export import export_campaign
from proxy_index.onboarding.files import atomic_json, read_json
from proxy_index.onboarding.rounds import round_directory
from proxy_index.onboarding.scheduler import validate_state

pytest_plugins = ["campaign_data_cases"]

GOLDEN = Path(__file__).parent / "fixtures" / "campaign-data"
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


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
    for key in (
        "finished_at",
        "observations",
        "duration_seconds",
        "requests",
        "errors",
        "stop_reason",
    ):
        last.pop(key)
    last["status"] = "running"
    state.update(
        status="running",
        active={"round": hour, "country": last["country"], "started_at": last["started_at"]},
    )
    atomic_json(folder / "round.json", state)
    (folder / last["country"].lower() / "summary.json").unlink()


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


def test_b2_completeness_verifies_a_killed_round_as_failed_ours(root):
    kill_round(root, "shifter", "tor1", 0)
    result = completeness.build(root)
    row = {(r["net"], r["region"], r["round"]): r for r in result["rows"]}["shifter", "tor1", 0]
    assert (row["cell"], row["jobs_ok"], row["observations"], row["requests"]) == (
        "failed_ours",
        2,
        4,
        6,
    )
    assert "shifter tor1 h00: round ended without closing" in result["issues"]
    assert "shifter tor1 h00: PM: running" in result["issues"]


def test_m1_a_failed_round_is_our_failure_with_its_data_verified(root):
    path = round_directory(merge.manifest_path(root, "shifter"), "ams3", 1) / "round.json"
    atomic_json(path, read_json(path) | {"status": "failed"})
    row = {(r["net"], r["region"], r["round"]): r for r in completeness.build(root)["rows"]}[
        "shifter", "ams3", 1
    ]
    assert (row["cell"], row["jobs_ok"], row["observations"]) == ("failed_ours", 12, 24)


def test_a_no_list_country_completes_its_cell_and_merges(root):
    # A country the provider has no list for ran no traffic and is complete with that reason.
    folder = round_directory(merge.manifest_path(root, "shifter"), "tor1", 0)
    state = read_json(folder / "round.json")
    job = state["jobs"][1]
    assert job["country"] == "GL"
    state["jobs"][1] = {key: job[key] for key in ("round", "country", "scheduled_at")} | {
        "status": "no_list",
        "stop_reason": "no_list",
    }
    atomic_json(folder / "round.json", state)
    shutil.move(folder / "gl", root.parent / "gl")
    row = {(r["net"], r["region"], r["round"]): r for r in completeness.build(root)["rows"]}[
        "shifter", "tor1", 0
    ]
    assert (row["cell"], row["completed"], row["jobs_ok"], row["missing"]) == (
        "complete",
        True,
        2,
        0,
    )
    assert row["finished"] == state["jobs"][2]["finished_at"]
    finish_day(root)
    regions = {s["region"]: s for s in merge.merge_network(root, "shifter")}
    path = merge.manifest_path(root, "shifter")
    issues = export_campaign(path)["issues"]
    # The same counts and export issues as when GL completed (no_list is not unfinished).
    state["jobs"][1] = job
    atomic_json(folder / "round.json", state)
    shutil.move(root.parent / "gl", folder / "gl")
    expected = {s["region"]: s for s in merge.merge_network(root, "shifter")}
    assert regions["tor1"]["unfinished_countries"] == expected["tor1"]["unfinished_countries"]
    assert regions["tor1"]["completed_countries"] == expected["tor1"]["completed_countries"] - 1
    assert issues == [i for i in export_campaign(path)["issues"] if "GL" not in i]


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
    assert result["totals"] == {
        "complete": 9,
        "partial": 1,
        "failed_ours": 85,
        "uncollected": 0,
        "running": 1,
        "pending": 0,
        "paused": 0,
        "cells": 96,
    }
    assert result["issues"] == [
        "shifter tor1 h01: GL: interrupted",
        "shifter tor1 h01: PM: not started",
    ]
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
    assert (tampered["cell"], tampered["badchunk"], tampered["completed"]) == ("partial", 1, True)
    assert rows["geonode", "tor1", 1]["cell"] == "failed_ours"
    path.write_text(json.dumps({"status": "completed"}))
    result = completeness.build(root)
    assert "geonode tor1 h01: round failed verification (RoundRefused)" in result["issues"]
    assert "shifter ams3 h00: NL: failed verification (ValueError)" in result["issues"]


def rows_by_cell(result: dict) -> dict:
    return {(r["net"], r["region"], r["round"]): r for r in result["rows"]}


def test_completeness_splits_answer_classes(root, monkeypatch):
    """CTRY-1: a round measured with answer classes (RL-1's country fields) shows rate limits,
    no-IP answers and other-country exits apart from real errors and duplicates."""
    plain = rows_by_cell(completeness.build(root))["shifter", "ams3", 0]
    verify = completeness.verify_round

    def classified(path, region, hour):
        result = verify(path, region, hour)
        if ("shifter" in path.parts, region, hour) == (True, "ams3", 0):
            nl = next(c for c in result["countries"] if c["country"] == "NL")
            nl |= {"rate_limited": 10, "no_inventory": 2, "other_country": 3}
        return result

    monkeypatch.setattr(completeness, "verify_round", classified)
    result = completeness.build(root)
    row = rows_by_cell(result)["shifter", "ams3", 0]
    assert (row["rate_limited"], row["no_inventory"], row["other_country"]) == (10, 2, 3)
    assert row["real_errors"] == plain["errors"] - 12 and row["errors"] == plain["errors"]
    assert (
        row["duplicates"]
        == plain["duplicates"]
        == row["observations"] - sum(c["unique_ips"] for c in row["per_country"])
    )
    nl = next(c for c in row["per_country"] if c["country"] == "NL")
    assert (
        nl["answered"] == nl["requests"] - 10
        and nl["real_errors"] == nl["requests"] - (nl["successes"]) - 12
    )
    tsv = (root / "completeness" / "per-country.tsv").read_text().splitlines()
    assert "shifter\tams3\t0\tNL\tcompleted\t" in "\n".join(tsv)
    line = next(x.split("\t") for x in tsv if x.startswith("shifter\tams3\t0\tNL\t"))
    assert dict(zip(completeness.COUNTRY_COLUMNS, line, strict=True))["rate_limited"] == "10"
    tables = (root / "completeness" / "per-hour-tables.md").read_text()
    section = tables.split("## Answer classes per network and region")[1]
    ams3 = next(x for x in section.splitlines() if x.startswith("| shifter | ams3 |"))
    requests = sum(
        r["requests"]
        for r in result["rows"]
        if r["net"] == "shifter" and r["region"] == "ams3" and r["requests"]
    )
    assert ams3.split(" | ")[2:6] == [
        str(requests),
        "10",
        str(requests - 10),
        str(
            sum(
                r["real_errors"]
                for r in result["rows"]
                if (r["net"], r["region"]) == ("shifter", "ams3") and r["requests"]
            )
        ),
    ]


def test_per_country_tsv(root):
    result = completeness.build(root)
    lines = (root / "completeness" / "per-country.tsv").read_text().splitlines()
    assert lines[0].split("\t") == list(completeness.COUNTRY_COLUMNS)
    entries = [(r, c) for r in result["rows"] for c in r.get("per_country", [])]
    assert len(lines) == 1 + len(entries) and entries
    row, country = entries[0]
    assert lines[1].split("\t")[:7] == [
        row["net"],
        row["region"],
        str(row["round"]),
        country["country"],
        country["status"],
        country["stop_reason"] or "",
        str(country["requests"]),
    ]
    assert not IPV4.search("\n".join(lines))


def test_old_rounds_have_zero_answer_classes(root):
    """Rounds measured before the answer classes count 0 rate limits, no-IP and other-country
    answers; real errors are their errors; the TSV only appends the five columns."""
    measured = [r for r in completeness.build(root)["rows"] if r["requests"] is not None]
    assert measured and all(
        (r["rate_limited"], r["no_inventory"], r["other_country"]) == (0, 0, 0)
        and r["real_errors"] == r["errors"]
        for r in measured
    )
    assert completeness.COLUMNS == completeness.DATA1_COLUMNS + (
        "cell",
        "size",
        "start_lateness_s",
        "create_to_ready_s",
        "overlap",
        "bytes_up",
        "bytes_down",
        "protocol",
        "rate_limited",
        "no_inventory",
        "other_country",
        "duplicates",
        "real_errors",
    )
    old = {
        "net": "n",
        "region": "r",
        "round": 0,
        "requests": 5,
        "errors": 2,
        "cell": "partial",
        "rate_limited": None,
        "real_errors": None,
    }
    assert completeness._mark(old | {"requests": None}) == "partial 0"


def test_no_ipv4_in_any_output(root):
    finish_day(root)
    for network in NETWORKS:
        merge.merge_network(root, network)
    completeness.build(root)
    outputs = [
        *root.glob("completeness/*"),
        *root.glob("ledger/*/hour.json"),
        *root.glob("networks/*/residential/*/regions/*/*.json"),
    ]
    assert len(outputs) == 4 + 24 + 2 * 2 * 2
    assert not [p.name for p in outputs if IPV4.search(p.read_text())]


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
