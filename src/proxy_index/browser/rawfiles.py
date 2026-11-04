"""A browser host's raw files (K-BP4) and their outbox (K-BP7; docs/browser.html, Bundles).

Every try of a run goes to `<run dir>/browser/tries.jsonl` and its screenshot, if any, to
`browser/screenshots/NNNN.jpg`, inside the node's run directory. When the run ends, seal()
writes `browser/manifest.json`, packs the directory deterministically into
`<outbox>/<run_id>.tar.gz` with the sidecar `<run_id>.json`, and only then removes the run
directory. The Outbox sends a bundle through its network's node agent and deletes it on the
manager's confirmation (contracts.confirmed) and on nothing else: 429, 503, other 5xx and
network errors keep it for a later pass with backoff, a 409 marks it `.conflict` and another
4xx `.rejected`. Nothing unconfirmed is ever deleted. Above `max_pending_gb` of runs and
outbox, no screenshot is written; the tries are still recorded.

Logs carry ids, statuses and error names, never a token, credential or proxy value.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

from ..archive.export import file_digests
from ..models import VERDICTS
from ..orchestrator.archive import pack
from . import contracts as bc

log = logging.getLogger(__name__)

MEMBERS = bc.bundle_members()
# The run's batch facts, for sealing after a restart; not a bundle member, so never packed.
BATCH_FILE = "browser/batch.json"
SIDECAR_FIELDS = ("batch_id", "sha256", "bytes", "network", "target", "country", "slot")
MARKERS = {"conflict": ".conflict", "rejected": ".rejected"}
INGEST_PARALLEL = 2
BACKOFF_S, BACKOFF_MAX_S = 30.0, 300.0
GUARD_EVERY_S, FULL_LOG_EVERY_S = 60.0, 3600.0
GB = 1 << 30


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


def _tree_bytes(path: Path) -> int:
    total = 0
    for folder, _, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(folder, name)).st_size
    return total


class DiskGuard:
    """Whether runs/ plus outbox/ are above the limit, measured at most once a minute; the
    first refused screenshot of each hour logs `browser_outbox_full`."""

    def __init__(self, roots: tuple[Path, ...], max_gb: float, notify=None, clock=time.monotonic):
        self.roots, self.limit = roots, int(max_gb * GB)
        self.notify, self.clock = notify, clock
        self.every = GUARD_EVERY_S
        self._measured, self._full, self._logged = -float("inf"), False, -float("inf")

    def full(self) -> bool:
        now = self.clock()
        if now - self._measured >= self.every:
            self._measured = now
            self._full = sum(_tree_bytes(root) for root in self.roots) > self.limit
        if self._full and now - self._logged >= FULL_LOG_EVERY_S:
            self._logged = now
            log.warning("browser_outbox_full: runs and outbox are above max_pending_gb")
            if self.notify is not None:
                self.notify({"kind": "browser_outbox_full", "limit_bytes": self.limit})
        return self._full


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


class FileRecorder:
    """The transport's recorder for one run; bind() names its run directory before any try."""

    def __init__(self, batch: Batch, settings_sha256: str, guard: DiskGuard | None = None):
        self.batch, self.settings_sha256, self.guard = batch, settings_sha256, guard
        self.directory: Path | None = None

    def bind(self, run_dir: Path) -> None:
        self.directory = run_dir / "browser"
        (self.directory / "screenshots").mkdir(parents=True, mode=0o700)
        facts = asdict(self.batch) | {"slot": self.batch.slot.isoformat()}
        facts["settings_sha256"] = self.settings_sha256
        _write(run_dir / BATCH_FILE, json.dumps(facts).encode(), "xb")
        _write(run_dir / MEMBERS.tries, b"", "xb")

    def try_done(self, record: dict[str, Any], screenshot: bytes | None) -> None:
        if self.directory is None:
            raise RuntimeError("the recorder has no run directory")
        run_dir = self.directory.parent
        if not self._shot(run_dir, record["screenshot"], screenshot):
            record = bc.try_record(**(record | {"screenshot": None}))
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with (run_dir / MEMBERS.tries).open("ab") as stream:
            stream.write(line.encode())

    def _shot(self, run_dir: Path, name: str | None, screenshot: bytes | None) -> bool:
        """Whether the try's screenshot was written: not above the disk limit, and a failed
        write (a full disk) loses only the screenshot. seal() fsyncs what it packs."""
        if screenshot is None or not name or (self.guard is not None and self.guard.full()):
            return False
        try:
            _write(run_dir / name, screenshot, "xb", sync=False)
        except OSError as exc:
            log.warning("screenshot %s not written (%s)", name, type(exc).__name__)
            (run_dir / name).unlink(missing_ok=True)
            return False
        return True


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


class Outbox:
    """The bundles of `root`, sent at most INGEST_PARALLEL at a time, oldest first."""

    def __init__(self, root: Path, clock: Callable[[], float] = time.monotonic) -> None:
        self.root, self.clock = Path(root), clock
        self.parallel = asyncio.Semaphore(INGEST_PARALLEL)
        self.confirmed = 0  # since the last hourly line
        self._retry: dict[str, tuple[int, float]] = {}  # run id: (failures, not before)

    def _sidecars(self) -> list[Path]:
        return list(self.root.glob("*.json")) if self.root.is_dir() else []

    def marker(self, bundle: Bundle) -> str | None:
        for name, suffix in MARKERS.items():
            if bundle.sidecar.with_suffix(suffix).exists():
                return name
        return None

    def bundles(self) -> list[Bundle]:
        """Every bundle whose tar.gz exists, oldest slot first, marked or not."""
        found = []
        for sidecar in self._sidecars():
            try:
                bundle = read_bundle(sidecar)
                key = (bundle.slot, sidecar.stat().st_mtime_ns, bundle.run_id)
            except (OSError, ValueError) as exc:
                log.warning("unreadable sidecar %s (%s)", sidecar.name, type(exc).__name__)
                continue
            if bundle.archive.exists():
                found.append((key, bundle))
        return [bundle for _, bundle in sorted(found, key=lambda entry: entry[0])]

    def pending(self) -> list[Bundle]:
        """The bundles still to send: unmarked, oldest first."""
        return [bundle for bundle in self.bundles() if self.marker(bundle) is None]

    def due(self, bundle: Bundle) -> bool:
        return self._retry.get(bundle.run_id, (0, 0.0))[1] <= self.clock()

    def hour_stats(self) -> dict[str, int]:
        """The hourly line's outbox fields; resets the confirmed count."""
        bundles = self.bundles()
        marks = [self.marker(bundle) for bundle in bundles]
        confirmed, self.confirmed = self.confirmed, 0
        return {
            "outbox_pending": marks.count(None),
            "outbox_bytes": sum(b.bytes for b in bundles),
            "confirmed_last_hour": confirmed,
            "rejected": marks.count("rejected"),
            "conflicts": marks.count("conflict"),
        }

    async def send(self, bundle: Bundle, agent: Any) -> str:
        """One upload through `agent` (its session, route and token): `confirmed`, `retry`,
        `conflict` or `rejected`. Never raises for an answer or a network error."""
        async with self.parallel:
            sha256 = bundle.sha256
            for attempt in range(2):
                try:
                    status, body, retry_after = await _post(bundle, agent, sha256)
                except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                    return self._later(bundle, f"network error ({type(exc).__name__})", 0)
                error = body.get("error") if isinstance(body, dict) else None
                if status == 400 and error == bc.ERROR_CHECKSUM_MISMATCH and not attempt:
                    sha256 = (await asyncio.to_thread(file_digests, bundle.archive))["sha256"]
                    continue
                break
        if bc.confirmed((status, body), sha256):
            await asyncio.to_thread(self.delete, bundle)
            self.confirmed += 1
            return "confirmed"
        if status in bc.RETRY_STATUSES or status >= 500:
            return self._later(bundle, f"answered {status}", retry_after)
        name = "conflict" if status in (200, 409) else "rejected"
        self._mark(bundle, name, status, error)
        return name

    def delete(self, bundle: Bundle) -> None:
        """A confirmed bundle goes: the tar.gz first, so a sidecar is never left pointing at
        nothing but a bundle already confirmed (recover() removes such a sidecar)."""
        bundle.archive.unlink(missing_ok=True)
        bundle.sidecar.unlink(missing_ok=True)
        self._retry.pop(bundle.run_id, None)
        log.info("bundle %s of batch %s confirmed and deleted", bundle.run_id, bundle.batch_id)

    def _later(self, bundle: Bundle, reason: str, retry_after: float) -> str:
        failures = self._retry.get(bundle.run_id, (0, 0.0))[0] + 1
        delay = max(min(BACKOFF_S * 2 ** (failures - 1), BACKOFF_MAX_S), retry_after)
        self._retry[bundle.run_id] = (failures, self.clock() + delay)
        log.warning("bundle %s kept: %s; retry in %.0fs", bundle.run_id, reason, delay)
        return "retry"

    def _mark(self, bundle: Bundle, name: str, status: int, error: Any) -> None:
        error = error if isinstance(error, str) and len(error) <= 64 else None
        note = {"status": status, "error": error, "at": datetime.now(UTC).isoformat()}
        _write(bundle.sidecar.with_suffix(MARKERS[name]), json.dumps(note).encode() + b"\n")
        self._retry.pop(bundle.run_id, None)
        log.warning("bundle %s kept as %s: answered %s %s", bundle.run_id, name, status, error)


async def _post(bundle: Bundle, agent: Any, sha256: str) -> tuple[int, Any, float]:
    """POST /v1/results as the node's own upload does, with the K-BP7 headers."""
    headers = agent._headers | {
        "Content-Type": bc.CONTENT_TYPE,
        bc.HEADER_SHA256: sha256,
        bc.HEADER_BUNDLE_ID: bundle.run_id,
    }
    stream = await asyncio.to_thread(bundle.archive.open, "rb")
    try:
        async with agent.session.post(
            agent.base + "/v1/results",
            params={"batch_id": str(bundle.batch_id)},
            data=stream,
            headers=headers,
        ) as response:
            text = await response.text()
            retry_after = response.headers.get("Retry-After", "")
            status = response.status
    finally:
        await asyncio.to_thread(stream.close)
    try:
        body = json.loads(text) if text else None
    except ValueError:
        body = None
    return status, body, float(retry_after) if retry_after.isdigit() else 0.0


SealRun = Callable[[Path, Batch, dict, str], Bundle]


def recover(runs: Path, outbox: Path, seal_run: SealRun) -> int:
    """At host start: seal every finished run directory a killed process left (a tar.gz
    without its sidecar is rebuilt), drop a run directory whose bundle is already sealed and
    verified, and drop half-written sidecars and those of bundles already confirmed. A
    directory without summary.json (killed during its run) is kept as it is and counted: the
    return value."""
    incomplete = 0
    if outbox.is_dir():
        for sidecar in [*outbox.glob("*.json.partial"), *outbox.glob("*.json")]:
            if sidecar.suffix == ".partial" or not sidecar.with_suffix(".tar.gz").exists():
                sidecar.unlink()
    for run_dir in sorted(p for p in runs.glob("*/*") if p.is_dir() and not p.is_symlink()):
        if not re.fullmatch(bc.RUN_ID, run_dir.name):
            continue
        archive, sidecar = bc.outbox_paths(outbox, run_dir.name)
        if sidecar.exists() and archive.exists():
            if file_digests(archive)["sha256"] == read_bundle(sidecar).sha256:
                shutil.rmtree(run_dir)
                continue
            sidecar.unlink()
        if not (run_dir / "summary.json").is_file() or not (run_dir / BATCH_FILE).is_file():
            incomplete += 1
            continue
        try:
            summary = json.loads((run_dir / "summary.json").read_bytes())
            batch, settings_sha256 = run_facts(run_dir)
            seal_run(run_dir, batch, summary, settings_sha256)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("run %s could not be sealed (%s)", run_dir.name, type(exc).__name__)
    if incomplete:
        log.warning("browser_run_incomplete: %s run directories kept unsealed", incomplete)
    return incomplete
