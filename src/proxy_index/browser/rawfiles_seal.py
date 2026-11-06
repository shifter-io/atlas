"""A browser run's bundle on disk: its facts, its seal and its recovery (K-BP4; docs/browser.html,
Bundles).

FileRecorder.bind() keeps the run's batch facts in `browser/batch.json` (run_facts() reads them
back). seal() writes a finished run's manifest, packs its directory into the outbox with its
sidecar (read_bundle() reads one) and only then removes the directory. recover() seals at host
start what a killed process left finished and deletes its half runs, under host_lock().
browser.rawfiles holds the recorder and the outbox and imports these names, so existing
imports keep working.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from ..archive.export import file_digests
from ..errors import UserError
from ..models import VERDICTS
from ..orchestrator.archive import pack
from . import contracts as bc

log = logging.getLogger(__name__)

MEMBERS = bc.bundle_members()
# The run's batch facts, for sealing after a restart; not a bundle member, so never packed.
BATCH_FILE = "browser/batch.json"
SIDECAR_FIELDS = ("batch_id", "sha256", "bytes", "network", "target", "country", "slot")
LOCK_NAME = "host.lock"


def _write(path: Path, data: bytes, mode: str = "wb", sync: bool = True) -> None:
    """Write an owner-only file, fsynced unless `sync` is false."""
    with path.open(mode) as stream:
        os.chmod(path, 0o600)
        stream.write(data)
        if sync:
            stream.flush()
            os.fsync(stream.fileno())


def _sync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class Batch:
    """The facts of a run's batch that summary.json does not hold; slot is its UTC start."""

    batch_id: int
    country: str
    slot: datetime
    host: str


def run_facts(run_dir: Path) -> tuple[Batch, str]:
    """The batch and settings sha256 FileRecorder.bind() kept in the run directory."""
    facts = json.loads((run_dir / BATCH_FILE).read_bytes())
    settings_sha256 = facts.pop("settings_sha256")
    return Batch(**(facts | {"slot": datetime.fromisoformat(facts["slot"])})), settings_sha256


@dataclass(frozen=True, slots=True)
class Bundle:
    """One sealed bundle in the outbox, as its sidecar describes it."""

    run_id: str
    archive: Path
    sidecar: Path
    batch_id: int
    sha256: str
    bytes: int
    network: str
    target: str
    country: str
    slot: str


def _outcomes(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    verdicts = dict.fromkeys(VERDICTS, 0)
    errors = dict.fromkeys(bc.CODE_VERDICTS, 0)
    for record in records:
        verdicts[record["verdict"]] += 1
        if record["error"]:
            errors[record["error"]] += 1
    return {"verdicts": verdicts, "errors": errors}


def _members(run_dir: Path) -> list[dict[str, Any]]:
    """Each bundle member below browser/ but the manifest, with its bytes and sha256."""
    names = sorted(
        path.relative_to(run_dir).as_posix() for path in (run_dir / "browser").rglob("*")
    )
    found = []
    for name in names:
        if MEMBERS.is_member(name) and name != MEMBERS.manifest:
            digests = file_digests(run_dir / name)
            found.append({"name": name, "bytes": digests["bytes"], "sha256": digests["sha256"]})
    return found


def build_manifest(
    run_dir: Path, batch: Batch, summary: dict, settings_sha256: str, camoufox: dict
) -> dict[str, Any]:
    """The run's K-BP4 manifest; requested_country and geo_targeting from its config (D11)."""
    lines = (run_dir / MEMBERS.tries).read_bytes().splitlines()
    records = [bc.try_record(**json.loads(line)) for line in lines]
    config = summary["config"]
    requested = config.get("requested_country")
    requested = requested.upper() if isinstance(requested, str) else None
    return bc.manifest(
        run_id=summary["run_id"],
        batch_id=batch.batch_id,
        network={"provider": config["provider"], "product": config["product"]},
        target=summary["target"]["id"],
        country=batch.country,
        requested_country=requested,
        geo_targeting=requested is not None,
        slot=batch.slot,
        started_at=summary["started_at"],
        finished_at=summary["finished_at"],
        tries=len(records),
        outcomes=_outcomes(records),
        settings_sha256=settings_sha256,
        camoufox={key: camoufox[key] for key in bc.CAMOUFOX_FIELDS},
        host=batch.host,
        files=_members(run_dir),
    )


def seal(
    run_dir: Path,
    batch: Batch,
    summary: dict,
    settings_sha256: str,
    camoufox: dict,
    *,
    outbox: Path,
) -> Bundle:
    """Manifest, deterministic tar.gz and sidecar, each fsynced; the tar.gz re-hashed against
    the sidecar; then, and only then, the run directory is removed."""
    run_dir = Path(run_dir)
    document = build_manifest(run_dir, batch, summary, settings_sha256, camoufox)
    _write(run_dir / MEMBERS.manifest, json.dumps(document, indent=2).encode() + b"\n")
    outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive, sidecar = bc.outbox_paths(outbox, summary["run_id"])
    pack(run_dir, archive, nested=MEMBERS.is_member, mtime=int(batch.slot.timestamp()))
    with archive.open("rb") as stream:
        os.fsync(stream.fileno())
    archive.chmod(0o600)
    digests = file_digests(archive)
    facts = {
        "batch_id": batch.batch_id,
        "sha256": digests["sha256"],
        "bytes": digests["bytes"],
        "network": f"{document['network']['provider']}/{document['network']['product']}",
        "target": document["target"],
        "country": batch.country,
        "slot": document["slot"],
    }
    partial = sidecar.with_name(sidecar.name + ".partial")
    _write(partial, json.dumps(facts, indent=2).encode() + b"\n")
    partial.replace(sidecar)
    _sync_dir(outbox)
    bundle = read_bundle(sidecar)
    if file_digests(archive)["sha256"] != bundle.sha256:
        raise OSError("the sealed bundle changed on disk")
    shutil.rmtree(run_dir)
    return bundle


def read_bundle(sidecar: Path) -> Bundle:
    facts = bc.check_fields(json.loads(sidecar.read_bytes()), SIDECAR_FIELDS, "sidecar")
    run_id = sidecar.name.removesuffix(".json")
    archive, _ = bc.outbox_paths(sidecar.parent, run_id)
    return Bundle(run_id, archive, sidecar, **facts)


SealRun = Callable[[Path, Batch, dict, str], Bundle]


class Recovered(NamedTuple):
    """What recover() did not send on: run directories deleted as half runs, finished ones
    kept unsealed, and half runs it could not delete (retried at the next start)."""

    discarded: int
    unsealed: int
    discard_failed: int


@contextlib.contextmanager
def host_lock(state: Path) -> Iterator[None]:
    """Hold <state>/host.lock (created 0600) with an exclusive flock while the block runs.

    A second browser-node on the same state directory would delete the first one's runs in
    progress at its start (recover()): it fails at once with a UserError instead, deleting
    nothing. The descriptor is not inherited by child processes, so the lock ends with this
    process.
    """
    path = Path(state) / LOCK_NAME
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise UserError(f"cannot open the host lock {path} ({type(exc).__name__})") from None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UserError(
                f"another browser-node holds {path}: one process per state directory"
            ) from None
        yield
    finally:
        os.close(descriptor)


def recover(runs: Path, outbox: Path, seal_run: SealRun) -> Recovered:
    """At host start: seal every finished run directory a killed process left (a tar.gz
    without its sidecar is rebuilt), drop a run directory whose bundle is already sealed and
    verified, and drop half-written sidecars and those of bundles already confirmed. A run
    directory without summary.json (killed during its run) is a half run: it is deleted, never
    sealed or uploaded, and logged once; one it cannot delete is counted `discard_failed` and
    tried again at the next start. A finished run it cannot read or seal is kept as it is, with
    its sidecar. One bad file never stops the start. Logs one line with the counts."""
    if outbox.is_dir():
        _drop_sidecars(outbox)
    found = {"sealed": 0, "discarded": 0, "kept": 0, "discard_failed": 0}
    for run_dir in sorted(p for p in runs.glob("*/*") if p.is_dir() and not p.is_symlink()):
        if not re.fullmatch(bc.RUN_ID, run_dir.name):
            continue
        try:
            outcome = _recover_run(run_dir, outbox, seal_run)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("run %s kept unsealed (%s)", run_dir.name, type(exc).__name__)
            outcome = "kept"
        found[outcome] += 1
    log.info("browser_recover: %s", " ".join(f"{key}={value}" for key, value in found.items()))
    return Recovered(found["discarded"], found["kept"], found["discard_failed"])


def _drop_sidecars(outbox: Path) -> None:
    """Drop the half-written sidecars and those whose bundle is gone (already confirmed)."""
    for sidecar in [*outbox.glob("*.json.partial"), *outbox.glob("*.json")]:
        try:
            if sidecar.suffix == ".partial" or not sidecar.with_suffix(".tar.gz").exists():
                sidecar.unlink()
        except OSError as exc:
            log.warning("sidecar %s kept (%s)", sidecar.name, type(exc).__name__)


def _recover_run(run_dir: Path, outbox: Path, seal_run: SealRun) -> str:
    """`sealed` when the run is in the outbox now (sealed before, its directory goes, or
    here), `discarded` when it was a half run, now deleted, `discard_failed` when that delete
    failed, `kept` otherwise."""
    archive, sidecar = bc.outbox_paths(outbox, run_dir.name)
    if sidecar.exists() and archive.exists():
        if file_digests(archive)["sha256"] == read_bundle(sidecar).sha256:
            shutil.rmtree(run_dir)
            return "sealed"
        sidecar.unlink()
    if not os.path.lexists(run_dir / "summary.json"):
        try:
            shutil.rmtree(run_dir)
        except OSError as exc:
            log.warning("browser_run_discard_failed: run %s (%s)", run_dir.name, type(exc).__name__)
            return "discard_failed"
        log.warning("browser_run_discarded: run %s killed during its run, deleted", run_dir.name)
        return "discarded"
    if not (run_dir / BATCH_FILE).is_file():
        log.warning("run %s kept unsealed (no %s)", run_dir.name, BATCH_FILE)
        return "kept"
    summary = json.loads((run_dir / "summary.json").read_bytes())
    batch, settings_sha256 = run_facts(run_dir)
    seal_run(run_dir, batch, summary, settings_sha256)
    return "sealed"
