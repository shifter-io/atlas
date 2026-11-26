"""A small async connection pool over psycopg, and the settings and partition helpers.

The framework has no pool dependency; the orchestrator's traffic (a heartbeat every ten seconds
and a progress post every thirty per node, plus the jobs) needs only a handful of connections.
Connections are autocommit; writes that must agree run inside `transaction()`.

A transaction that closes a test batch (`mark_closed`) hands the batch ids to the `on_close`
hooks once it has committed (the HTML reports); a hook never runs for a rolled-back close and
never delays or fails the transaction that closed the batch.

A connection found dropped (the server restarted or shut it down: `connection_dropped`) makes
the pool close its idle ones too, which the same restart dropped. `reconnecting` calls a
database step again on a dropped connection, with backoff, for about RECONNECT_SECONDS
(PULSE-DB-RECONNECT-1: the pulse hour's steps, each safe to run twice).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Any

import psycopg
from psycopg import pq
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .. import partitions
from ..providers.dbconnect import connect_options, session_options

log = logging.getLogger(__name__)

CloseHook = Callable[[list[int]], Awaitable[None]]
# The server shut the session down (57P01 admin shutdown, 57P02 crash, 57P03 cannot connect now).
SHUTDOWN_STATES = frozenset({"57P01", "57P02", "57P03"})
RECONNECT_SECONDS = 60.0
RECONNECT_DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0)  # then 15 s each
# Batch ids closed inside a connection's open transaction, keyed weakly by the connection.
_CLOSED: weakref.WeakKeyDictionary[psycopg.AsyncConnection, list[int]] = weakref.WeakKeyDictionary()

DEFAULTS: dict[str, Any] = {
    # Seconds between a census slot and the synchronized start every node waits for.
    "census.lead_seconds": 30,
    # Completed requests before the unique-ratio rule is evaluated (including failures).
    "census.min_sample": 1000,
    # A census that had successes and then none for this long stops as 'stalled' (an outage).
    "census.stall_seconds": 600,
    # Minutes between census slots for every enabled country, aligned to the epoch; None keeps
    # the daily countries.census_time slot.
    "census.every_minutes": None,
    # Seconds between a pulse slot opening and its synchronized start.
    "pulse.lead_seconds": 15,
    # A pulse request's timeout (ms); census and load keep the target's timeout_ms.
    "pulse.timeout_ms": 10000,
    # On-demand pulses (PULSE-ONDEMAND-1, orchestrator/pulse_hour.py): when true, pulse_plan and
    # census_plan plan nothing and the hourly pulse job opens each hour's pulses for the
    # droplets it armed, a region's countries back to back, pulse.step_seconds apart.
    "pulse.on_demand": False,
    "pulse.step_seconds": 12,
    # A pulse node with a run this many minutes overdue and no heartbeat, or no run progressed
    # or finished, in that time has stalled: its pulses move (pulse_moves.py).
    "pulse.stall_minutes": 3,
    # A pulse node with an overdue run whose host reports at least this I/O pressure (PSI some
    # avg10, %; node_health) has stalled too: a disk-starved droplet never catches up.
    "pulse.stall_io_pressure": 80,
    # ... or with this many runs that overdue at once: falling behind (one on time has 0 to 2).
    "pulse.stall_behind_runs": 6,
    # ... or with at least this share of its recent answers timeouts (runs started 2 to 5
    # minutes ago) while its network's other droplets stay under half of it: one droplet failing.
    "pulse.stall_timeout_share": 0.5,
    # The network pulse evaluator (orchestrator/pulse.py), per complete 5-minute window: down
    # when connection errors / requests over all regions reach down_error_share and at least
    # down_min_regions regions each reach it; the outage closes after recover_windows healthy
    # windows. latency_rise when latency_windows consecutive pulse runs of a country (runs, one
    # per slot, not 5-minute windows) have a p50 of at least latency_ratio x the baseline
    # (pulse.latency_baseline). A window is evaluated settle_seconds after it ends, so it must
    # cover the upload lag; evaluated_until is the evaluator's own watermark.
    "pulse.down_error_share": 0.5,
    "pulse.down_min_regions": 3,
    "pulse.recover_windows": 1,
    "pulse.latency_ratio": 1.5,
    "pulse.latency_windows": 2,
    "pulse.settle_seconds": 180,
    "pulse.evaluated_until": None,
    # Latency under load: every pulse slot of a census target adds one 'load' batch per runner
    # region, in the region's reference country (load_references), where every product fires
    # census_concurrency requests at census concurrency. The reference is chosen once per UTC
    # day: an override when one is set for the region; else the fastest enabled country tested
    # from the region over the trailing load.reference_days (lowest light-load p50 of its census
    # target pulses that did not run during a census, with at least
    # load.reference_min_successes successes); else the region's default; else its first
    # enabled country. Keys are region slugs. load.enabled switches the wave off.
    "load.enabled": True,
    "load.reference_overrides": {},
    "load.reference_defaults": {
        "ams3": "NL",
        "blr1": "IN",
        "fra1": "DE",
        "lon1": "GB",
        "nyc3": "US",
        "sgp1": "SG",
        "syd1": "AU",
        "tor1": "CA",
    },
    "load.reference_days": 7,
    "load.reference_min_successes": 300,
    # The load wave starts this long after its slot's pulses, so the reference pulse it is
    # compared with is measured at light load (a pulse of 100 at 25 takes at most 4 x 15 s).
    "load.delay_seconds": 60,
    # A queued run whose start_at passed this long ago without the node starting it fails.
    "batch.start_grace_seconds": 180,
    # A running or uploading run silent for this long fails; nodes post progress every 30 s.
    "batch.stale_seconds": 900,
    "node.offline_seconds": 60,
    "calculate.min_interval_seconds": 300,
    # "hourly": one calculation per UTC hour, hour_offset_seconds past it (after that hour's
    # estimate is released); "batch": the old schedule, after each finished batch, debounced to
    # min_interval_seconds. The calculator floors as_of to 5 minutes and the release job runs
    # every minute, so an offset below 360 s would leave out the hour just released (jobs.py
    # raises it to 360 with a warning).
    "calculate.trigger": "hourly",
    "calculate.hour_offset_seconds": 360,
    "retention.events_days": 90,
    "retention.job_runs_days": 30,
    # Estimated hours older than this are deleted. The archive (atlas.archive) moves them
    # with their run once the longest calculator window (30 days) has passed; this is the
    # fallback without it, kept longer so a late archive run still finds them.
    "retention.estimates_days": 35,
    # Upload limits for /v1/results; past the time limit the body is refused with 408.
    "results.max_upload_bytes": 1 << 30,
    "results.max_extracted_bytes": 4 << 30,
    # The most rows an upload's summary may declare for its IP or geo artifact; more is refused
    # (400) before any artifact is read.
    "results.max_artifact_rows": 5_000_000,
    "results.upload_timeout_seconds": 600,
    # Census and pulse uploads received and imported at once by this process; the next is 503
    # with Retry-After before its body is read (INGEST-HANG-1). The healthy peak of 25 Nov 2026
    # was a few hundred (380 answered in one second); 26 Nov's blocked imports left 16,000.
    "results.ingest_concurrency": 2000,
    # The planner switches: when off, census_plan / pulse_plan record 'skipped' and plan nothing.
    "scheduler.census_enabled": True,
    "scheduler.pulse_enabled": True,
    # Directory for the HTML batch reports written when a batch closes; None writes none.
    "reports.dir": None,
    # The daily raw-data archive (atlas.archive): off until the bucket is set up.
    "archive.enabled": False,
    "archive.spaces_file": ".secrets/spaces.toml",
    "archive.work_dir": ".state/archive",
    # The raw files of each update, kept 30 days and uploaded hourly (None keeps none).
    "raw.spool_dir": None,
    # The daily DB-IP update (jobs.dbip_update): the release directory (None runs no update) and
    # the CDN directory holding dbip-full-YYYY-MM-DD.mmdb (None: $DBIP_BASE_URL).
    "dbip.dir": None,
    "dbip.base_url": None,
    # The browser pulse planner (orchestrator/browser.py, job browser_plan): off until true, and
    # the path of the private browser settings file (schema 2); None plans nothing.
    "browser.enabled": False,
    "browser.settings_file": None,
    # A browser run whose counters have not moved for this long is failed as stale and its place
    # on the host freed (orchestrator/browser_hosts.py); census and pulse runs keep stale_seconds.
    "browser.stall_minutes": 30,
    # The coverage job (orchestrator/coverage.py, COVERAGE-ALERT-1): a network's browser target,
    # its pulses or its daily tests with no completed run for this many hours raise its alert.
    # Browser: above the normal cycle (a pair's counted runs are up to 28 h apart plus a run's
    # 3 h deadline; COVERAGE-ALERT-2), so a once-a-day target's 26-27 h gap is not one.
    "coverage.browser_hours": 36,
    "coverage.pulse_hours": 3,
    "coverage.daily_hours": 24,
    # Browser bundle ingest (orchestrator/browser_ingest.py, K-BP7): bundles processed at once
    # by this process, the Spaces prefix of their objects, and a local directory used instead
    # of Spaces with the same key layout (staging and tests only, never production).
    "browser.ingest_concurrency": 4,
    "browser.archive_prefix": "browser-files",
    "browser.archive_dir": None,
    # The calculate job's subprocess is killed after this long (MANAGER-ORCH-1). A build may take
    # up to 3 hours (CALC-LIMITS-3): this kills it at 10800 s, the scope's RuntimeMaxSec (this +
    # 60 s) is the hard limit, and job_stuck pages one still running 300 s after this. The
    # scheduler skips the job's fires while it runs, so two calculations never overlap.
    "calculate.timeout_seconds": 10800,
    # The calculation's reads (calculated/parallel_reads.py, CALC-PARALLEL-1): the helper
    # connections that read its missed groups in its snapshot (1 to max(16, CPUs / 2); `calculate
    # --parallel-reads` overrides it), of which at most heavy_parallel read the groups above
    # heavy_rows estimated observation rows, at heavy_work_mem, the others at work_mem (also the
    # calculation's own); the most server memory the helpers may take together (each 2 x its
    # work_mem; the database server is shared with production): the helpers are lowered to fit.
    # None: sized to the machine (atlas/sizing.py); the memory ones are the database server's.
    "calculate.parallel_reads": None,
    # Worker processes measuring an incremental build's product groups, each over its own
    # snapshot connection (calculated/parallel_measure.py); 1: in the calculation's process.
    "calculate.workers": 1,
    "calculate.work_mem": "256MB",
    "calculate.heavy_parallel": None,
    "calculate.heavy_rows": 1_000_000,
    "calculate.heavy_work_mem": "1GB",
    # The series points' distinct IPs (calculated/point_ips.py) are counted in batches of at
    # most this many estimated observation rows each.
    "calculate.point_rows": 20_000_000,
    "calculate.max_parallel_memory": "6GB",
    # The hourly ingest (INGEST-SCALE-1): daily tests per transaction, transactions at once, the
    # worker processes they go over and the bytes in flight. None: sized to the machine.
    "ingest.batch": 20,
    "ingest.connections": None,
    "ingest.processes": None,
    "ingest.memory_budget": None,
    # The connections left free (sizing.capped): of max_connections at least reserve, or
    # reserve_share of it; of the login role's connection limit, if any, role_reserve.
    "database.reserve": 20,
    "database.reserve_share": 0.25,
    "database.role_reserve": 10,
}


def mark_closed(conn: psycopg.AsyncConnection, batch_id: int) -> None:
    """Record that this connection's transaction closed a batch (see Database.on_close)."""
    _CLOSED.setdefault(conn, []).append(batch_id)


class Database:
    """Reusable autocommit connections, at most `size` at once."""

    def __init__(self, credentials_file: str, *, size: int = 8) -> None:
        self.credentials_file = str(credentials_file)
        self._credentials = connect_options(credentials_file)
        self._idle: list[psycopg.AsyncConnection] = []
        self._slots = asyncio.Semaphore(size)
        self._partitions: set[str] = set()
        # Called with the batch ids a committed transaction closed; keyed so a hook is
        # installed once however many apps share this pool.
        self.on_close: dict[str, CloseHook] = {}
        self._hooks: set[asyncio.Task[None]] = set()

    async def _open(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(
            **self._credentials,
            **session_options(),  # CALC-STALE-SESSION-1: keepalives (dbconnect.py)
            autocommit=True,
            connect_timeout=5,
            row_factory=dict_row,
        )

    @contextlib.asynccontextmanager
    async def connection(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self._slots:
            conn = self._idle.pop() if self._idle else await self._open()
            reusable = False
            try:
                yield conn
                reusable = True
            finally:
                if (
                    reusable
                    and not conn.closed
                    and not conn.broken
                    and conn.info.transaction_status == pq.TransactionStatus.IDLE
                ):
                    self._idle.append(conn)
                else:
                    dropped = conn.broken
                    await conn.close()
                    if dropped:
                        await self._close_idle()

    async def _close_idle(self) -> None:
        """Close the idle connections: one found dropped means a restart dropped them too."""
        idle, self._idle = self._idle, []
        for conn in idle:
            await conn.close()

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self.connection() as conn:
            _CLOSED.pop(conn, None)
            try:
                async with conn.transaction():
                    yield conn
            finally:
                closed = _CLOSED.pop(conn, None)
            # Reached only once the transaction committed.
            if closed:
                self._notify(closed)

    def _notify(self, batch_ids: list[int]) -> None:
        for name, hook in self.on_close.items():
            task = asyncio.create_task(self._run_hook(name, hook, list(batch_ids)))
            self._hooks.add(task)
            task.add_done_callback(self._hooks.discard)

    @staticmethod
    async def _run_hook(name: str, hook: CloseHook, batch_ids: list[int]) -> None:
        try:
            await hook(batch_ids)
        except Exception:  # boundary: a report must never fail the barrier
            log.exception("batch close hook %s failed for batches %s", name, batch_ids)

    async def drain(self) -> None:
        """Wait for the close hooks already started, and any they start (tests, shutdown).

        Finished hooks are dropped here, not left to their done callbacks: a hook that finished
        in the current loop step still has its callback queued, and on Python 3.12 gather()
        over finished tasks returns without yielding, so the callback would never run and the
        loop would spin forever.
        """
        while self._hooks:
            await asyncio.gather(*self._hooks, return_exceptions=True)
            self._hooks.difference_update([task for task in self._hooks if task.done()])

    async def close(self) -> None:
        await self.drain()
        await self._close_idle()

    async def ensure_partition(self, table: str, moment: datetime) -> None:
        """partitions.partition for a monthly table (test_events) on a connection of its own, in
        a transaction of its own (a caller's that rolls back cannot leave the cache claiming the
        partition exists); asked once per month and process."""
        name, start, end = partitions.monthly(table, moment)
        if name in self._partitions:
            return
        async with self.connection() as conn:
            found = await conn.execute("SELECT to_regclass(%s) AS found", (name,))
            if (await found.fetchone())["found"] is None:
                await _run_in_transaction(conn, partitions.creation(table, name, start, end))
        self._partitions.add(name)


def connection_dropped(error: BaseException) -> bool:
    """A connection lost, not a refused statement: the server shut the session down
    (SHUTDOWN_STATES), a class 08 connection error, or one without a SQLSTATE (reset, closed by
    the server, refused, timed out)."""
    if not isinstance(error, psycopg.OperationalError):
        return False
    state = error.sqlstate
    return state is None or state in SHUTDOWN_STATES or state.startswith("08")


async def reconnecting[T](clock, call: Callable[..., Awaitable[T]], *args: Any) -> T:
    """`call(*args)`, called again on a dropped connection (each time on a new connection: the
    pool closes a dropped one) after RECONNECT_DELAYS while the next call starts within
    RECONNECT_SECONDS of the first failure; then the last failure is raised. Any other error is
    raised at once. `clock`: now() and sleep(), as the pulse hour's."""
    first, delays = None, iter(RECONNECT_DELAYS)
    while True:
        try:
            return await call(*args)
        except psycopg.OperationalError as error:
            if not connection_dropped(error):
                raise
            now, delay = clock.now(), next(delays, RECONNECT_DELAYS[-1])
            first = now if first is None else first
            if now + delay > first + RECONNECT_SECONDS:
                raise
            kind, name = type(error).__name__, getattr(call, "__name__", "step")
            log.warning("%s: connection dropped (%s), again in %g s", name, kind, delay)
        await clock.sleep(delay)


async def _run_in_transaction(conn: psycopg.AsyncConnection, queries) -> None:
    async with conn.transaction():
        for query in queries:
            await conn.execute(query)


async def setting(conn: psycopg.AsyncConnection, key: str) -> Any:
    """An operator setting from the settings table, else the built-in default."""
    row = await (await conn.execute("SELECT value FROM settings WHERE key=%s", (key,))).fetchone()
    return row["value"] if row else DEFAULTS[key]


async def put_setting(conn: psycopg.AsyncConnection, key: str, value: Any) -> None:
    await conn.execute(
        "INSERT INTO settings(key,value) VALUES (%s,%s) ON CONFLICT(key) DO UPDATE "
        "SET value=excluded.value,updated_at=now()",
        (key, Jsonb(value)),
    )


@contextlib.asynccontextmanager
async def session(db: Database) -> AsyncIterator[psycopg.AsyncConnection]:
    """A connection of its own, outside the pool, closed on exit (which ends its session locks):
    for a lock held while the work it guards uses the pool, so it can never take the slot that
    work waits for (browser_ingest's per-run advisory lock)."""
    conn = await db._open()
    try:
        yield conn
    finally:
        await conn.close()
