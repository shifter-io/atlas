"""Shared runs and bundles for the browser bundle tests on the host (browser/rawfiles.py): a
finished run directory as the node and the recorder leave it, and its seal into the outbox."""

from __future__ import annotations

import json
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from browser_contracts_cases import try_fields

from proxy_index.browser import contracts as bc
from proxy_index.browser import rawfiles
from proxy_index.browser.rawfiles import Batch, FileRecorder, seal

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
