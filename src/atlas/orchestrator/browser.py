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
more than GRACE (a restart, or its host was full) starts at the next whole minute when its host
has a free place and no open run of its network (BROWSER-COVERAGE-1: no browser idles while a
run waits), else at its host's next position from now that holds no other catch-up and whose own
run is on another network: a batch keeping its scheduled_for, starting there. If a run starting
there could not end (start + run_deadline_minutes) before the entry's
next start, it is skipped instead: a 'skipped' batch and the alert browser_run_missed, one per
host and day. Entries before a host's first on-time batch, or a period old, are never missed.
An entry whose run its node never started (the reaper's not_started) is caught up the same way
once the node is online again, its batch reopened there (browser_rerun.py, BROWSER-COVERAGE-1).
An entry of a paused network (its host entry "<network>/paused", netpause) is skipped at its own
start without a run or a catch-up: a 'skipped' batch with paused_network set, so the hour report
expects it (VERIFY-PAUSED-BATCH-1).
With [schedule] queue_start, the entries before it are the daily queue's and those from it the
configured queue's (browser_queue.near); a catch-up takes a position of the queue in force now.
A host runs at most browsers_per_host browser runs at once: an entry of a full host waits, unless
it could no longer finish in time (skipped then), and starts once a place frees (browser_hosts.py,
which also fails stuck runs through the reaper and alerts). Slots mode is browser_slots.py.

The settings file is parsed again only when its SHA-256 changes. An unreadable or invalid file
raises the alert browser_settings_invalid (one alert, its occurrences counted) and nothing is
planned; the alert is acknowledged once the file loads again. A settings target with no row in
`targets` (a database seeded before it was added) raises the alert browser_targets_missing, naming
the ids and the fix, `atlas db-seed --only targets`; it is not planned, the other targets
are, and the alert is acknowledged once every target is there.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from ..browser.contracts import BROWSER_KIND, BROWSER_REGION_ID
from ..browser.settings import BrowserSettings, Schedule, load_settings, settings_sha256
from ..errors import UserError
from ..netpause import PAUSED
from . import browser_rerun
from .batches import PLAN_LOCK, close_batch
from .browser_hosts import OPEN_AHEAD, check_limit, open_runs
from .browser_queue import Entry, HostQueue, catch_up_at, first_on_time, in_force, near
from .browser_slots import GRACE, create_runs, plan_slots
from .browser_slots import LATE_END as LATE_END
from .browser_slots import slot_starts as slot_starts
from .db import Database, setting
from .events import raise_alert
from .nodes import BROWSER_REGION_ROW, BROWSER_REGION_SQL

MINUTE = timedelta(minutes=1)
ALERT = "browser_settings_invalid"
TARGETS_ALERT = "browser_targets_missing"
MISSED_ALERT = "browser_run_missed"
# Per settings path: the SHA-256 of the bytes last parsed and the settings, or the error.
_LOADED: dict[str, tuple[str, BrowserSettings | str]] = {}


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


async def _pause_entry(conn: psycopg.AsyncConnection, entry: Entry, now: datetime) -> None:
    """An entry of a paused network: a 'skipped' batch at its own start, marked with the network
    (paused_network), so it is decided once and the hour report does not count it as due."""
    await conn.execute(
        "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,start_at,"
        "finished_at,status,paused_network) VALUES (%s,%s,%s,%s,%s,%s,%s,'skipped',%s) "
        "ON CONFLICT DO NOTHING",
        (BROWSER_KIND, entry.country, BROWSER_REGION_ID, entry.target, entry.start, entry.start)
        + (now, entry.network.split("/", 1)[0]),
    )


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


@dataclass
class _Tick:
    """One plan_queue pass: each host's queue in force and open runs, the catch-ups, the result."""

    conn: psycopg.AsyncConnection
    schedule: Schedule
    now: datetime
    hosts: dict[str, HostQueue]
    runs: Counter[str]
    busy: set[tuple[str, str]]  # (host, network) of the open runs
    taken: set[tuple[str | None, datetime]]
    result: dict[str, Any]

    def in_time(self, entry: Entry, start: datetime) -> bool:
        """Whether a run of the entry starting then ends before the entry's next start."""
        deadline = timedelta(minutes=self.schedule.run_deadline_minutes)
        return start + deadline <= entry.start + self.hosts[entry.host].period

    def now_start(self, entry: Entry) -> datetime | None:
        """A late entry's start at the next whole minute (its host has a free place), unless its
        network has an open run on the host or it would not end in time (BROWSER-COVERAGE-1)."""
        start = self.now.replace(second=0, microsecond=0)
        start += MINUTE if start < self.now else timedelta(0)
        if (entry.host, entry.network) in self.busy or not self.in_time(entry, start):
            return None
        return start


async def _place(tick: _Tick, entry: Entry, rerun: int | None) -> None:
    """Open a due entry on time, or one more than GRACE late at once (its host has a free place)
    or else at its host's next free position (`rerun`: reopen its lost batch there), or skip it
    (a lost run stays failed)."""
    start: datetime | None = entry.start
    if tick.now - entry.start > GRACE:
        held = {at for host, at in tick.taken if host == entry.host}
        start = tick.now_start(entry) or catch_up_at(
            tick.hosts[entry.host], entry, held, tick.now, tick.schedule
        )
        if start is None:
            if rerun is None:
                await _skip_entry(tick.conn, entry, tick.now)
                tick.result["missed"] += 1
            return
        tick.taken.add((entry.host, start))
        counter = "caught_up" if rerun is None else "rerun"
        tick.result[counter] = tick.result.get(counter, 0) + 1
    if rerun is not None:
        await browser_rerun.reopen(tick.conn, rerun, start)
    elif await _open_entry(tick.conn, entry, start, tick.now):
        tick.result["opened"].append(entry.target)
        tick.result["created"] += 1
    else:
        return
    if start <= tick.now + OPEN_AHEAD:
        tick.runs[entry.host] += 1
        tick.busy.add((entry.host, entry.network))


async def plan_queue(
    conn: psycopg.AsyncConnection, settings: BrowserSettings, now: datetime, missing: list[str]
) -> dict[str, Any]:
    """Queue mode: open every entry due within OPEN_AHEAD. One missed by more than GRACE (a
    restart, or its host was full) starts at once when its host has a free place, else at its
    host's next free position, or is skipped when it could not finish before its next start
    (also while its host is full); one a period old or older is no longer looked at. An entry
    whose run its node never started is run again so once the node is online (browser_rerun.py,
    "rerun"). An entry whose host has browsers_per_host open runs waits (browser_hosts.py)."""
    limit = settings.browser.browsers_per_host
    runs, busy = await open_runs(conn, now)
    await check_limit(conn, runs, limit)
    hosts = in_force(settings, now)
    entries = near(settings, now)
    batches = await _queue_batches(conn, entries[0].start)
    lost = await browser_rerun.lost(conn, entries[0].start, now)
    began = first_on_time(entries, batches)
    owner = {(e.target, e.country, e.start): e.host for e in entries}
    # (host, start) of the catch-ups: a batch that starts at another time than its own.
    taken = {(owner.get(key), start) for key, start in batches.items() if start != key[2]}
    result: dict[str, Any] = {"opened": [], "created": 0, "caught_up": 0, "missed": 0}
    tick = _Tick(conn, settings.schedule, now, hosts, runs, busy, taken, result)
    for entry in entries:
        key = (entry.target, entry.country, entry.start)
        rerun = lost.get(key)
        if entry.target in missing or (key in batches and rerun is None):
            continue
        if not now - hosts[entry.host].period < entry.start <= now + OPEN_AHEAD:
            continue
        late = now - entry.start > GRACE
        if late and rerun is None and not began.get(entry.host, now) < entry.start:
            continue
        if entry.network.endswith(f"/{PAUSED}"):
            if rerun is None:
                await _pause_entry(conn, entry, now)
                result["paused"] = result.get("paused", 0) + 1
            continue
        if runs[entry.host] >= limit and not (late and not tick.in_time(entry, now)):
            result["waiting"] = result.get("waiting", 0) + 1
            continue
        await _place(tick, entry, rerun)
    return result


async def browser_plan(
    db: Database, now: datetime | None = None, given: BrowserSettings | None = None
) -> dict[str, Any]:
    """Open what is due: in slots mode the current slot of every settings target that has no
    batch for it yet, in queue mode the queue entries that are due (plan_queue).

    Returns {"opened": target ids, "created": batches, "networks": networks with a browser
    node}, in queue mode also "caught_up" and "missed" (counts), "waiting" (entries whose
    host is full), "paused" (entries of a paused network) and "rerun" (lost runs run again)
    when there are any, plus "missing": target ids not in `targets` when there are any, or
    {"skipped": reason} when the planner is off or the settings file is unusable.
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
            result["networks"] = await create_runs(conn, [])
        else:
            result = await plan_slots(conn, settings, now, missing)
    return result | ({"missing": missing} if missing else {})
