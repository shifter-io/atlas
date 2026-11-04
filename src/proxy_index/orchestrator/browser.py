"""The browser pulse planner (job browser_plan, K-BP5): per-target slots in the browser region.

It plans only while the setting browser.enabled is true and the settings file at the setting
browser.settings_file loads with `enabled = true`. Every [[target]] of that file (index i of n,
in start order) has its own interval: its slot is floor(now / every_minutes) since the epoch,
and each slot gets one 'browser' batch per [schedule] country in the browser region, starting
lead_seconds + i x every / n into the slot, so the targets of a host spread over it (D8). A slot
first planned more than GRACE after it began (a restart, the planner just switched on) spreads
from now over what is left of it minus LATE_END instead; a target whose start would fall after
that waits for its next slot. Each new batch gets a queued run for every network that has a node
in the browser region, and a batch without one closes at once. Census and pulse never plan in
the browser region (batches.participants leaves its nodes out).

The settings file is parsed again only when its SHA-256 changes. An unreadable or invalid file
raises the alert browser_settings_invalid (one alert, its occurrences counted) and nothing is
planned; the alert is acknowledged once the file loads again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from ..browser.contracts import BROWSER_KIND, BROWSER_REGION_ID
from ..browser.settings import BrowserSettings, load_settings, settings_sha256
from ..errors import UserError
from .batches import PLAN_LOCK, close_batch
from .db import Database, setting
from .events import raise_alert
from .nodes import BROWSER_REGION_ROW, BROWSER_REGION_SQL

GRACE = timedelta(seconds=60)  # a slot opened this late still uses its whole interval
LATE_END = timedelta(minutes=10)  # a late slot leaves this much of its end unplanned
ALERT = "browser_settings_invalid"
# Per settings path: the SHA-256 of the bytes last parsed and the settings, or the error.
_LOADED: dict[str, tuple[str, BrowserSettings | str]] = {}


def slot_starts(
    targets: list[tuple[str, int]], now: datetime, lead: timedelta
) -> list[tuple[str, datetime, datetime]]:
    """(target, slot, start_at) for each (target, every_minutes), in order, whose current slot
    may open now: on time, start_at = slot + lead + i x every / n; late (now more than GRACE
    after the slot), now + lead + i x (rest of the slot - LATE_END) / n, and none after that."""
    starts = []
    for i, (target, minutes) in enumerate(targets):
        every = timedelta(minutes=minutes)
        step = every.total_seconds()
        slot = datetime.fromtimestamp(now.timestamp() // step * step, UTC)
        late = now - slot > GRACE
        origin, span = (now, slot + every - LATE_END - now) if late else (slot, every)
        start = origin + lead + span * i / len(targets)
        if late and (span <= timedelta(0) or start > slot + every - LATE_END):
            continue
        starts.append((target, slot, start))
    return starts


def _settings(path: str) -> BrowserSettings | str:
    """The settings at path, parsed again only when the file's bytes changed; else the reason
    it cannot be used (UserError messages name a field, never a value)."""
    try:
        digest = settings_sha256(path)
    except OSError:
        return "settings: the file cannot be read"
    cached = _LOADED.get(path)
    if cached is None or cached[0] != digest:
        try:
            loaded: BrowserSettings | str = load_settings(path)
        except UserError as error:
            loaded = str(error)
        cached = _LOADED[path] = (digest, loaded)
    return cached[1]


async def _open(
    conn: psycopg.AsyncConnection, target: str, slot: datetime, start: datetime, countries
) -> list[int]:
    rows = await (
        await conn.execute(
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at) SELECT %s,u.c,%s,%s,%s,%s FROM unnest(%s::text[]) u(c) ORDER BY u.c "
            "ON CONFLICT DO NOTHING RETURNING batch_id",
            (BROWSER_KIND, BROWSER_REGION_ID, target, slot, start, list(countries)),
        )
    ).fetchall()
    return [row["batch_id"] for row in rows]


async def _create_runs(conn: psycopg.AsyncConnection, batch_ids: list[int]) -> int:
    """A queued run per network with a browser node in each batch; returns how many networks."""
    if batch_ids:
        await conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,node_id,status) "
            "SELECT b.id,p.product_id,n.node_id,'queued' FROM unnest(%s::bigint[]) b(id) "
            "CROSS JOIN nodes n JOIN products p ON p.provider=n.provider AND p.name=n.product "
            "WHERE n.region_id=%s ON CONFLICT DO NOTHING",
            (batch_ids, BROWSER_REGION_ID),
        )
    row = await (
        await conn.execute(
            "SELECT count(*) AS networks FROM nodes WHERE region_id=%s", (BROWSER_REGION_ID,)
        )
    ).fetchone()
    return row["networks"]


async def _usable(conn: psycopg.AsyncConnection) -> BrowserSettings | dict[str, Any]:
    """The enabled settings, or the job's skip result."""
    if not await setting(conn, "browser.enabled"):
        return {"skipped": "browser.enabled is off"}
    path = await setting(conn, "browser.settings_file")
    if not path:
        return {"skipped": "browser.settings_file is not set"}
    settings = _settings(str(path))
    if isinstance(settings, str):
        await raise_alert(
            conn,
            ALERT,
            f"The browser settings file cannot be used ({settings}); no browser runs are planned",
            severity="major",
            dedupe_key=ALERT,
            data={"error": settings},
        )
        return {"skipped": "the browser settings file is invalid"}
    await conn.execute(
        "UPDATE alerts SET acknowledged_at=now() WHERE dedupe_key=%s AND acknowledged_at IS NULL",
        (ALERT,),
    )
    if not settings.enabled:
        return {"skipped": "the browser settings file has enabled = false"}
    return settings


async def browser_plan(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """Open the current slot of every settings target that has no batch for it yet.

    Returns {"opened": target ids, "created": batches, "networks": networks with a browser
    node}, or {"skipped": reason} when the planner is off or the settings file is unusable.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    async with db.transaction() as conn:
        settings = await _usable(conn)
        if not isinstance(settings, BrowserSettings):
            return settings
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (PLAN_LOCK,))
        await conn.execute(BROWSER_REGION_SQL, BROWSER_REGION_ROW)
        lead = timedelta(seconds=settings.schedule.lead_seconds)
        targets = [(target.id, target.every_minutes) for target in settings.targets]
        opened, created = [], []
        for target, slot, start in slot_starts(targets, now, lead):
            batch_ids = await _open(conn, target, slot, start, settings.schedule.countries)
            if batch_ids:
                opened.append(target)
                created.extend(batch_ids)
        networks = await _create_runs(conn, created)
        if not networks:
            for batch_id in created:
                await close_batch(conn, batch_id, now)
    return {"opened": opened, "created": len(created), "networks": networks}
