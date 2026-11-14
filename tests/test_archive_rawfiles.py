"""ARCHIVE-1 part 3 and ARCHIVE-RAW-1: the raw files of each update, spooled 30 days per network
and hour, rebuilt into a database (the upload: test_archive_rawhours.py)."""

import gzip
import json
import tarfile
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_archive import empty_database
from test_storage import enrichment, measurement

from atlas.archive import ArchiveError
from atlas.archive.rawfiles import rebuild, spool, try_spool
from atlas.cli import main
from atlas.enrichment import enrich_run
from atlas.orchestrator.archive import pack
from atlas.storage import connect

pytest_plugins = ["test_storage", "test_enrichment_cli"]

HOUR = datetime(2026, 10, 1, 10, tzinfo=UTC)


def raw(tmp_path, data=b"run bytes"):
    path = tmp_path / f"{uuid.uuid4().hex}.src"
    path.write_bytes(data)
    return path


def name(kind="pulse", batch=7):
    return f"{kind}-b{batch}-{uuid.uuid4()}.tar.gz"


def test_runs_are_spooled_by_hour_and_network(tmp_path):
    root = tmp_path / "spool"
    first = spool(root, HOUR + timedelta(minutes=3), "decodo", name(), raw(tmp_path), move=True)
    assert first.parent == root / "2026/10/01/10/decodo" and first.stat().st_mode & 0o777 == 0o600
    assert spool(root, HOUR, "decodo", first.name, raw(tmp_path, b"replay"), move=False) == first
    assert first.read_bytes() == b"run bytes"  # a replay keeps the first copy
    with pytest.raises(ArchiveError, match="not a raw file name"):
        spool(root, HOUR, "decodo", "../evil.tar.gz", raw(tmp_path), move=False)
    for network in ("../decodo", "Decodo", "", "a/b"):
        with pytest.raises(ArchiveError, match="not a network name"):
            spool(root, HOUR, network, name(), raw(tmp_path), move=False)
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the spool should be")
    assert try_spool(blocked, HOUR, "decodo", name(), raw(tmp_path), move=False) in (
        "NotADirectoryError",
        "FileExistsError",
    )
    assert try_spool(root, HOUR, "decodo", "bad", raw(tmp_path), move=False) == "ArchiveError"


async def test_a_run_directory_is_spooled_without_its_enrichment(tmp_path, fake_database):
    mmdb, _ = fake_database
    directory = await measurement(tmp_path / "daily")
    enrich_run(directory, mmdb)  # a rerun finds the directory already enriched
    assert any(p.name.startswith("enriched-") for p in directory.iterdir())
    file = f"daily-{directory.name}.tar.gz"
    kept = spool(tmp_path / "spool", HOUR, "shifter", file, directory, move=False)
    with tarfile.open(kept) as archive:
        assert archive.getnames() == ["ips.jsonl.gz", "summary.json"]


async def test_spooled_runs_rebuild_a_database(
    database, databases, tmp_path, capsys, fake_database, monkeypatch
):
    root = tmp_path / "spool"
    daily = await measurement(tmp_path / "daily")
    spool(root, HOUR, "fixture", f"daily-{daily.name}.tar.gz", daily, move=False)
    census = await measurement(tmp_path / "census")
    run_id = json.loads((census / "summary.json").read_text())["run_id"]
    upload = pack(census, tmp_path / "census.tar.gz")
    later = HOUR + timedelta(hours=1)
    spool(root, later, "fixture", f"census-b99-{run_id}.tar.gz", upload, move=True)
    # A pulse upload keeps the enrichment its droplet made and gets its IP rows (PULSE-ENRICH-1).
    pulse = await measurement(tmp_path / "pulse")
    enrichment(pulse, release="b" * 64)
    pulse_id = json.loads((pulse / "summary.json").read_text())["run_id"]
    pulse_file = f"pulse-b98-{pulse_id}.tar.gz"
    spool(root, later, "fixture", pulse_file, pack(pulse, tmp_path / "pulse.tar.gz"), move=True)
    # A run filed directly in the hour, enriched (the layout before ARCHIVE-RAW-1), is still
    # read, and its enrichment kept.
    mmdb, _ = fake_database
    old = await measurement(tmp_path / "old")
    enrich_run(old, mmdb)
    pack(old, root / "2026/10/01/11" / f"daily-{old.name}.tar.gz")
    # A daily run that enrichment refuses is skipped and listed, as daily ingest skips it.
    refused = await measurement(tmp_path / "refused")
    summary = json.loads((refused / "summary.json").read_text()) | {"pool_eligible": False}
    (refused / "summary.json").write_text(json.dumps(summary))
    spool(root, later, "fixture", f"daily-{refused.name}.tar.gz", refused, move=False)
    target = empty_database(databases)
    report = rebuild(root, target, date(2026, 10, 1), date(2026, 10, 1), mmdb=mmdb)
    assert report == {
        "runs": 5,
        "imported": 4,
        "unlinked": [f"census-b99-{run_id}.tar.gz", pulse_file],
        "not_enriched": [f"daily-{refused.name}.tar.gz"],
    }
    with connect(target) as conn:
        assert conn.execute("SELECT count(*) FROM test_runs").fetchone()[0] == 4
        # The daily runs were spooled raw, so the rebuild enriched them with the DB-IP file.
        assert conn.execute("SELECT count(*) FROM dbip_releases").fetchone()[0] == 2
        pulse_rows = conn.execute(
            "SELECT count(*) FROM ip_observations JOIN test_runs USING(run_key) WHERE run_id=%s",
            (pulse_id,),
        )
        assert pulse_rows.fetchone()[0] == 1
    # A replay is not enriched again (with another file it would add a second release).
    monkeypatch.setattr(gzip, "time", SimpleNamespace(time=lambda: 2_000_000_000.0))
    args = ["archive", "rebuild", "--spool", str(root), "--from", "2026-10-01", "--to"]
    args += ["2026-10-01", "--mmdb", str(mmdb)]
    assert main([*args, "--database-file", str(target)]) == 0
    replayed = json.loads(capsys.readouterr().out)
    assert replayed["imported"] == 0 and len(replayed["not_enriched"]) == 1  # replays
    assert rebuild(root, target, date(2026, 10, 2), date(2026, 10, 3))["runs"] == 0
