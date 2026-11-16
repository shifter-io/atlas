"""The browser pulse planner (job browser_plan, K-BP5): per-target slots in the browser region,
or, with `[schedule] plan = "queue"`, each host's once-a-period queue (browser_queue.py).

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

In queue mode each queue entry gets its own batch (scheduled_for = start_at = its start) with one
run, for its host's node of its network, opened OPEN_AHEAD before its start. An entry missed by
more than GRACE (a restart) catches up at its host's next position from now that holds no other
catch-up and whose own run is on another network: a batch keeping its scheduled_for, starting
there. If a run starting there could not end (start + run_deadline_minutes) before the entry's
next start, it is skipped instead: a 'skipped' batch and the alert browser_run_missed, one per
host and day. Entries before a host's first on-time batch, or a period old, are never missed.
With [schedule] queue_start, the entries before it are the daily queue's and those from it the
configured queue's (browser_queue.near); a catch-up takes a position of the queue in force now.
A host runs at most browsers_per_host browser runs at once: an entry of a full host waits
(browser_hosts.py, which also fails stuck runs through the reaper and alerts).

The settings file is parsed again only when its SHA-256 changes. An unreadable or invalid file
raises the alert browser_settings_invalid (one alert, its occurrences counted) and nothing is
planned; the alert is acknowledged once the file loads again. A settings target with no row in
`targets` (a database seeded before it was added) raises the alert browser_targets_missing, naming
the ids and the fix, `atlas db-seed --only targets`; it is not planned, the other targets
are, and the alert is acknowledged once every target is there.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from ..browser.contracts import BROWSER_KIND, BROWSER_REGION_ID
from ..browser.settings import BrowserSettings, Schedule, load_settings, settings_sha256
from ..errors import UserError
from .batches import PLAN_LOCK, close_batch
from .browser_hosts import OPEN_AHEAD, check_limit, open_runs
from .browser_queue import Entry, HostQueue, in_force, near
from .db import Database, setting
from .events import raise_alert
from .nodes import BROWSER_REGION_ROW, BROWSER_REGION_SQL

GRACE = timedelta(seconds=60)  # a slot opened this late still uses its whole interval
LATE_END = timedelta(minutes=10)  # a late slot leaves this much of its end unplanned
ALERT = "browser_settings_invalid"
TARGETS_ALERT = "browser_targets_missing"
MISSED_ALERT = "browser_run_missed"
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


async def _acknowledge(conn: psycopg.AsyncConnection, key: str) -> None:
    await conn.execute(
        "UPDATE alerts SET acknowledged_at=now() WHERE dedupe_key=%s AND acknowledged_at IS NULL",
        (key,),
    )


async def _missing(conn: psycopg.AsyncConnection, settings: BrowserSettings) -> list[str]:
    """The settings targets with no row in `targets`, alerted; acknowledged when there are none."""
    ids = [target.id for target in settings.targets]
    rows = await (
        await conn.execute("SELECT target_id FROM targets WHERE target_id = ANY(%s)", (ids,))
    ).fetchall()
    present = {row["target_id"] for row in rows}
    missing = [target for target in ids if target not in present]
    if not missing:
        await _acknowledge(conn, TARGETS_ALERT)
        return missing
    await raise_alert(
        conn,
        TARGETS_ALERT,
        f"The browser targets {', '.join(missing)} are not in targets and are not planned; "
        "run `atlas db-seed --only targets`",
        severity="major",
        dedupe_key=TARGETS_ALERT,
        data={"missing": missing},
    )
    return missing


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
    await _acknowledge(conn, ALERT)
    if not settings.enabled:
        return {"skipped": "the browser settings file has enabled = false"}
    return settings


async def _queue_batches(conn: psycopg.AsyncConnection, since: datetime) -> dict[tuple, datetime]:
    """(target, country, scheduled_for) -> start_at of the browser batches since `since`."""
    rows = await (
        await conn.execute(
            "SELECT target_id,country_code,scheduled_for,start_at FROM test_batches "
            "WHERE kind=%s AND scheduled_for>=%s",
            (BROWSER_KIND, since),
        )
    ).fetchall()
    return {
        (row["target_id"], row["country_code"], row["scheduled_for"]): row["start_at"]
        for row in rows
    }


async def _open_entry(
    conn: psycopg.AsyncConnection, entry: Entry, start: datetime, now: datetime
) -> bool:
    """The entry's batch (scheduled_for: the entry's own start; start_at: `start`) with one run,
    for its host's node of its network; closed at once when there is no such node."""
    row = await (
        await conn.execute(
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING batch_id",
            (BROWSER_KIND, entry.country, BROWSER_REGION_ID, entry.target, entry.start, start),
        )
    ).fetchone()
    if row is None:
        return False
    provider, product = entry.network.split("/", 1)
    cursor = await conn.execute(
        "INSERT INTO batch_runs(batch_id,product_id,node_id,status) "
        "SELECT %s,p.product_id,n.node_id,'queued' FROM nodes n JOIN products p "
        "ON p.provider=n.provider AND p.name=n.product WHERE n.region_id=%s AND n.provider=%s "
        "AND n.product=%s AND n.host=%s ON CONFLICT DO NOTHING",
        (row["batch_id"], BROWSER_REGION_ID, provider, product, entry.host),
    )
    if not cursor.rowcount:
        await close_batch(conn, row["batch_id"], now)
    return True


async def _skip_entry(conn: psycopg.AsyncConnection, entry: Entry, now: datetime) -> None:
    """A missed entry that cannot finish in time: a 'skipped' batch, so it is decided once, and
    the alert browser_run_missed, one per host and day (its occurrences count the runs)."""
    await conn.execute(
        "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,start_at,"
        "finished_at,status) VALUES (%s,%s,%s,%s,%s,%s,%s,'skipped') ON CONFLICT DO NOTHING",
        (BROWSER_KIND, entry.country, BROWSER_REGION_ID, entry.target, entry.start, entry.start)
        + (now,),
    )
    day = entry.start.date().isoformat()
    await raise_alert(
        conn,
        MISSED_ALERT,
        f"Browser runs of {entry.host} due on {day} were missed and could not catch up in time; "
        "they are skipped",
        dedupe_key=f"{MISSED_ALERT}:{entry.host}:{day}",
        data={"host": entry.host, "day": day, "target": entry.target, "country": entry.country},
    )


def _catch_up_at(
    queue: HostQueue, entry: Entry, taken: set[datetime], now: datetime, schedule: Schedule
) -> datetime | None:
    """The host's first position from now that no catch-up holds and whose own run is on
    another network, if a run starting there still ends before the entry's next start."""
    lead = timedelta(seconds=schedule.lead_seconds)
    deadline = timedelta(minutes=schedule.run_deadline_minutes)
    rounds = len(queue.items) * len(queue.networks)
    day = queue.day(now)
    day_start = queue.day_start(day)
    k = -((queue.position(day_start, lead, 0) - now) // queue.gap)  # the first at or after now
    while (start := queue.position(day_start, lead, k)) + deadline <= entry.start + queue.period:
        extra, position = divmod(k, rounds)
        if start not in taken and queue.network_at(day + extra, position) != entry.network:
            return start
        k += 1
    return None


def _began(entries: list[Entry], batches: dict[tuple, datetime]) -> dict[str, datetime]:
    """Each host's first on-time batch (start_at = scheduled_for) among these entries. Before
    it the queue was not running for that host (just switched on, a new host): not missed."""
    began: dict[str, datetime] = {}
    for entry in entries:
        if batches.get((entry.target, entry.country, entry.start)) == entry.start:
            began.setdefault(entry.host, entry.start)
    return began


async def plan_queue(
    conn: psycopg.AsyncConnection, settings: BrowserSettings, now: datetime, missing: list[str]
) -> dict[str, Any]:
    """Queue mode: open every entry due within OPEN_AHEAD. One missed by more than GRACE (a
    restart) catches up at its host's next free position, or is skipped when it could not
    finish before its next start; one a period old or older is no longer looked at. An entry
    whose host has browsers_per_host open runs waits (browser_hosts.py)."""
    limit = settings.browser.browsers_per_host
    runs = await open_runs(conn, now)
    await check_limit(conn, runs, limit)
    hosts = in_force(settings, now)
    entries = near(settings, now)
    batches = await _queue_batches(conn, entries[0].start)
    began = _began(entries, batches)
    owner = {(e.target, e.country, e.start): e.host for e in entries}
    # (host, start) of the catch-ups: a batch that starts at another time than its own.
    taken = {(owner.get(key), start) for key, start in batches.items() if start != key[2]}
    result: dict[str, Any] = {"opened": [], "created": 0, "caught_up": 0, "missed": 0}
    for entry in entries:
        key = (entry.target, entry.country, entry.start)
        if entry.target in missing or key in batches:
            continue
        if not now - hosts[entry.host].period < entry.start <= now + OPEN_AHEAD:
            continue
        late = now - entry.start > GRACE
        if late and not began.get(entry.host, now) < entry.start:
            continue
        if runs[entry.host] >= limit:
            result["waiting"] = result.get("waiting", 0) + 1
            continue
        start: datetime | None = entry.start
        if late:
            held = {at for host, at in taken if host == entry.host}
            start = _catch_up_at(hosts[entry.host], entry, held, now, settings.schedule)
            if start is None:
                await _skip_entry(conn, entry, now)
                result["missed"] += 1
                continue
            taken.add((entry.host, start))
            result["caught_up"] += 1
        if await _open_entry(conn, entry, start, now):
            result["opened"].append(entry.target)
            result["created"] += 1
            runs[entry.host] += start <= now + OPEN_AHEAD
    return result


async def _plan_slots(
    conn: psycopg.AsyncConnection, settings: BrowserSettings, now: datetime, missing: list[str]
) -> dict[str, Any]:
    lead = timedelta(seconds=settings.schedule.lead_seconds)
    targets = [(target.id, target.every_minutes) for target in settings.targets]
    opened, created = [], []
    for target, slot, start in slot_starts(targets, now, lead):
        if target in missing:  # its spread place stays free; the others keep theirs
            continue
        batch_ids = await _open(conn, target, slot, start, settings.schedule.countries)
        if batch_ids:
            opened.append(target)
            created.extend(batch_ids)
    networks = await _create_runs(conn, created)
    if not networks:
        for batch_id in created:
            await close_batch(conn, batch_id, now)
    return {"opened": opened, "created": len(created), "networks": networks}


async def browser_plan(
    db: Database, now: datetime | None = None, given: BrowserSettings | None = None
) -> dict[str, Any]:
    """Open what is due: in slots mode the current slot of every settings target that has no
    batch for it yet, in queue mode the queue entries that are due (plan_queue).

    Returns {"opened": target ids, "created": batches, "networks": networks with a browser
    node}, in queue mode also "caught_up" and "missed" (counts) and "waiting" (entries whose
    host is full) when there are any, plus "missing": target ids
    not in `targets` when there are any, or {"skipped": reason} when the planner is off or the
    settings file is unusable.
    `given` settings (the scale replay's model) are planned as they are, without reading the
    settings browser.enabled and browser.settings_file.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    async with db.transaction() as conn:
        settings = given or await _usable(conn)
        if not isinstance(settings, BrowserSettings):
            return settings
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (PLAN_LOCK,))
        await conn.execute(BROWSER_REGION_SQL, BROWSER_REGION_ROW)
        missing = await _missing(conn, settings)
        if settings.schedule.plan == "queue":
            result = await plan_queue(conn, settings, now, missing)
            result["networks"] = await _create_runs(conn, [])
        else:
            result = await _plan_slots(conn, settings, now, missing)
    return result | ({"missing": missing} if missing else {})
