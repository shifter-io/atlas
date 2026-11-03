"""The daily archive: every due day goes into its file, into Spaces (verified), then out of the
database.

A day is archived only when it is due (purge.pending) and its object `raw/YYYY/YYYY-MM-DD.tar.gz`
is in the bucket with the local file's size, MD5 and sha256. The object also carries the
sha256 of the day's fact entries (`x-amz-meta-facts`): a rerun after a crash between the upload
and the deletion finds the object already there and deletes the rows when the facts are the
same (dimension rows, such as a product added since, may differ); different facts leave the
day alone and fail the run (`archive_changed`). Objects are never overwritten or deleted.
Days are taken oldest first, at most `max_days` per run, stopping at the first that is not due.
Only days that still hold rows are archived: a day without any batch or run gets no file.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ..storage import connect
from . import ArchiveError
from .export import export_day, file_digests
from .purge import pending, purge_day
from .spaces import Spaces
from .tables import select_day

MAX_DAYS = 3


def object_key(day: date) -> str:
    return f"raw/{day:%Y}/{day.isoformat()}.tar.gz"


def facts_digest(manifest: dict) -> str:
    entries = [
        [t["name"], t["rows"], t["sha256"]] for t in manifest["tables"] if t["role"] == "fact"
    ]
    return hashlib.sha256(json.dumps([manifest["day"], entries]).encode()).hexdigest()


def oldest_day(database) -> date | None:
    """The UTC day of the oldest batch, or of the oldest run without a batch."""
    with connect(database) as conn:
        first = conn.execute(
            "SELECT least((SELECT min(scheduled_for) FROM test_batches),"
            "(SELECT min(started_at) FROM test_runs WHERE batch_id IS NULL))"
        ).fetchone()[0]
    return first.astimezone(UTC).date() if first else None


def waiting(database, day: date, now: datetime) -> str | None:
    """Why the day is not due yet (read-only), or None."""
    with connect(database) as conn:
        select_day(conn, day)
        return pending(conn, day, now)


def store(spaces: Spaces, path: Path, manifest: dict) -> dict[str, Any]:
    """Upload the day file unless its facts are already stored; return the verified object."""
    key = object_key(date.fromisoformat(manifest["day"]))
    digests, facts = file_digests(path), facts_digest(manifest)
    found = spaces.head(key)
    if found is None:
        spaces.put_file(key, path, digests, {"facts": facts})
        found = spaces.head(key)
        expected = {k: digests[k] for k in ("bytes", "md5", "sha256")} | {"facts": facts}
    else:
        expected = {"facts": facts}
    if found is None or any(found.get(k) != v for k, v in expected.items()):
        raise ArchiveError(f"archive_changed: {key} in the bucket differs from the day's rows")
    return {"key": key, **found}


def archive_day(database, spaces: Spaces, day: date, workdir: Path, now: datetime) -> dict:
    path, manifest = export_day(database, day, workdir)
    try:
        stored = store(spaces, path, manifest)
        purged = purge_day(database, day, manifest, now)
    finally:
        path.unlink(missing_ok=True)
    return {"day": day.isoformat(), "object": stored, **purged}


def run_due(database, spaces: Spaces, workdir: Path, now: datetime, max_days: int = MAX_DAYS):
    """Archive the due days, oldest first; a summary for the job log."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    archived, reason = [], None
    while len(archived) < max_days:
        day = oldest_day(database)
        if day is None:
            reason = "no raw rows"
            break
        if archived and day.isoformat() <= archived[-1]["day"]:
            raise ArchiveError(f"{day} still holds rows after it was archived")
        reason = waiting(database, day, now)
        if reason:
            reason = f"{day}: {reason}"
            break
        archived.append(archive_day(database, spaces, day, workdir, now))
    summary = {"archived": [a["day"] for a in archived], "waiting": reason}
    summary["deleted_rows"] = sum(sum(a["deleted"].values()) for a in archived)
    return summary
