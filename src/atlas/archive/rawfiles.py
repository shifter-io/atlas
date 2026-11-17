"""The raw files of every hourly database update: spooled 30 days locally, each hour in Spaces.

Owner decisions of 2026-11-03 and 2026-11-14 (ARCHIVE-RAW-1): the raw files an update used are
kept 30 days on the server that writes the database (so it can be rebuilt from them), uploaded
to Spaces per network and hour (kept long-term) and removed locally once older than that.
Raw means before IP enrichment.

- `spool` files a run as `<root>/YYYY/MM/DD/HH/<network>/<name>.tar.gz`, by the UTC hour it
  was spooled: the node's upload exactly as received (`<kind>-b<batch>-<run id>.tar.gz`, by the
  orchestrator API, network = the node's provider) or a converted daily-test run directory
  before its DB-IP enrichment (`daily-<run id>.tar.gz`, by `daily ingest`; enrichment files a
  rerun finds are left out).
- rawhours.upload_hours (the orchestrator's `raw_upload` job) uploads and expires them.
- `rebuild` imports the spooled runs of a range of days, oldest first, into a database (with
  `mmdb`, daily runs are enriched first and a pulse run's droplet enrichment is replaced, as the
  manager does at upload; a pulse run's IPs go to the lifetime counters only, PULSE-COUNTERS-1):
  replays of runs already there are no-ops, and a run whose batch is not in the target is
  imported without its batch link (reported).
  `rebuild_browser` does the same from Spaces for the browser bundles (K-BP4 objects under
  `browser.archive_prefix`, by slot day): each one's run directory, its `browser/` members
  left out.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

from .. import storage
from ..enrichment import ENRICHED_NAME as ENRICHED
from ..enrichment import enrich_run, strip
from . import ArchiveError
from .spaces import Spaces

FILE = re.compile(
    r"(?P<kind>census|pulse|load|browser|daily)(?:-b(?P<batch>\d+))?-[0-9a-f-]{36}\.tar\.gz"
)
NETWORK = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
SLOT_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})T\d{4}Z\.tar\.gz")
KEEP_DAYS = 30
MAX_EXTRACTED = 64 << 30  # a rebuild trusts its own spool more than an upload


def hour_dir(root: Path, moment: datetime) -> Path:
    return Path(root, f"{moment.astimezone(UTC):%Y/%m/%d/%H}")


def spool(root, moment: datetime, network: str, name: str, source: Path, *, move: bool) -> Path:
    """File `source` (a .tar.gz, or a run directory packed into one without its enrichment
    files) under the network's folder of the hour of moment."""
    if not FILE.fullmatch(name):
        raise ArchiveError(f"not a raw file name: {name!r}")
    if not NETWORK.fullmatch(network):
        raise ArchiveError(f"not a network name: {network!r}")
    folder = hour_dir(Path(root), moment) / network
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target, partial = folder / name, folder / f".{name}.partial"
    if target.exists():
        return target  # a replay: the first copy stays
    if Path(source).is_dir():
        from ..orchestrator.archive import pack

        pack(source, partial, skip=ENRICHED.fullmatch)
    elif move:
        shutil.move(source, partial)
    else:
        shutil.copyfile(source, partial)
    partial.chmod(0o600)
    partial.replace(target)
    return target


def try_spool(root, moment: datetime, network: str, name: str, source: Path, *, move: bool):
    """spool, for callers whose run must go on whatever happens to its raw file: a failure is
    returned (the exception type, else None) for an alert instead of raised."""
    try:
        spool(root, moment, network, name, source, move=move)
    except (OSError, ArchiveError) as exc:
        return type(exc).__name__
    return None


def hours(root: Path) -> list[tuple[datetime, Path]]:
    found = []
    for folder in Path(root).glob("[0-9]" * 4 + "/[0-9][0-9]/[0-9][0-9]/[0-9][0-9]"):
        y, m, d, h = (int(p) for p in folder.relative_to(root).parts)
        found.append((datetime(y, m, d, h, tzinfo=UTC), folder))
    return sorted(found)


def files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and FILE.fullmatch(p.name))


def networks(folder: Path) -> list[tuple[str, Path]]:
    """The hour folder's network folders, by name."""
    found = (p for p in folder.iterdir() if p.is_dir() and NETWORK.fullmatch(p.name))
    return sorted((p.name, p) for p in found)


def _runs(folder: Path) -> list[Path]:
    """The hour's spooled runs: its networks' and any filed directly in it (the layout before
    ARCHIVE-RAW-1)."""
    return files(folder) + [path for _, own in networks(folder) for path in files(own)]


def _batch(database, batch: str | None) -> int | None:
    """The run's batch id when the target database has that batch, else None."""
    if batch is None:
        return None
    with storage.connect(database) as conn:
        known = conn.execute("SELECT 1 FROM test_batches WHERE batch_id=%s", (int(batch),))
        return int(batch) if known.fetchone() else None


def _enrich(directory: str, mmdb, kind: str) -> bool:
    """Enrich a daily run as daily ingest does, a pulse run as the manager does at upload (in
    place of its droplet's); False when enrichment refuses it."""
    try:
        if kind == "pulse":
            strip(directory)
        enrich_run(directory, mmdb)
    except FileExistsError:
        pass  # spooled enriched (the layout before ARCHIVE-RAW-1)
    except ValueError:
        return False
    return True


def _stored(database, directory: str) -> bool:
    """Whether the run is already in the database: a replay, which is not enriched again (with
    another file, a fresh enrichment would add a second release to the stored run)."""
    run_id = json.loads(Path(directory, "summary.json").read_text())["run_id"]
    with storage.connect(database) as conn:
        found = conn.execute("SELECT 1 FROM test_runs WHERE run_id=%s::uuid", (run_id,))
        return found.fetchone() is not None


def _import(path: Path, database, batch: int | None, kind: str, mmdb=None) -> bool | None:
    """Import one spooled run: whether it was new, or None when its enrichment was refused
    (daily ingest does not import such a run either)."""
    from ..orchestrator.archive import unpack

    with tempfile.TemporaryDirectory(prefix="atlas-rebuild-") as temporary:
        unpack(path, temporary, max_bytes=MAX_EXTRACTED)
        enrich = kind in ("daily", "pulse") and mmdb is not None
        if enrich and not _stored(database, temporary) and not _enrich(temporary, mmdb, kind):
            return None
        pulse = kind == "pulse"
        done = storage.ingest(Path(temporary), database, batch_id=batch, counters=pulse)
    return bool(done["imported"])


def rebuild(root, database, first: date, last: date, mmdb=None) -> dict:
    """Import the spooled runs of the days first..last (by spool hour), oldest first; a daily
    run that enrichment refuses is skipped and listed (`not_enriched`)."""
    report = {"runs": 0, "imported": 0, "unlinked": [], "not_enriched": []}
    for hour, folder in hours(Path(root)):
        if not first <= hour.date() <= last:
            continue
        for path in _runs(folder):
            match = FILE.fullmatch(path.name)
            batch = _batch(database, match["batch"])
            if match["batch"] and batch is None:
                report["unlinked"].append(path.name)
            report["runs"] += 1
            imported = _import(path, database, batch, match["kind"], mmdb)
            if imported is None:
                report["not_enriched"].append(path.name)
            report["imported"] += bool(imported)
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
        with tempfile.TemporaryDirectory(prefix="atlas-rebuild-") as temporary:
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
