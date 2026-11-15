"""PULSE-ENRICH-1: the manager enriches each pulse upload with its own DB-IP release.

A pulse droplet's node enriches its runs with the release in its image. The manager replaces
that with the file `$DBIP_MMDB` names, the one the daily `dbip update` keeps current and daily
runs are enriched with, so a pulse run records the same release (`dbip_releases`) and its exit
IPs are labelled like a daily run's in the lifetime counters it updates (storage.ingest's
counters, PULSE-COUNTERS-1): one file on the manager, none to download per droplet. The
file is opened once (enrichment.SharedEnricher). Without it (none configured, or it cannot be
opened) the droplet's enrichment is imported as uploaded and an alert says so; a
run whose artifacts enrichment refuses goes on to the import, which refuses it too.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiohttp import web

from ..enrichment import MMDB_ENV, SharedEnricher, Unavailable
from .db import Database
from .events import raise_alert

log = logging.getLogger(__name__)

ENRICHER_KEY: web.AppKey[SharedEnricher] = web.AppKey("enricher", SharedEnricher)


def configured() -> SharedEnricher | None:
    """The manager's enricher of the file `$DBIP_MMDB` names; None when it names none."""
    path = os.environ.get(MMDB_ENV)
    return SharedEnricher(path) if path else None


async def enrich(
    db: Database, enricher: SharedEnricher | None, batch_id: int, directory: Path, summary: dict
) -> None:
    """Replace the pulse run's enrichment in `directory` with the manager's release."""
    if enricher is None or not summary.get("pool_eligible"):
        return
    try:
        await asyncio.to_thread(enricher.reenrich, directory)
    except Unavailable as exc:
        log.warning("DB-IP file unavailable (%s): batch %s keeps its node's", exc, batch_id)
        async with db.transaction() as conn:
            await raise_alert(
                conn,
                "dbip_unavailable",
                f"pulse runs keep their droplet's DB-IP enrichment: the manager's file ({exc})",
                severity="major",
                dedupe_key="dbip_unavailable:pulse",
                data={"batch_id": batch_id, "error": str(exc)},
            )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("batch %s not enriched here (%s)", batch_id, type(exc).__name__)
