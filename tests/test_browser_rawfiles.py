"""Browser bundles on the host (browser/rawfiles.py, K-BP4 and the mini's side of K-BP7):
recorder, seal, outbox and its confirmation rule, disk guard and crash recovery, against a fake
orchestrator in-process (no manager, no Spaces)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import tarfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from test_browser_contracts import try_fields
from test_browser_node import SHIFTER

from proxy_index.browser import contracts as bc
from proxy_index.browser import rawfiles
from proxy_index.browser.rawfiles import Batch, DiskGuard, FileRecorder, Outbox, recover, seal
from proxy_index.node import NodeAgent
from proxy_index.orchestrator import browser_ingest

SLOT = datetime(2026, 11, 4, 12, tzinfo=UTC)
RUN_ID = "0b6a7e4c-1d2e-4f3a-9b8c-7d6e5f4a3b2c"
SETTINGS_SHA = "a" * 64
CAMOUFOX = {
    "release": "158.0.1-beta.41",
    "engine_version": "158.0.1",
    "user_agent": bc.user_agent("158.0.1", "macos"),
}
MEMBERS = bc.bundle_members()


def summary_of(run_id=RUN_ID, requested="us", provider="shifter"):
    return {
        "run_id": run_id,
        "target": {"id": "amazon_pdp"},
        "config": {"provider": provider, "product": "residential", "requested_country": requested},
        "started_at": (SLOT + timedelta(minutes=1)).isoformat(),
        "finished_at": (SLOT + timedelta(minutes=9)).isoformat(),
        "status": "completed",
    }


def make_run(root: Path, run_id=RUN_ID, slot=SLOT, guard=None, shots=(1,), **summary) -> Path:
    """A finished run directory as the node and the recorder leave it: two failures, one ok."""
    run_dir = root / "runs" / "shifter.residential" / run_id
    run_dir.mkdir(parents=True)
    recorder = FileRecorder(Batch(41, "US", slot, "browser-1"), SETTINGS_SHA, guard)
    recorder.bind(run_dir)
    for i, (verdict, error) in enumerate((("ok", None), ("block", "blocked"), ("error", "tls"))):
        shot = MEMBERS.screenshot(i) if i in shots else None
        record = bc.try_record(**try_fields(i=i, verdict=verdict, error=error, screenshot=shot))
        recorder.try_done(record, b"\xff\xd8jpeg" if shot else None)
    (run_dir / "summary.json").write_text(json.dumps(summary_of(run_id, **summary)))
    (run_dir / "ips.jsonl.gz").write_bytes(b"\x1f\x8b-not-checked")
    return run_dir


def seal_dir(run_dir: Path, outbox: Path):
    batch, sha = rawfiles.run_facts(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text())
    return seal(run_dir, batch, summary, sha, CAMOUFOX, outbox=outbox)


def members(archive: Path) -> list[tarfile.TarInfo]:
    with tarfile.open(archive, "r:gz") as tar:
        return tar.getmembers()


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
    monkeypatch.setattr(rawfiles.shutil, "rmtree", checked_rmtree)
    seal_dir(run_dir, tmp_path / "outbox")
    assert removed == [(run_dir, True, True)]


def test_a_bundle_that_changes_while_sealed_keeps_its_run_directory(tmp_path, monkeypatch):
    run_dir = make_run(tmp_path)
    real = rawfiles.read_bundle
    monkeypatch.setattr(
        rawfiles, "read_bundle", lambda sidecar: replace(real(sidecar), sha256="0" * 64)
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


# --- outbox: confirmation, markers, retries -----------------------------------------------


class Ingest:
    """A fake orchestrator's /v1/results speaking K-BP7: `replies` answer in turn (the last
    repeats); "ok" is the confirming 200 for the bytes received."""

    def __init__(self, replies) -> None:
        self.replies = list(replies)
        self.posts: list[dict] = []
        self.app = web.Application(client_max_size=64 << 20)
        self.app.router.add_post("/v1/results", self.result)

    async def result(self, request):
        body = await request.read()
        sha = hashlib.sha256(body).hexdigest()
        self.posts.append(
            {
                "batch_id": request.query["batch_id"],
                "headers": dict(request.headers),
                "sha256": sha,
            }
        )
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if reply == "ok":
            archived = {"key": "browser-files/x.tar.gz", "sha256": sha}
            reply = (200, {"ok": True, "ingested": True, "status": "done", "archived": archived})
        status, document, *headers = reply
        return web.json_response(document, status=status, headers=(headers or [None])[0])


@pytest.fixture
async def agent_for(tmp_path):
    servers, sessions = [], []

    async def make(ingest):
        server = TestServer(ingest.app)
        await server.start_server()
        servers.append(server)
        agent = NodeAgent(
            str(server.make_url("")),
            "token-shifter",
            provider="shifter",
            product="residential",
            proxy_factory=lambda country: None,
            work_dir=tmp_path / "work",
            region="browser",
        )
        agent.session = aiohttp.ClientSession()
        sessions.append(agent.session)
        return agent

    yield make
    for session in sessions:
        await session.close()
    for server in servers:
        await server.close()


async def send_one(tmp_path, agent_for, replies, clock=None):
    ingest = Ingest(replies)
    agent = await agent_for(ingest)
    bundle = seal_dir(make_run(tmp_path), tmp_path / "outbox")
    outbox = Outbox(tmp_path / "outbox", **({"clock": clock} if clock else {}))
    return ingest, agent, bundle, outbox


async def test_a_confirmed_bundle_is_deleted_and_the_headers_are_k_bp7(tmp_path, agent_for):
    ingest, agent, bundle, outbox = await send_one(tmp_path, agent_for, ["ok"])
    local = hashlib.sha256(bundle.archive.read_bytes()).hexdigest()
    assert await outbox.send(bundle, agent) == "confirmed"
    assert not bundle.archive.exists() and not bundle.sidecar.exists()
    [post] = ingest.posts
    assert post["batch_id"] == "41" and post["sha256"] == local
    assert post["headers"][bc.HEADER_SHA256] == local
    assert post["headers"][bc.HEADER_BUNDLE_ID] == RUN_ID  # summary.run_id
    assert post["headers"]["Content-Type"] == bc.CONTENT_TYPE
    assert post["headers"]["Authorization"] == "Bearer token-shifter"
    assert outbox.hour_stats() == {
        "outbox_pending": 0,
        "outbox_bytes": 0,
        "confirmed_last_hour": 1,
        "rejected": 0,
        "conflicts": 0,
    }
    assert outbox.hour_stats()["confirmed_last_hour"] == 0


async def test_a_discarded_run_ingested_false_is_deleted(tmp_path, agent_for):
    _, agent, bundle, outbox = await send_one(tmp_path, agent_for, [(200, {"ingested": False})])
    assert await outbox.send(bundle, agent) == "confirmed"
    assert outbox.bundles() == []


@pytest.mark.parametrize(
    ("reply", "outcome", "marker"),
    [
        ((200, {"ingested": True, "archived": {"sha256": "0" * 64}}), "conflict", ".conflict"),
        ((200, {"ok": True, "status": "done"}), "conflict", ".conflict"),
        ((409, {"error": "bundle_changed"}), "conflict", ".conflict"),
        ((403, {"error": "another host"}), "rejected", ".rejected"),
        ((400, {"error": "the manifest is for another batch"}), "rejected", ".rejected"),
        ((429, {"error": "busy"}, {"Retry-After": "30"}), "retry", None),
        ((503, {"error": "archive unavailable"}), "retry", None),
        ((500, {"error": "boom"}), "retry", None),
    ],
)
async def test_every_other_reply_keeps_the_bundle(tmp_path, agent_for, reply, outcome, marker):
    ingest, agent, bundle, outbox = await send_one(tmp_path, agent_for, [reply])
    assert await outbox.send(bundle, agent) == outcome
    assert bundle.archive.exists() and bundle.sidecar.exists()
    if marker:
        note = json.loads(bundle.sidecar.with_suffix(marker).read_text())
        assert note["status"] == reply[0]
        assert outbox.pending() == [] and len(outbox.bundles()) == 1
    else:
        assert outbox.pending() == [bundle] and not outbox.due(bundle)
    stats = outbox.hour_stats()
    assert stats["outbox_bytes"] == bundle.bytes
    assert (stats["conflicts"], stats["rejected"]) == (
        int(marker == ".conflict"),
        int(marker == ".rejected"),
    )
    assert len(ingest.posts) == 1


async def test_a_reply_that_is_not_json_keeps_the_bundle(tmp_path, agent_for):
    class Text(Ingest):
        async def result(self, request):
            await request.read()
            status = self.replies.pop(0)
            return web.Response(status=status, text="<html>proxy error</html>")

    ingest = Text([502, 200])
    agent = await agent_for(ingest)
    bundle = seal_dir(make_run(tmp_path), tmp_path / "outbox")
    outbox = Outbox(tmp_path / "outbox")
    assert await outbox.send(bundle, agent) == "retry"
    outbox._retry.clear()
    assert await outbox.send(bundle, agent) == "conflict"
    assert bundle.archive.exists()


async def test_a_checksum_mismatch_is_rehashed_and_resent_once(tmp_path, agent_for):
    mismatch = (400, {"error": bc.ERROR_CHECKSUM_MISMATCH})
    ingest, agent, bundle, outbox = await send_one(tmp_path, agent_for, [mismatch])
    assert await outbox.send(bundle, agent) == "rejected"
    assert len(ingest.posts) == 2 and bundle.sidecar.with_suffix(".rejected").exists()
    ingest, agent, bundle, outbox = await send_one(tmp_path / "2", agent_for, [mismatch, "ok"])
    assert await outbox.send(bundle, agent) == "confirmed"
    assert len(ingest.posts) == 2 and not bundle.archive.exists()


async def test_503_three_times_then_200_sends_four_times_and_deletes_once(tmp_path, agent_for):
    now = [1000.0]
    busy = (503, {"error": "archive unavailable"})
    ingest, agent, bundle, outbox = await send_one(
        tmp_path, agent_for, [busy, busy, busy, "ok"], clock=lambda: now[0]
    )
    delays = []
    for _ in range(3):
        assert await outbox.send(bundle, agent) == "retry"
        assert not outbox.due(bundle)
        delays.append(outbox._retry[bundle.run_id][1] - now[0])
        now[0] += rawfiles.BACKOFF_MAX_S
        assert outbox.due(bundle)
    assert delays == [30.0, 60.0, 120.0]  # exponential backoff, at most 5 minutes
    deleted = []
    real = outbox.delete
    outbox.delete = lambda b: (deleted.append(b.run_id), real(b))
    assert await outbox.send(bundle, agent) == "confirmed"
    assert len(ingest.posts) == 4 and deleted == [RUN_ID] and outbox.bundles() == []


async def test_retry_after_is_honoured_and_a_network_error_keeps_the_bundle(tmp_path, agent_for):
    now = [0.0]
    busy = (429, {"error": "busy"}, {"Retry-After": "250"})
    ingest, agent, bundle, outbox = await send_one(
        tmp_path, agent_for, [busy], clock=lambda: now[0]
    )
    assert await outbox.send(bundle, agent) == "retry"
    assert outbox._retry[bundle.run_id][1] == 250.0
    agent.base = "http://127.0.0.1:9"  # nothing listens: connection refused
    assert await outbox.send(bundle, agent) == "retry"
    assert bundle.archive.exists() and outbox._retry[bundle.run_id][0] == 2


async def test_at_most_two_uploads_run_at_once(tmp_path, agent_for):
    gate, inside, peak = asyncio.Event(), [0], [0]

    class Slow(Ingest):
        async def result(self, request):
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
            await gate.wait()
            inside[0] -= 1
            return await super().result(request)

    ingest = Slow(["ok"])
    agent = await agent_for(ingest)
    outbox = Outbox(tmp_path / "outbox")
    bundles = []
    for n in range(4):
        run_id = f"0b6a7e4c-1d2e-4f3a-9b8c-7d6e5f4a3b2{n}"
        bundles.append(seal_dir(make_run(tmp_path / str(n), run_id), tmp_path / "outbox"))
    sends = asyncio.gather(*(outbox.send(bundle, agent) for bundle in bundles))
    await asyncio.sleep(0.2)
    gate.set()
    assert await sends == ["confirmed"] * 4 and peak[0] == rawfiles.INGEST_PARALLEL


def test_pending_skips_unreadable_sidecars_and_sidecars_without_their_tar_gz(tmp_path):
    outbox = tmp_path / "outbox"
    bundle = seal_dir(make_run(tmp_path), outbox)
    (outbox / "broken.json").write_text("{")
    other = bc.outbox_paths(outbox, "0b6a7e4c-1d2e-4f3a-9b8c-7d6e5f4a3b2f")[1]
    other.write_text(bundle.sidecar.read_text())
    assert Outbox(outbox).pending() == [bundle]
    assert Outbox(tmp_path / "none").pending() == []


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
    killed = make_run(tmp_path, ids[3])  # killed during its run: no summary.json
    (killed / "summary.json").unlink()
    confirmed = bc.outbox_paths(outbox, ids[4])[1]  # confirmed: its tar.gz already gone
    confirmed.write_text("{}")
    (outbox / f"{ids[0]}.json.partial").write_text("{")
    (runs / "shifter.residential" / "not-a-run").mkdir()

    def seal_run(run_dir, batch, summary, sha):
        return seal(run_dir, batch, summary, sha, CAMOUFOX, outbox=outbox)

    assert recover(runs, outbox, seal_run) == 1
    assert not finished.exists() and not half.exists() and not sealed.exists()
    assert killed.exists() and (killed / MEMBERS.tries).exists()
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

    assert recover(tmp_path / "runs", tmp_path / "outbox", seal_run) == 1
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

    assert recover(tmp_path / "runs", outbox, seal_run) == 1
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
    assert recover(tmp_path / "runs", outbox, lambda *a: None) == 0
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

    assert recover(tmp_path / "runs", outbox, seal_run) == 0
    [rebuilt] = Outbox(outbox).pending()
    assert not run_dir.exists() and rebuilt.sha256 == bundle.sha256
    assert hashlib.sha256(rebuilt.archive.read_bytes()).hexdigest() == bundle.sha256
