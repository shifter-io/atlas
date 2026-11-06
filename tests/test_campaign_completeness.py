"""Campaign completeness: offline, RFC 5737 addresses only.

The built campaign and its ledger helpers are in `campaign_data_cases.py`; the merge is tested
in `test_campaign_data.py`.
"""

import gzip
import json
import re
import shutil
from pathlib import Path

from campaign_data_cases import NETWORKS, _cells, finish_day, kill_round, unpull, write_hour

from proxy_index.campaign import completeness, merge
from proxy_index.cli import main
from proxy_index.onboarding.export import export_campaign
from proxy_index.onboarding.files import atomic_json, read_json
from proxy_index.onboarding.rounds import round_directory

pytest_plugins = ["campaign_data_cases"]

GOLDEN = Path(__file__).parent / "fixtures" / "campaign-data"
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


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
