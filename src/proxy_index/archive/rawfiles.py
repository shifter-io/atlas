"""The raw files of every hourly database update: spooled 30 days locally, each hour in Spaces.

Owner decision of 2026-11-03: the files an update imported are kept 30 days on the server
that writes the database (so it can be rebuilt from them), uploaded to Spaces after the update
(kept long-term) and removed locally once older than that.

- `spool` files an imported run as `<root>/YYYY/MM/DD/HH/<name>.tar.gz`, by the UTC hour it was
  imported: the node's upload exactly as received (`<kind>-b<batch>-<run id>.tar.gz`, by the
  orchestrator API) or a converted daily-test run directory (`daily-<run id>.tar.gz`).
- `upload_hours` (the orchestrator's `raw_upload` job) packs each ended hour into one object
  `raw-files/YYYY/MM/DD/YYYY-MM-DDTHH.tar` (manifest.json with each file's size and sha256, then
  the files), uploads it private, verifies size, MD5 and sha256, and records it in the hour's
  `uploaded.json`. A file that arrives after its hour was uploaded goes into a further part
  (`…THH.1.tar`, ...). Then it removes every hour older than `keep_days` whose files are all
  recorded as uploaded; an hour with a file not uploaded is never removed.
- `rebuild` imports the spooled runs of a range of days, oldest first, into a database:
  replays of runs already there are no-ops, and a run whose batch is not in the target is
  imported without its batch link (reported). `rebuild_browser` does the same from Spaces for
  the browser bundles (K-BP4 objects under `browser.archive_prefix`, by slot day): each one's
  run directory, its `browser/` members left out.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import tarfile
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .. import storage
from . import ArchiveError
from .export import file_digests
from .spaces import Spaces

FILE = re.compile(
    r"(?P<kind>census|pulse|load|browser|daily)(?:-b(?P<batch>\d+))?-[0-9a-f-]{36}\.tar\.gz"
)
UPLOADED = "uploaded.json"
SLOT_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})T\d{4}Z\.tar\.gz")
SETTLE = timedelta(minutes=5)  # an hour is packed this long after it ended
KEEP_DAYS = 30
MAX_EXTRACTED = 64 << 30  # a rebuild trusts its own spool more than an upload


def hour_dir(root: Path, moment: datetime) -> Path:
    return Path(root, f"{moment.astimezone(UTC):%Y/%m/%d/%H}")


def spool(root: str | Path, moment: datetime, name: str, source: Path, *, move: bool) -> Path:
    """File `source` (a .tar.gz, or a run directory packed into one) under the hour of moment."""
    if not FILE.fullmatch(name):
        raise ArchiveError(f"not a raw file name: {name!r}")
    folder = hour_dir(Path(root), moment)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target, partial = folder / name, folder / f".{name}.partial"
    if target.exists():
        return target  # a replay: the first copy stays
    if Path(source).is_dir():
        from ..orchestrator.archive import pack

        pack(source, partial)
    elif move:
        shutil.move(source, partial)
    else:
        shutil.copyfile(source, partial)
    partial.chmod(0o600)
    partial.replace(target)
    return target


def try_spool(root, moment: datetime, name: str, source: Path, *, move: bool) -> str | None:
    """spool, for callers whose import has already committed: a failure must not change the
    import's outcome, so it is returned (the exception type) for an alert instead of raised."""
    try:
        spool(root, moment, name, source, move=move)
    except (OSError, ArchiveError) as exc:
        return type(exc).__name__
    return None


def _hours(root: Path) -> list[tuple[datetime, Path]]:
    found = []
    for folder in Path(root).glob("[0-9]" * 4 + "/[0-9][0-9]/[0-9][0-9]/[0-9][0-9]"):
        y, m, d, h = (int(p) for p in folder.relative_to(root).parts)
        found.append((datetime(y, m, d, h, tzinfo=UTC), folder))
    return sorted(found)


def _files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and FILE.fullmatch(p.name))


def _uploaded(folder: Path) -> dict:
    path = folder / UPLOADED
    return json.loads(path.read_text()) if path.exists() else {"parts": []}


def _pack(hour: datetime, files: list[Path], out: Path) -> Path:
    manifest = {"hour": hour.isoformat(), "files": []}
    for path in files:
        digests = file_digests(path)
        manifest["files"].append(
            {"name": path.name, **{k: digests[k] for k in ("bytes", "sha256")}}
        )
    data = json.dumps(manifest, indent=1, sort_keys=True).encode()
    with tarfile.open(out, "w", format=tarfile.GNU_FORMAT) as archive:
        info = tarfile.TarInfo("manifest.json")
        info.size, info.mtime, info.mode = len(data), int(hour.timestamp()), 0o644
        archive.addfile(info, io.BytesIO(data))
        for path in files:
            info = archive.gettarinfo(path, arcname=path.name)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as stream:
                archive.addfile(info, stream)
    return out


def _upload_part(spaces: Spaces, hour: datetime, folder: Path, files: list[Path], part: int):
    key = f"raw-files/{hour:%Y/%m/%d}/{hour:%Y-%m-%dT%H}" + (f".{part}" if part else "") + ".tar"
    with tempfile.TemporaryDirectory(prefix="proxy-index-raw-") as temporary:
        path = _pack(hour, files, Path(temporary, "hour.tar"))
        digests = file_digests(path)
        spaces.put_verified(key, path, digests, what="the hour's files")
    return {"key": key, "sha256": digests["sha256"], "files": [p.name for p in files]}


def upload_hours(root, spaces: Spaces, now: datetime, keep_days: int = KEEP_DAYS) -> dict:
    """Upload every ended hour's files not uploaded yet, then remove the expired hours."""
    uploaded, removed = [], []
    for hour, folder in _hours(Path(root)):
        state = _uploaded(folder)
        done = {name for part in state["parts"] for name in part["files"]}
        new = [p for p in _files(folder) if p.name not in done]
        if new and hour + timedelta(hours=1) + SETTLE <= now:
            state["parts"].append(_upload_part(spaces, hour, folder, new, len(state["parts"])))
            partial = folder / f".{UPLOADED}.partial"
            partial.write_text(json.dumps(state, indent=1))
            partial.replace(folder / UPLOADED)
            uploaded.append(state["parts"][-1]["key"])
            new = []
        if not new and hour + timedelta(days=keep_days, hours=1) <= now:
            shutil.rmtree(folder)
            removed.append(f"{hour:%Y-%m-%dT%H}")
            for parent in folder.parents[:3]:
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
    return {"uploaded": uploaded, "removed_hours": removed}


def _batch(database, batch: str | None) -> int | None:
    """The run's batch id when the target database has that batch, else None."""
    if batch is None:
        return None
    with storage.connect(database) as conn:
        known = conn.execute("SELECT 1 FROM test_batches WHERE batch_id=%s", (int(batch),))
        return int(batch) if known.fetchone() else None


def _import(path: Path, database, batch: int | None, kind: str) -> bool:
    from ..orchestrator.archive import unpack

    with tempfile.TemporaryDirectory(prefix="proxy-index-rebuild-") as temporary:
        unpack(path, temporary, max_bytes=MAX_EXTRACTED)
        done = storage.ingest(Path(temporary), database, batch_id=batch, ip_rows=kind != "pulse")
    return bool(done["imported"])


def rebuild(root, database, first: date, last: date) -> dict:
    """Import the spooled runs of the days first..last (by import hour), oldest first."""
    report = {"runs": 0, "imported": 0, "unlinked": []}
    for hour, folder in _hours(Path(root)):
        if not first <= hour.date() <= last:
            continue
        for path in _files(folder):
            match = FILE.fullmatch(path.name)
            batch = _batch(database, match["batch"])
            if match["batch"] and batch is None:
                report["unlinked"].append(path.name)
            report["runs"] += 1
            report["imported"] += _import(path, database, batch, match["kind"])
    return report


def rebuild_browser(spaces: Spaces, database, prefix: str, first: date, last: date) -> dict:
    """Import the archived browser bundles whose slot falls on the days first..last (CALC-INC
    Q5: a rebuild downloads from Spaces), like `rebuild`: replays are no-ops."""
    from ..orchestrator.archive import ArchiveError as UploadError
    from ..orchestrator.browser_ingest import unpack_bundle

    report = {"runs": 0, "imported": 0, "unlinked": []}
    for key in spaces.list_keys(f"{prefix}/"):
        slot = SLOT_NAME.fullmatch(key.rsplit("/", 1)[-1])
        if slot is None or not first <= date.fromisoformat(slot[1]) <= last:
            continue
        with tempfile.TemporaryDirectory(prefix="proxy-index-rebuild-") as temporary:
            bundle, directory = Path(temporary, "bundle.tar.gz"), Path(temporary, "run")
            spaces.get_file(key, bundle)
            directory.mkdir()
            try:
                unpack_bundle(bundle, directory, MAX_EXTRACTED)
                manifest = json.loads((directory / "browser" / "manifest.json").read_text())
            except (UploadError, OSError, ValueError) as exc:
                raise ArchiveError(f"{key} is not a readable browser bundle: {exc}") from None
            shutil.rmtree(directory / "browser")
            batch = _batch(database, str(manifest.get("batch_id")))
            if batch is None:
                report["unlinked"].append(key)
            done = storage.ingest(directory, database, batch_id=batch, ip_rows=False)
        report["runs"] += 1
        report["imported"] += bool(done["imported"])
    return report
