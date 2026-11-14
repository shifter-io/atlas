"""DAILY-1: the daily test's ingest, offline.

The ingest path: a real round through the production runner with a fake transport, a fake DB-IP
database and an isolated PostgreSQL, and the `daily ingest` CLI. No droplet, request or database
outside the test is touched.
"""

from __future__ import annotations

import gzip
import json
import tarfile

import pytest
from campaign_fakes import CID
from campaign_hour_cases import make_spec
from daily_cases import chunk, daily_root, observation
from onboarding_round_cases import make
from test_onboarding import START

from atlas.campaign.contracts import spec_path
from atlas.cli import main
from atlas.daily import ingest as daily_ingest
from atlas.onboarding.files import read_json
from atlas.orchestrator.db import Database
from atlas.storage import connect, seed

pytest_plugins = ["test_storage", "test_enrichment_cli"]


def test_observations_become_ip_rows_per_bucket_and_are_converted_once(tmp_path):
    folder = tmp_path / "ca"
    chunk(
        folder,
        "observations-000001.jsonl.gz",
        [
            observation("192.0.2.1", "2027-01-01T22:00:01Z"),
            observation("192.0.2.1", "2027-01-01T22:01:00Z"),
        ],
    )
    chunk(
        folder,
        "observations-000002.jsonl.gz",
        [
            observation("192.0.2.1", "2027-01-01T22:05:00Z"),
            observation("192.0.2.2", "2027-01-01T22:00:00Z"),
        ],
    )
    summary = {"run_id": "r1", "schema_version": "1"}
    (folder / "summary.json").write_text(json.dumps({"measurement": summary}))
    directory = daily_ingest.convert_country(folder, tmp_path / "runs")
    with gzip.open(directory / "ips.jsonl.gz", "rt") as stream:
        rows = [json.loads(line) for line in stream]
    assert [(r["ip"], r["bucket_start"], r["count"]) for r in rows] == [
        ("192.0.2.1", "2027-01-01T22:00:00.000000+00:00", 2),
        ("192.0.2.2", "2027-01-01T22:00:00.000000+00:00", 1),
        ("192.0.2.1", "2027-01-01T22:05:00.000000+00:00", 1),
    ]
    assert rows[0]["first_seen"] == "2027-01-01T22:00:01.000000+00:00" and rows[0]["run_id"] == "r1"
    written = read_json(directory / "summary.json")
    assert written["artifacts"]["ips"]["rows"] == 3 and written["run_id"] == "r1"
    assert written["test_kind"] == "daily"  # the daily-test marker (atlas.daily)
    (directory / "ips.jsonl.gz").write_bytes(b"kept")
    assert daily_ingest.convert_country(folder, tmp_path / "runs") == directory
    assert (directory / "ips.jsonl.gz").read_bytes() == b"kept"
    (folder / "summary.json").write_text(json.dumps({"measurement": None}))
    with pytest.raises(ValueError, match="no measurement"):
        daily_ingest.convert_country(folder, tmp_path / "other")


async def test_a_collected_test_is_imported_and_its_events_emitted_once(
    tmp_path, monkeypatch, database, fake_database
):
    import atlas.runner as runner

    monkeypatch.setattr(runner.time, "time", lambda: START.timestamp() + 1)
    seed(database)  # db-seed: the runner regions the events reference
    root = await daily_root(tmp_path)
    mmdb, _ = fake_database
    report = await daily_ingest.ingest_campaign(root, database, mmdb)
    tor1, fra1 = report["cells"]["shifter/tor1"], report["cells"]["shifter/fra1"]
    assert (tor1["status"], tor1["ingested"], tor1["events"]) == ("done", ["CA", "PM"], True)
    assert tor1["requests"] == 6 and tor1["unique_ips"] == 2 and tor1["issues"] == []
    assert fra1["status"] == "failed" and fra1["issues"][0].startswith("round unreadable")
    # fra1 comes first, before shifter's product exists: its events wait for the rerun.
    assert fra1["events"] is False and report["complete"] is False
    assert kinds(database) == ["daily_test_finished", "daily_test_started"]
    spool = tmp_path / "spool"
    again = await daily_ingest.ingest_campaign(root, database, mmdb, spool=spool)
    assert again["cells"]["shifter/tor1"]["ingested"] == ["CA", "PM"]
    # The rerun's update used both runs: each is spooled once, as a .tar.gz of its directory,
    # in its network's folder and raw: without the DB-IP enrichment the rerun found.
    files = sorted(spool.glob("*/*/*/*/shifter/daily-*.tar.gz"))
    spooled = [p.name for p in files]
    assert spooled == sorted(f"daily-{d.name}.tar.gz" for d in (root / "runs").iterdir())
    for path in files:
        with tarfile.open(path) as archive:
            assert archive.getnames() == ["ips.jsonl.gz", "summary.json"]
    assert again["cells"]["shifter/fra1"]["events"] is True and again["complete"] is False
    assert kinds(database) == ["daily_test_finished"] * 2 + ["daily_test_started"] * 2
    assert len(list((root / "runs").iterdir())) == 2
    with connect(database) as conn:
        marked = "SELECT count(*) FROM test_runs WHERE summary->>'test_kind'='daily'"
        assert conn.execute(marked).fetchone()[0] == 2
    text = (root / daily_ingest.REPORT).read_text()
    seen = set()
    for ips in (root / "runs").glob("*/ips.jsonl.gz"):
        with gzip.open(ips, "rt") as stream:
            seen |= {json.loads(line)["ip"] for line in stream}
    assert seen and not any(ip in text for ip in seen) and str(tmp_path) not in text
    # A spool that cannot be written (a file where its folder should be) only raises an alert:
    # the imports already committed, so the countries stay ingested and the cell done.
    blocked = tmp_path / "blocked-spool"
    blocked.write_text("not a directory")
    third = await daily_ingest.ingest_campaign(root, database, mmdb, spool=blocked)
    tor1 = third["cells"]["shifter/tor1"]
    assert (tor1["status"], tor1["ingested"], tor1["issues"]) == ("done", ["CA", "PM"], [])
    assert [issue.split(":")[0] for issue in tor1["spool_issues"]] == ["CA", "PM"]
    with connect(database) as conn:
        alerts = "SELECT severity,data->>'cell' FROM alerts WHERE kind='raw_spool_failed'"
        assert conn.execute(alerts).fetchall() == [("major", "shifter/tor1")]


def kinds(database):
    with connect(database) as conn:
        return sorted(row[0] for row in conn.execute("SELECT kind FROM test_events"))


async def test_a_network_never_imported_gets_no_events_until_it_is(tmp_path, database):
    root = tmp_path / "daily"
    path = make(root, due_countries={"tor1": ["CA"]})
    spec = make_spec(
        networks=("shifter",),
        regions=("tor1",),
        manifests={"shifter": path.relative_to(root).as_posix()},
    )
    spec_path(root).parent.mkdir(parents=True, exist_ok=True)
    spec_path(root).write_text(spec.to_json())
    db = Database(str(database), size=1)
    try:
        report = await daily_ingest.ingest_campaign(root, database, database=db)
    finally:
        await db.close()
    cell = report["cells"]["shifter/tor1"]
    assert cell["status"] == "failed" and cell["events"] is False


def test_ingest_cli_refuses_a_download_that_did_not_verify(tmp_path, capsys):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "collect-report.json").write_text(json.dumps({"clean": False}))
    args = ["daily", "ingest", "--inventory", str(tmp_path / "inv"), "--campaign-id", CID]
    args += ["--dest", str(dest), "--database-file", str(tmp_path / "db.toml")]
    assert main(args) == 3
    assert "did not verify" in capsys.readouterr().err


def test_ingest_cli_downloads_once_then_imports(tmp_path, monkeypatch, capsys):
    import atlas.campaign.collect as collect

    downloads = []

    def fake_collect(inventory, campaign_id, dest):
        downloads.append(campaign_id)
        (dest / collect.REPORT).write_text(json.dumps({"clean": True}))

    async def fake_ingest(root, credentials, mmdb, *, formulas=None, spool=None):
        return {"campaign_id": CID, "complete": len(downloads) == 1}

    monkeypatch.setattr(collect, "collect", fake_collect)
    monkeypatch.setattr(daily_ingest, "ingest_campaign", fake_ingest)
    dest = tmp_path / "dest"
    dest.mkdir()
    args = ["daily", "ingest", "--inventory", str(tmp_path / "inv"), "--campaign-id", CID]
    args += ["--dest", str(dest), "--database-file", str(tmp_path / "db.toml")]
    assert main(args) == 0 and json.loads(capsys.readouterr().out)["complete"] is True
    downloads.append("not again")
    assert main(args) == 3 and downloads == [CID, "not again"]  # the rerun reuses the download
