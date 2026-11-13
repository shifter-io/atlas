"""Browser bundles on the host (browser/rawfiles.py, K-BP4): recorder, seal, disk guard and
crash recovery, in-process. The outbox and its confirmation rule (the mini's side of K-BP7)
are in test_browser_rawfiles_outbox.py."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest
from browser_contracts_cases import try_fields
from browser_rawfiles_cases import (
    CAMOUFOX,
    MEMBERS,
    RUN_ID,
    SETTINGS_SHA,
    SLOT,
    make_run,
    members,
    seal_dir,
)
from test_browser_node import SHIFTER

from atlas.browser import contracts as bc
from atlas.browser import rawfiles, rawfiles_seal
from atlas.browser.rawfiles import Batch, DiskGuard, FileRecorder, Outbox, recover, seal
from atlas.errors import UserError
from atlas.orchestrator import browser_ingest

# --- recorder and seal ---------------------------------------------------------------------


def test_a_recorded_run_seals_to_the_k_bp4_members_manifest_and_sidecar(tmp_path):
    run_dir = make_run(tmp_path, requested=None)
    bundle = seal_dir(run_dir, tmp_path / "outbox")
    assert not run_dir.exists()
    assert bundle.archive == tmp_path / "outbox" / f"{RUN_ID}.tar.gz"
    assert sorted(m.name for m in members(bundle.archive)) == [
        MEMBERS.manifest,
        MEMBERS.screenshot(1),
        MEMBERS.tries,
        "ips.jsonl.gz",
        "summary.json",
    ]  # browser/batch.json is not a member
    info = members(bundle.archive)
    assert {(m.uid, m.gid, m.uname, m.gname, m.mode) for m in info} == {(0, 0, "", "", 0o600)}
    assert {m.mtime for m in info} == {int(SLOT.timestamp())}
    sidecar = json.loads(bundle.sidecar.read_text())
    data = bundle.archive.read_bytes()
    assert sidecar == {
        "batch_id": 41,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "network": SHIFTER,
        "target": "amazon_pdp",
        "country": "US",
        "slot": SLOT.isoformat(),
    }
    # The manager's own unpack and checks accept it (orchestrator/browser_ingest.py).
    work = tmp_path / "manager"
    work.mkdir()
    shutil.copy(bundle.archive, work / "bundle.tar.gz")
    summary, manifest = browser_ingest._open_bundle(work, 10 << 20)
    assert summary["run_id"] == manifest["run_id"] == RUN_ID
    assert manifest["tries"] == 3 and manifest["host"] == "browser-1"
    assert manifest["outcomes"]["verdicts"] == {
        "ok": 1, "challenge": 0, "block": 1, "timeout": 0, "proxy_error": 0, "error": 1
    }  # fmt: skip
    assert manifest["outcomes"]["errors"]["blocked"] == 1
    assert manifest["outcomes"]["errors"]["tls"] == 1
    # Untargeted (D11): taken from the run's config.
    assert (manifest["requested_country"], manifest["geo_targeting"]) == (None, False)
    assert [f["name"] for f in manifest["files"]] == [MEMBERS.screenshot(1), MEMBERS.tries]
    assert manifest["camoufox"] == CAMOUFOX and manifest["settings_sha256"] == SETTINGS_SHA


def test_a_targeted_run_records_its_requested_country(tmp_path):
    bundle = seal_dir(make_run(tmp_path), tmp_path / "outbox")
    with tarfile.open(bundle.archive, "r:gz") as tar:
        manifest = json.load(tar.extractfile(MEMBERS.manifest))
    assert (manifest["requested_country"], manifest["geo_targeting"]) == ("US", True)


def test_the_tar_gz_is_byte_identical_for_identical_inputs(tmp_path):
    one = seal_dir(make_run(tmp_path / "a"), tmp_path / "out-a")
    two = seal_dir(make_run(tmp_path / "b"), tmp_path / "out-b")  # other files, inodes, times
    assert one.archive.read_bytes() == two.archive.read_bytes()
    assert one.sha256 == two.sha256


def test_the_run_directory_is_removed_only_after_the_sidecar_exists(tmp_path, monkeypatch):
    run_dir = make_run(tmp_path)
    removed = []

    def checked_rmtree(path, *args, **kwargs):
        _, sidecar = bc.outbox_paths(tmp_path / "outbox", RUN_ID)
        removed.append((Path(path), sidecar.exists(), (Path(path) / "summary.json").exists()))
        return real(path, *args, **kwargs)

    real = shutil.rmtree
    monkeypatch.setattr(rawfiles_seal.shutil, "rmtree", checked_rmtree)
    seal_dir(run_dir, tmp_path / "outbox")
    assert removed == [(run_dir, True, True)]


def test_a_bundle_that_changes_while_sealed_keeps_its_run_directory(tmp_path, monkeypatch):
    run_dir = make_run(tmp_path)
    real = rawfiles.read_bundle
    monkeypatch.setattr(
        rawfiles_seal, "read_bundle", lambda sidecar: replace(real(sidecar), sha256="0" * 64)
    )
    with pytest.raises(OSError, match="changed"):
        seal_dir(run_dir, tmp_path / "outbox")
    assert run_dir.exists()


def test_a_recorder_needs_its_run_directory():
    recorder = FileRecorder(Batch(1, "US", SLOT, "browser-1"), SETTINGS_SHA)
    with pytest.raises(RuntimeError):
        recorder.try_done(bc.try_record(**try_fields()), None)


def test_a_screenshot_that_cannot_be_written_loses_only_the_screenshot(tmp_path, monkeypatch):
    real = rawfiles._write

    def full_disk(path, data, mode="wb", sync=True):
        if path.suffix == ".jpg":
            path.write_bytes(data[:1])  # a partial file is removed
            raise OSError(28, "No space left on device")
        return real(path, data, mode, sync)

    monkeypatch.setattr(rawfiles, "_write", full_disk)
    run_dir = make_run(tmp_path, shots=(1,))
    lines = (run_dir / MEMBERS.tries).read_text().splitlines()
    failed = json.loads(lines[1])
    assert failed["screenshot"] is None and failed["error"] == "blocked"
    assert list((run_dir / "browser" / "screenshots").iterdir()) == []


# --- disk guard ----------------------------------------------------------------------------


def test_at_the_limit_no_screenshots_are_written_tries_continue_nothing_is_deleted(tmp_path):
    keep = tmp_path / "outbox" / "old.tar.gz"
    keep.parent.mkdir()
    keep.write_bytes(b"x" * 4096)
    notes = []
    now = [0.0]
    guard = DiskGuard(
        (tmp_path / "runs", tmp_path / "outbox"), 1e-9, notify=notes.append, clock=lambda: now[0]
    )
    run_dir = make_run(tmp_path, guard=guard, shots=(0, 1, 2))
    lines = (run_dir / MEMBERS.tries).read_text().splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["screenshot"] for line in lines] == [None, None, None]
    assert list((run_dir / "browser" / "screenshots").iterdir()) == []
    assert keep.read_bytes() == b"x" * 4096
    assert notes == [{"kind": "browser_outbox_full", "limit_bytes": 1}]  # once per hour
    now[0] = rawfiles.FULL_LOG_EVERY_S
    assert guard.full() and len(notes) == 2
    bundle = seal_dir(run_dir, tmp_path / "outbox")
    assert MEMBERS.screenshot(1) not in [m.name for m in members(bundle.archive)]


def test_below_the_limit_screenshots_are_written(tmp_path):
    guard = DiskGuard((tmp_path / "runs",), 1.0)
    run_dir = make_run(tmp_path, guard=guard, shots=(0, 2))
    names = sorted(p.name for p in (run_dir / "browser" / "screenshots").iterdir())
    assert names == ["0000.jpg", "0002.jpg"] and not guard.full()


# --- recovery ------------------------------------------------------------------------------


def test_recovery_seals_rebuilds_and_cleans_but_never_deletes_unconfirmed(tmp_path):
    runs, outbox = tmp_path / "runs", tmp_path / "outbox"
    ids = [f"0b6a7e4c-1d2e-4f3a-9b8c-7d6e5f4a3b2{n}" for n in range(5)]
    finished = make_run(tmp_path, ids[0])  # killed before its seal
    half = make_run(tmp_path, ids[1])  # killed while packing: a tar.gz, no sidecar
    outbox.mkdir()
    bc.outbox_paths(outbox, ids[1])[0].write_bytes(b"half a tar")
    sealed = make_run(tmp_path, ids[2])  # killed after its sidecar, before removing the run
    shutil.copytree(sealed, tmp_path / "keep")
    seal_dir(sealed, outbox)
    shutil.copytree(tmp_path / "keep", sealed)
    killed = make_run(tmp_path, ids[3])  # killed during its run: no summary.json, a half run
    (killed / "summary.json").unlink()
    confirmed = bc.outbox_paths(outbox, ids[4])[1]  # confirmed: its tar.gz already gone
    confirmed.write_text("{}")
    (outbox / f"{ids[0]}.json.partial").write_text("{")
    (runs / "shifter.residential" / "not-a-run").mkdir()

    def seal_run(run_dir, batch, summary, sha):
        return seal(run_dir, batch, summary, sha, CAMOUFOX, outbox=outbox)

    assert recover(runs, outbox, seal_run) == (1, 0, 0)
    assert not finished.exists() and not half.exists() and not sealed.exists()
    assert not killed.exists() and not bc.outbox_paths(outbox, ids[3])[0].exists()
    assert not confirmed.exists() and not (outbox / f"{ids[0]}.json.partial").exists()
    pending = Outbox(outbox).pending()
    assert sorted(b.run_id for b in pending) == ids[:3]
    rebuilt = next(b for b in pending if b.run_id == ids[1])
    assert hashlib.sha256(rebuilt.archive.read_bytes()).hexdigest() == rebuilt.sha256


def test_recovery_keeps_a_run_it_cannot_seal(tmp_path, caplog):
    run_dir = make_run(tmp_path)
    (run_dir / MEMBERS.tries).write_text("not json\n")
    caplog.set_level(logging.WARNING)

    def seal_run(run_dir, batch, summary, sha):
        return seal(run_dir, batch, summary, sha, CAMOUFOX, outbox=tmp_path / "outbox")

    assert recover(tmp_path / "runs", tmp_path / "outbox", seal_run) == (0, 1, 0)
    assert run_dir.exists() and "kept unsealed (JSONDecodeError)" in caplog.text


@pytest.mark.parametrize("sidecar", ["{", '{"batch_id": 41, "newer_field": 1}'])
def test_an_unreadable_sidecar_beside_a_run_never_stops_recovery(tmp_path, caplog, sidecar):
    """Review F1: a corrupted sidecar, or one from a newer build after a rollback, beside a
    leftover run directory: that run is kept with its files and counted, the others sealed."""
    outbox = tmp_path / "outbox"
    ids = [f"0b6a7e4c-1d2e-4f3a-9b8c-7d6e5f4a3b2{n}" for n in range(2)]
    stuck, other = make_run(tmp_path, ids[0]), make_run(tmp_path, ids[1])
    shutil.copytree(stuck, tmp_path / "keep")
    seal_dir(stuck, outbox)
    shutil.copytree(tmp_path / "keep", stuck)
    archive, side = bc.outbox_paths(outbox, ids[0])
    side.write_text(sidecar)
    caplog.set_level(logging.WARNING)

    def seal_run(run_dir, batch, summary, sha):
        return seal(run_dir, batch, summary, sha, CAMOUFOX, outbox=outbox)

    assert recover(tmp_path / "runs", outbox, seal_run) == (0, 1, 0)
    assert stuck.exists() and archive.exists() and side.read_text() == sidecar
    assert not other.exists() and [b.run_id for b in Outbox(outbox).pending()] == [ids[1]]
    assert f"run {ids[0]} kept unsealed (" in caplog.text  # its error type only


def test_a_sidecar_that_cannot_be_removed_never_stops_recovery(tmp_path, monkeypatch, caplog):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    orphan = bc.outbox_paths(outbox, RUN_ID)[1]
    orphan.write_text("{}")
    real = Path.unlink

    def refuse(self, missing_ok=False):
        if self == orphan:
            raise PermissionError(13, "denied")
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse)
    caplog.set_level(logging.WARNING)
    assert recover(tmp_path / "runs", outbox, lambda *a: None) == (0, 0, 0)
    assert orphan.exists() and "kept (PermissionError)" in caplog.text


def test_a_sidecar_whose_tar_gz_differs_is_rebuilt_from_its_run(tmp_path):
    outbox = tmp_path / "outbox"
    run_dir = make_run(tmp_path)
    shutil.copytree(run_dir, tmp_path / "keep")
    bundle = seal_dir(run_dir, outbox)
    shutil.copytree(tmp_path / "keep", run_dir)
    bundle.archive.write_bytes(b"truncated")

    def seal_run(run_dir, batch, summary, sha):
        return seal(run_dir, batch, summary, sha, CAMOUFOX, outbox=outbox)

    assert recover(tmp_path / "runs", outbox, seal_run) == (0, 0, 0)
    [rebuilt] = Outbox(outbox).pending()
    assert not run_dir.exists() and rebuilt.sha256 == bundle.sha256
    assert hashlib.sha256(rebuilt.archive.read_bytes()).hexdigest() == bundle.sha256


def test_a_half_run_is_deleted_at_start_and_nothing_finished_or_unconfirmed(tmp_path, caplog):
    """BP-DATA-HALF-1: a run directory without summary.json (killed mid-run) is deleted, never
    sealed or uploaded, and logged once with its run id; a finished run with summary.json and
    the outbox's bundles (pending, marked) are untouched, and the disk guard no longer counts
    the half run."""
    runs, outbox = tmp_path / "runs", tmp_path / "outbox"
    ids = [f"0b6a7e4c-1d2e-4f3a-9b8c-7d6e5f4a3b2{n}" for n in range(5)]
    half = make_run(tmp_path, ids[0])
    (half / "summary.json").unlink()
    (half / "browser" / "screenshots" / "big.bin").write_bytes(b"x" * 65536)
    bare = runs / "shifter.residential" / ids[1]  # killed before bind(): an empty directory
    bare.mkdir()
    linked = make_run(tmp_path, ids[2])  # summary.json there but not a regular file: kept
    (linked / "summary.json").unlink()
    (linked / "summary.json").symlink_to(tmp_path / "gone.json")
    factless = make_run(tmp_path, ids[4])  # finished, but its batch facts are gone: kept
    (factless / rawfiles.BATCH_FILE).unlink()
    pending = seal_dir(make_run(tmp_path / "p", RUN_ID), outbox)
    marked = seal_dir(make_run(tmp_path / "m", ids[3]), outbox)
    marked.sidecar.with_suffix(".rejected").write_text("{}")
    kept = {path: path.read_bytes() for path in outbox.iterdir()}
    sealed = []
    caplog.set_level(logging.WARNING)
    guard = DiskGuard((runs,), 32768 / rawfiles.GB)
    assert guard.full()

    def seal_run(run_dir, batch, summary, sha):
        sealed.append(run_dir.name)
        raise AssertionError("no run here is sealed")

    assert recover(runs, outbox, seal_run) == (2, 2, 0)
    assert not half.exists() and not bare.exists() and sealed == []
    assert linked.exists() and (factless / "summary.json").exists()
    assert f"run {factless.name} kept unsealed (no {rawfiles.BATCH_FILE})" in caplog.text
    assert {path: path.read_bytes() for path in outbox.iterdir()} == kept
    assert Outbox(outbox).pending() == [pending] and marked in Outbox(outbox).bundles()
    assert caplog.text.count("browser_run_discarded") == 2
    for run_id in ids[:2]:
        assert caplog.text.count(f"browser_run_discarded: run {run_id} ") == 1
    guard.every = 0
    assert not guard.full()


def test_a_half_run_that_cannot_be_deleted_counts_discard_failed(tmp_path, monkeypatch, caplog):
    """BP-HOST-FIXES-1 (BP-DATA-HALF-1 review N1): a half run whose delete fails is counted
    `discard_failed`, not `unsealed`, and the next start deletes it."""
    runs, outbox = tmp_path / "runs", tmp_path / "outbox"
    half = make_run(tmp_path, RUN_ID)
    (half / "summary.json").unlink()

    def refuse(path, *args, **kwargs):
        raise PermissionError("read-only")

    def seal_run(*args):
        raise AssertionError("a half run is never sealed")

    caplog.set_level(logging.INFO)
    with monkeypatch.context() as patched:
        patched.setattr(rawfiles_seal.shutil, "rmtree", refuse)
        assert recover(runs, outbox, seal_run) == (0, 0, 1)
    assert half.exists() and f"browser_run_discard_failed: run {RUN_ID} (PermissionError)" in (
        caplog.text
    )
    assert "browser_recover: sealed=0 discarded=0 kept=0 discard_failed=1" in caplog.text
    assert recover(runs, outbox, seal_run) == (1, 0, 0) and not half.exists()


def test_the_host_lock_names_a_state_directory_it_cannot_open(tmp_path):
    refused = r"cannot open the host lock .*host\.lock \(FileNotFoundError\)"
    with pytest.raises(UserError, match=refused):
        with rawfiles.host_lock(tmp_path / "absent"):
            raise AssertionError("not reached")
