"""VERIFY-HOUR-1 (2/2): the hour's read-only cloud checks, after daily.verify's database ones.

- droplets: the daily test's workers (WORKER_TAG) and the pulse nodes (PULSE_TAG) up now, listed
  by tag (DigitalOcean.list_tag, a GET); one created more than ORPHAN ago (an hour plus 30
  minutes) outlived its hour: an orphan. Nothing is ever deleted.
- archive: with archive.enabled and raw.spool_dir set (the raw_upload job's switches), every
  network that stored a non-browser run in the hour needs its raw files in Spaces under the
  hour or the next one (archive.rawhours: a run is spooled when it is imported, which can be in
  the next hour). A network without them is not due yet until ARCHIVE_DUE after the hour's end,
  when the next hour has been uploaded too. archive.spaces_file resolves under MANAGER_ROOT, the
  orchestrator's working directory, as the DigitalOcean token does.

A cloud that cannot be read fails its check with the error's type, never the report.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from ..archive.rawhours import PART, listing
from ..archive.spaces import Spaces
from ..campaign.collector import MANAGER_ROOT
from ..campaign.contracts import PULSE_TAG, WORKER_TAG, with_legacy
from ..legacy_names import current
from ..orchestrator.control import read_setting
from ..storage import connect
from .verify import HOUR, SETTLE, Check, Hour, Report, listed, total

ORPHAN = HOUR + SETTLE
ARCHIVE_DUE = HOUR + timedelta(minutes=15)  # the next hour's end, its settle and one upload run
TAGS = (WORKER_TAG, PULSE_TAG)
NETWORKS_SQL = (
    "SELECT DISTINCT p.provider FROM test_runs t JOIN scope_stats s USING(scope_id) "
    "JOIN products p USING(product_id) LEFT JOIN test_batches b USING(batch_id) "
    "WHERE t.started_at>=%s AND t.started_at<%s AND b.kind IS DISTINCT FROM 'browser' ORDER BY 1"
)


async def _list_tags(cloud) -> list:
    try:
        # A droplet tagged before the rename (legacy_names) counts under its current tag.
        return [droplet for tag in with_legacy(list(TAGS)) for droplet in await cloud.list_tag(tag)]
    finally:
        await cloud.close()


def droplets(cloud, hour: Hour) -> list[Check]:
    """Each tag's droplets up now and its orphans, then the total."""
    try:
        found = asyncio.run(_list_tags(cloud))
    except Exception as error:  # boundary: an unreadable cloud fails the check, not the report
        return total("droplets", [], f"not listed ({type(error).__name__})", False)
    checks = []
    for tag in TAGS:
        mine = [d for d in found if tag in map(current, d.tags)]
        orphans = sorted(d.name for d in mine if hour.now - d.created_at > ORPHAN)
        detail = f"{len(mine)} up, {len(orphans)} orphans" + listed(orphans)
        checks.append(Check("droplets", tag, not orphans, detail))
    return total("droplets", checks, f"{len(found)} up now (read only)")


def _archived(spaces: Spaces, hour: Hour) -> set[str]:
    keys = listing(spaces, hour.start) + listing(spaces, hour.end)
    return {PART.fullmatch(key.rsplit("/", 1)[1])["network"] for key in keys}


def archive(database_file: Path, hour: Hour, spaces=Spaces.from_file) -> list[Check]:
    """The hour's raw files in Spaces per network that stored runs; off unless uploaded."""
    with connect(database_file) as conn:
        conn.read_only = True
        if not (read_setting(conn, "archive.enabled") and read_setting(conn, "raw.spool_dir")):
            return total("archive", [], "off (archive.enabled or raw.spool_dir is off)", None)
        spaces_file = read_setting(conn, "archive.spaces_file")
        networks = [row[0] for row in conn.execute(NETWORKS_SQL, (hour.start, hour.end))]
    try:
        found = _archived(spaces(MANAGER_ROOT / spaces_file), hour)
    except Exception as error:  # boundary: an unreadable bucket fails the check, not the report
        return total("archive", [], f"not listed ({type(error).__name__})", False)
    due = hour.now >= hour.end + ARCHIVE_DUE
    checks = [
        Check("archive", n, True, "raw files in Spaces")
        if n in found
        else Check("archive", n, False if due else None, "no raw files in Spaces")
        for n in networks
    ]
    stored = sum(n in found for n in networks)
    return total("archive", checks, f"{stored} of {len(networks)} networks' raw files in Spaces")


def with_cloud(found: Report, database_file: Path, hour: Hour, token_file: Path) -> Report:
    """The report with the droplet and archive checks after the database ones."""
    from ..campaign.do_api import DigitalOcean, file_token

    cloud = DigitalOcean(file_token(token_file))
    extra = droplets(cloud, hour) + archive(database_file, hour)
    return Report(found.hour, found.checks + tuple(extra))
