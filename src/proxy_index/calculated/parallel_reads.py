"""Reads in the calculation's snapshot over helper connections (CALC-PARALLEL-1).

The calculation's transaction exports its snapshot once (export()); each helper connection
(snapshot_connection()) opens REPEATABLE READ, READ ONLY, imports it with SET TRANSACTION
SNAPSHOT and the exporter's work_mem and TimeZone, and only reads, so it reads exactly what the
calculation reads. fleetsql.count_spans() counts its slices on such connections, and the
Prefetcher reads the missed groups' window inputs (incremental.py) on K of them, ahead of the
group loop that consumes them. Every write stays on the calculation's own connection.

Group sizes are very uneven (a big network's big countries read far more rows), so the
Prefetcher has two lanes (plan()): the heavy groups (above calculate.heavy_rows estimated rows),
largest first, on at most calculate.heavy_parallel helpers at calculate.heavy_work_mem, and the
light ones, in the consumer's order, on the other helpers at calculate.work_mem.
"""

from __future__ import annotations

import queue
import sys
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row

from .. import storage, storage_parallel
from ..orchestrator.control import read_setting

# calculate.<name> (orchestrator/db.py DEFAULTS): the counts, then the memory settings.
SETTINGS = ("parallel_reads", "heavy_parallel", "heavy_rows")
MEMORY = ("work_mem", "heavy_work_mem", "max_parallel_memory")
MAX_PARALLEL_READS = 16
MEMORY_UNITS = {"kB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
EXPORT = "SELECT pg_export_snapshot(),current_setting('work_mem'),current_setting('TimeZone')"
IMPORTED = "SELECT set_config('work_mem',%s,true),set_config('TimeZone',%s,true)"
MB = 1024**2
TEMP = (
    "SELECT temp_bytes FROM pg_stat_clear_snapshot(),pg_stat_database "
    "WHERE datname=current_database()"
)


def export(conn):
    """conn's transaction snapshot, exported, with its work_mem and TimeZone (which the session
    dates of sqlwindow's days follow): what snapshot_connection() imports."""
    return conn.cursor(row_factory=tuple_row).execute(EXPORT).fetchone()


def snapshot_connection(conn, exported):
    """A new read-only connection to conn's database whose transaction reads exported (export()):
    the snapshot, the work_mem and the TimeZone of conn's transaction."""
    snapshot, work_mem, zone = exported
    params = {"password": conn.info.password, "connect_timeout": 5}
    other = psycopg.connect(conn.info.dsn, **params)
    try:
        other.isolation_level, other.read_only = psycopg.IsolationLevel.REPEATABLE_READ, True
        other.execute(sql.SQL("SET TRANSACTION SNAPSHOT {}").format(sql.Literal(snapshot)))
        other.execute(IMPORTED, (work_mem, zone))
    except BaseException:
        other.close()
        raise
    return other


def settings(conn, parallel_reads=None):
    """The calculate.* settings (orchestrator/db.py DEFAULTS, the settings table), validated;
    parallel_reads (`calculate --parallel-reads`) overrides the table's."""
    found = {name: read_setting(conn, f"calculate.{name}") for name in SETTINGS + MEMORY}
    if parallel_reads is not None:
        found["parallel_reads"] = parallel_reads
    for name, most in zip(SETTINGS, (MAX_PARALLEL_READS, MAX_PARALLEL_READS, None), strict=True):
        n = found[name]
        if type(n) is not int or n < 1 or most and n > most:
            raise ValueError(f"calculate.{name} must be a whole number from 1 to {most or 'any'}")
    for name in MEMORY:
        memory_bytes(found[name])
    return found


def plan(found, heavy, light):
    """(heavy helpers, light helpers) for heavy and light groups under the settings found: at
    most heavy_parallel of parallel_reads helpers read heavy groups, the others light ones, none
    more than there are groups of its class, and fewer, light ones first, while the worst case
    (worst_case()) passes max_parallel_memory. Without a light helper, the heavy ones read the
    light groups too."""
    h = min(found["heavy_parallel"], found["parallel_reads"], heavy)
    n = min(found["parallel_reads"] - h, light)
    while worst_case(found, h, n) > memory_bytes(found["max_parallel_memory"]) and h + n > 1:
        h, n = (h, n - 1) if n else (h - 1, n)
    return h, n


def worst_case(found, h, n):
    """The server memory h heavy and n light helpers may take at most, in bytes."""
    return (h * memory_bytes(found["heavy_work_mem"]) + n * memory_bytes(found["work_mem"])) * 2


@contextmanager
def temp_written(database):
    """Print the temp file bytes the database wrote while the block ran (pg_stat_database: a
    backend reports them when its transaction ends, so the block's connections are closed)."""
    with storage.connect(database) as conn:
        before = conn.execute(TEMP).fetchone()[0]
    yield
    with storage.connect(database) as conn:
        after = conn.execute(TEMP).fetchone()[0]
    print(f"calculate: temp written {(after - before) // MB} MB", file=sys.stderr)


def memory_bytes(value):
    """A PostgreSQL memory setting with its unit ("256MB") in bytes; at least 64kB."""
    text = value if isinstance(value, str) else ""
    number, unit = text[:-2], MEMORY_UNITS.get(text[-2:])
    if not (number.isascii() and number.isdigit()) or unit is None or int(number) * unit < 2**16:
        raise ValueError(f"Invalid memory setting {value!r}: a whole number of kB, MB, GB or TB")
    return int(number) * unit


class _Lane:
    """One class of helpers: their connections, the keys they read next, in that order, and how
    many reads are running."""

    def __init__(self, keys, connections):
        self.pending, self.running = deque(keys), 0
        self.free = queue.SimpleQueue()
        for connection in connections:
            self.free.put(connection)
        self.threads = len(connections)
        self.pool = ThreadPoolExecutor(self.threads, thread_name_prefix="calc-read")


class Prefetcher:
    """Reads keys on helper connections ahead of their consumer (start(), take()).

    configure() reads the settings (settings()); start() plans the lanes (plan()), lowered to
    the connections the server has free (storage_parallel.capped()). Each lane runs at most its
    helpers' reads at once and holds at most 2 x parallel_reads results not yet taken. A context
    manager: leaving it cancels what is left and closes the helpers.
    """

    def __init__(self, conn):
        self.conn, self.settings, self.read = conn, None, None
        self.lanes, self.connections, self.futures = [], [], {}  # futures: key -> (lane, future)
        self.position, self.wanted, self.closing = {}, None, False
        self.lane_of, self.floor = {}, 0  # the keys not read yet; the keys before floor are skipped
        self.cond = threading.Condition()

    def __enter__(self):
        return self

    def __exit__(self, kind, error, trace):
        self.close()

    def configure(self, parallel_reads=None):
        """Read the settings in self.conn's transaction and SET LOCAL its work_mem (a SET takes
        no snapshot; the light helpers copy it, export())."""
        self.settings = settings(self.conn, parallel_reads)
        work_mem = sql.Literal(self.settings["work_mem"])
        self.conn.execute(sql.SQL("SET LOCAL work_mem TO {}").format(work_mem))

    def start(self, order, read, rows):
        """Read each key of order (the consumer's order) with read(connection, key) on the
        helpers, ahead of take(), in the snapshot of self.conn's transaction; rows: each key's
        estimated rows, which make it heavy (read largest first) or light (read in order)."""
        self.read, order = read, list(order)
        self.position = {key: n for n, key in enumerate(order)}
        limit = self.settings["heavy_rows"]
        heavy = sorted((k for k in order if rows[k] > limit), key=lambda k: -rows[k])
        light = [k for k in order if rows[k] <= limit]
        h, n = self._helpers(len(heavy), len(light))
        if not h + n:
            return
        exported = export(self.conn)
        heavy_export = (exported[0], self.settings["heavy_work_mem"], exported[2])
        with ThreadPoolExecutor(h + n) as opener:
            opening = [
                opener.submit(snapshot_connection, self.conn, heavy_export if i < h else exported)
                for i in range(h + n)
            ]
            wait(opening)
        self.connections = [f.result() for f in opening if f.exception() is None]
        for f in opening:
            if f.exception() is not None:
                raise f.exception()
        if h:
            self.lanes.append(_Lane(heavy if n else heavy + light, self.connections[:h]))
        if n:
            self.lanes.append(_Lane(light, self.connections[h:]))
        self.lane_of = {key: lane for lane in self.lanes for key in lane.pending}
        with self.cond:
            self._dispatch()

    def _helpers(self, heavy, light):
        """plan()'s helpers, lowered to the server's free connections, printed with the
        worst-case server memory."""
        found = self.settings
        h, n = plan(found, heavy, light)
        if not h + n:
            return 0, 0
        allowed = storage_parallel.capped(self.conn, h + n, "calculate")
        h, n = min(h, allowed), min(n, allowed - min(h, allowed))
        print(
            f"calculate: {h + n} parallel reads (of {found['parallel_reads']}): {h} heavy at "
            f"{found['heavy_work_mem']} (of {found['heavy_parallel']}) for {heavy} groups above "
            f"{found['heavy_rows']} rows, {n} light at {found['work_mem']} for {light} groups; "
            f"worst-case server memory {worst_case(found, h, n) // MB} MB "
            f"(max_parallel_memory {found['max_parallel_memory']})",
            file=sys.stderr,
        )
        return h, n

    def _dispatch(self):
        """Start the next reads of each lane (self.cond held): its wanted key first, then its
        next pending ones while its results not yet taken stay within 2 x parallel_reads."""
        if self.closing:
            return
        cap = 2 * self.settings["parallel_reads"]
        for lane in self.lanes:
            held = sum(1 for owner, _ in self.futures.values() if owner is lane)
            while lane.running < lane.threads:
                key = self.wanted if self.lane_of.get(self.wanted) is lane else None
                if key is None and held < cap:
                    key = self._next(lane)
                if key is None:
                    break
                del self.lane_of[key]
                lane.running, held = lane.running + 1, held + 1
                future = lane.pool.submit(self._run, lane, key)
                self.futures[key] = (lane, future)
                future.add_done_callback(lambda _, lane=lane: self._done(lane))
        self.cond.notify_all()

    def _next(self, lane):
        """The lane's next pending key not read yet (a wanted key may have gone first) and not
        skipped."""
        while lane.pending:
            key = lane.pending.popleft()
            if key in self.lane_of and self.position[key] >= self.floor:
                return key
        return None

    def _done(self, lane):
        with self.cond:
            lane.running -= 1
            self._dispatch()

    def _run(self, lane, key):
        connection = lane.free.get()
        try:
            return self.read(connection, key)
        finally:
            lane.free.put(connection)

    def take(self, key):
        """read()'s result for key: the helpers', waited for, else read on the calculation's own
        connection (a key not read ahead, or already taken). The keys before key in the order
        that were never taken are dropped (their groups were skipped): no read waits on them."""
        n = self.position.get(key)
        with self.cond:
            if self.lanes and n is not None:
                self._drop(n)
                if key in self.lane_of:
                    self.wanted = key
                    self._dispatch()
                    self.cond.wait_for(lambda: key in self.futures)
                    self.wanted = None
            entry = self.futures.pop(key, None)
            self._dispatch()
        return self.read(self.conn, key) if entry is None else entry[1].result()

    def _drop(self, n):
        self.floor = max(self.floor, n)
        for key in [k for k in self.futures if self.position[k] < n]:
            self.futures.pop(key)[1].cancel()

    def close(self):
        """Cancel the reads not taken and close the helpers."""
        with self.cond:
            self.closing = True
            for _, future in self.futures.values():
                future.cancel()
            self.futures = {}
            busy = any(lane.running for lane in self.lanes)
        if busy:  # their queries would only delay the end
            for connection in self.connections:
                connection.cancel_safe()
        for lane in self.lanes:
            lane.pool.shutdown(wait=True, cancel_futures=True)
        for connection in self.connections:
            connection.close()
        self.lanes = []
