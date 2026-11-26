"""INGEST-PARALLEL-1: an hour's groups of daily tests stored over N database connections at once.

A group is what one `storage.ingest_many` transaction and then one `compute.estimate_runs`
transaction store (daily.batches builds them). Each runs on one of N connections, a worker thread
owning its connection for the whole store (psycopg releases the GIL while it waits on the
network). Imports hold the ingest lock shared, each network's counts go under that network's lock
(storage.NETWORK_LOCK) and only the cross-network overlap, their last statement, under the
overlap lock (storage.OVERLAP_LOCK): N groups of different networks never wait for each other.

INGEST-PROCESSES-1: the N connections are spread over P lanes, P worker processes (`spawn`) of
about N / P threads each, so the client's row preparation uses P cores (P = 1: N threads in this
process). A dispatching thread hands the groups out, never two of one network at once while a
group of another network can go instead, to the least busy lane; the caller sees each committed
group (`on_commit`) in its own thread. A Pool stays open over several stores (simulate.py hands
it the next hours' groups while an hour is stored, --store-ahead). A deadlock (40P01) or
serialization failure (40001) rolls back only its group, retried RETRIES times with backoff on
a new connection while the others go on; a group that still fails, or whose process died, gets
an IngestBatchError naming its run ids, for the caller's own retry. A worker process sends each
outcome back with its errors described (type and message): nothing it sends needs to pickle an
exception.

Bounds: N (default sized to the machine, atlas/sizing.py) is capped to the free connections
(sizing.capped), and so that N groups in flight fit the memory budget (a group is
never split: N goes down instead, and the groups in flight over all lanes never take more than
the budget); the N used is printed. connections=1 stores the groups one after the other on one
connection, with the statements of ingest_many and estimate_runs.

INGEST-HANG-1 (26 Nov 2026: a group's session waited hours on its own client, holding its
networks' locks, and every upload of those networks waited behind it): a group still running
DEADLINE seconds after it was handed out has its worker process killed, which ends its sessions
(their locks go) and fails that process's groups, named, while the other lanes go on; the lane
is not replaced. Each worker session also ends itself, on the server, when one transaction
passes SESSION's transaction_timeout or sits idle in one past its idle timeout: the bound of a
thread lane (P = 1), which cannot be killed.
"""

from __future__ import annotations

import multiprocessing
import queue
import signal
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from . import sizing, storage, timing
from .estimates import compute

RETRIES = 3
BACKOFF = 0.5  # seconds before the first retry, doubled for each next one
EXPANSION = 20  # bytes in memory per byte of a run directory's files while it is stored
TRANSIENT = (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure)
ERRORS = (storage.IngestBatchError, OSError, KeyError, ValueError, ZeroDivisionError)
ERRORS += (psycopg.Error,)
# The 1,523 groups of 19-26 Nov 2026 in production took 6.6 s (median) to 143 s (the longest
# that did not hang): far below both bounds.
DEADLINE = 900.0  # seconds
SESSION = ("transaction_timeout=10min", "idle_in_transaction_session_timeout=5min")


@dataclass
class Options:
    """How many connections at once over how many processes, the memory the groups in flight
    may take (bytes; None: sized to the machine), and an open Pool to store with, if any."""

    connections: int | None = None
    memory_budget: int | None = None
    processes: int | None = None
    pool: Pool | None = None


@dataclass
class Group:
    """One transaction's run directories, the estimated hours to store for those with a formula
    ([(run_id, country, formula)], compute.estimate_runs) and the networks of its runs."""

    directories: list
    estimates: list = field(default_factory=list)
    networks: frozenset = frozenset()

    def run_ids(self) -> list[str]:
        return [Path(d).name for d in self.directories]


@dataclass
class Outcome:
    """A group's ingest_many results, or its error after the retries; its estimates' error."""

    results: list = field(default_factory=list)
    error: storage.IngestBatchError | None = None
    estimate_error: Exception | None = None
    retries: int = 0
    seconds: float = 0.0


class WorkerError(Exception):
    """An error raised in a worker process, by its type and message."""


class Connections:
    """Each worker thread's own connection, opened when first needed; all closed at the end."""

    def __init__(self, credentials_file):
        self.credentials, self.local = credentials_file, threading.local()
        self.opened, self.lock = [], threading.Lock()

    def get(self):
        conn = getattr(self.local, "conn", None)
        if conn is None:
            conn = self.local.conn = storage.connect(self.credentials, *SESSION)
            with self.lock:
                self.opened.append(conn)
        return conn

    def drop(self) -> None:
        """Close this thread's connection after a failure: the next try opens a new one."""
        conn, self.local.conn = getattr(self.local, "conn", None), None
        if conn is not None:
            conn.close()

    def close(self) -> None:
        for conn in self.opened:
            conn.close()


def retried(work: Callable, pool: Connections, outcome: Outcome):
    """(work(conn), None) once it succeeds, else (None, its error): a deadlock or serialization
    failure is retried RETRIES times with backoff (each retry printed with its SQLSTATE), each
    time on a new connection. After any failure the connection is closed, which rolls its
    transaction back and frees its locks."""
    attempt = 0
    while True:
        try:
            return work(pool.get()), None
        except ERRORS as error:
            pool.drop()
            cause = error if isinstance(error, TRANSIENT) else error.__cause__
            if not isinstance(cause, TRANSIENT) or attempt == RETRIES:
                return None, error
        except BaseException:  # its transaction's locks go with the connection, never kept
            pool.drop()
            raise
        print(f"storage_parallel: retry {attempt + 1} ({cause.sqlstate})", file=sys.stderr)
        time.sleep(BACKOFF * 2**attempt)
        attempt += 1
        outcome.retries += 1


def _store_group(group: Group, pool: Connections) -> Outcome:
    """ingest_many, then estimate_runs, of one group on this thread's connection, timed."""
    outcome, began = Outcome(), time.monotonic()
    directories, credentials = group.directories, pool.credentials
    results, error = retried(
        lambda conn: storage.ingest_many(directories, credentials, conn=conn), pool, outcome
    )
    if error is not None:
        if not isinstance(error, storage.IngestBatchError):  # a directory's own check
            failed = storage.IngestBatchError(group.run_ids())
            failed.__cause__, error = error, failed
        outcome.error, outcome.seconds = error, time.monotonic() - began
        return outcome
    outcome.results = results
    if group.estimates:
        estimates = group.estimates
        _, outcome.estimate_error = retried(
            lambda conn: compute.estimate_runs(credentials, estimates, conn=conn), pool, outcome
        )
    outcome.seconds = time.monotonic() - began
    return outcome


def _described(error: BaseException | None) -> str | None:
    return None if error is None else f"{type(error).__name__}: {error}"


def _failed(run_ids: list[str], why: str) -> Outcome:
    error = storage.IngestBatchError(run_ids)
    error.__cause__ = WorkerError(why)
    return Outcome(error=error)


def _serve(conn, credentials_file, threads: int) -> None:
    """A worker process: store each (key, group) received over `threads` connections and send
    back (key, outcome, its errors described), until None. A stop (SIGINT) is the main
    process's: it hands out no more groups and this one finishes those it has."""
    if threading.current_thread() is threading.main_thread():  # (a test serves from a thread)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    pool, lock = Connections(credentials_file), threading.Lock()

    def send(key, group: Group, future) -> None:
        try:
            outcome = future.result()
        except Exception as error:  # reported as the group's failure, as a dead process's
            outcome = _failed(group.run_ids(), _described(error))
        error = outcome.error
        broke = (error.run_ids, _described(error.__cause__ or error)) if error else None
        errors = (broke, _described(outcome.estimate_error))
        outcome.error = outcome.estimate_error = None  # sent described: they need not pickle
        with lock:
            conn.send((key, outcome, errors))

    with ThreadPoolExecutor(threads) as executor:
        while (item := conn.recv()) is not None:
            key, group = item
            future = executor.submit(_store_group, group, pool)
            future.add_done_callback(lambda done, key=key, group=group: send(key, group, done))
    pool.close()


def _restored(outcome: Outcome, errors) -> Outcome:
    broke, estimate = errors
    if broke:
        outcome.error = _failed(*broke).error
    outcome.estimate_error = WorkerError(estimate) if estimate else None
    return outcome


class ThreadLane:
    """N worker threads of this process (P = 1): #764's thread pool."""

    def __init__(self, credentials_file, capacity: int, events: queue.Queue):
        self.connections = Connections(credentials_file)
        self.capacity, self.events, self.running, self.alive = capacity, events, set(), True
        self.executor = ThreadPoolExecutor(capacity)

    def send(self, key, group: Group) -> None:
        self.running.add(key)
        future = self.executor.submit(_store_group, group, self.connections)
        future.add_done_callback(lambda done: self.events.put(("done", key, done)))

    def close(self) -> None:
        self.executor.shutdown()
        self.connections.close()


class ProcessLane:
    """A worker process (_serve) of `capacity` threads; a thread here reads what it sends back."""

    def __init__(self, credentials_file, capacity: int, events: queue.Queue):
        context = multiprocessing.get_context("spawn")  # never a fork of a threaded process
        self.conn, theirs = context.Pipe()
        args = (theirs, credentials_file, capacity)
        self.process = context.Process(target=_serve, args=args, daemon=True)
        self.process.start()
        theirs.close()
        self.capacity, self.events, self.running, self.alive = capacity, events, set(), True
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            while True:
                key, outcome, errors = self.conn.recv()
                done = Future()
                done.set_result(_restored(outcome, errors))
                self.events.put(("done", key, done))
        except (EOFError, OSError):  # it exited, or died
            self.events.put(("dead", self))

    def send(self, key, group: Group) -> None:
        self.running.add(key)
        try:
            self.conn.send((key, group))
        except OSError:
            pass  # died, not yet known here: its "dead" event fails the group, named

    def close(self) -> None:
        try:
            self.conn.send(None)
        except OSError:
            pass  # already dead
        self.process.join()
        self.conn.close()


def _sized(name: str, value: int | None) -> int:
    """value, or the machine's default for ingest.<name> (sizing.derived) when None."""
    return sizing.derived(sizing.machine())[f"ingest.{name}"] if value is None else value


def _capped(credentials_file, wanted: int) -> int:
    with storage.connect(credentials_file) as conn:
        return sizing.capped(conn, wanted, "storage_parallel")


def _bytes(group: Group) -> int:
    found = (p.stat().st_size for d in group.directories for p in Path(d).iterdir() if p.is_file())
    return sum(found) * EXPANSION


def _fitting(groups: list[Group], connections: int, budget: int) -> int:
    """connections, or fewer: as many as the budget holds of the largest group in flight."""
    largest = max(_bytes(group) for group in groups)
    allowed = max(1, min(connections, budget // max(1, largest)))
    if allowed < connections:
        print(
            f"storage_parallel: warning: {allowed} connections, not {connections}: a group takes "
            f"about {largest / 2**20:.0f} MB of the {budget / 2**30:.1f} GB memory budget",
            file=sys.stderr,
        )
    return allowed


def _next(pending: list, parts: dict, running) -> object:
    """The first pending group of no network a running group (key) holds; else the first."""
    busy = frozenset().union(*(parts[k].networks for k in running))
    return next((k for k in pending if not parts[k].networks & busy), pending[0])


class Pool:
    """N connections over P lanes, open until closed; groups go in (submit, in order) and come
    out (wait) as each is stored. A group submitted again before it was waited for is the same."""

    def __init__(self, credentials_file, options: Options):
        wanted = _sized("connections", options.connections)
        self.connections = _capped(credentials_file, wanted) if wanted > 1 else 1
        lanes = min(_sized("processes", options.processes), self.connections)
        budget = _sized("memory_budget", options.memory_budget)
        self.events, self.budget, self.lock = queue.Queue(), budget, threading.Lock()
        if lanes == 1:
            self.lanes = [ThreadLane(credentials_file, self.connections, self.events)]
        else:
            n = self.connections
            shares = [n // lanes + (k < n % lanes) for k in range(lanes)]
            self.lanes = [ProcessLane(credentials_file, share, self.events) for share in shares]
        self.tickets, self.parts, self.sizes, self.pending, self.running = {}, {}, {}, [], {}
        self.loaded, self.closing, self.deadline, self.started = 0, False, DEADLINE, {}
        self.thread = threading.Thread(target=self._dispatch, daemon=True)
        self.thread.start()

    def __enter__(self) -> Pool:
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def submit(self, groups: list[Group]) -> list[Future]:
        """Each group's ticket (a Future of its Outcome); its group is handed out in turn."""
        found = []
        with self.lock:
            for group in groups:
                key = tuple(map(str, group.directories))
                if key not in self.tickets:
                    self.tickets[key] = Future()
                    self.events.put(("submit", key, group, _bytes(group)))
                found.append(self.tickets[key])
        return found

    def wait(self, groups: list[Group], tickets: list[Future], on_commit=None) -> list[Outcome]:
        """The groups' outcomes; on_commit(group, outcome) in this thread as each one commits.
        Each group's store time is a timing line."""
        found, order = {}, {ticket: k for k, ticket in enumerate(tickets)}
        for ticket in as_completed(tickets):
            k = order[ticket]
            found[k] = ticket.result()
            _timed(groups[k], found[k], f"{k + 1}/{len(tickets)}")
            if on_commit and found[k].error is None:
                on_commit(groups[k], found[k])
        with self.lock:
            for group in groups:
                self.tickets.pop(tuple(map(str, group.directories)), None)
        return [found[k] for k in range(len(tickets))]

    def close(self) -> None:
        """Hand out no more groups; wait for those in flight, then close the lanes."""
        self.closing = True
        self.events.put(("close",))
        self.thread.join()

    def _dispatch(self) -> None:
        try:
            while not self.closing or self.running:
                self._hand_out()
                try:
                    event = self.events.get(timeout=self._patience())
                except queue.Empty:
                    self._overdue()
                    continue
                self._handle(*event)
        finally:  # closed, or broken: no ticket is left unresolved
            for key in [*self.pending, *self.running]:
                if not self.tickets[key].cancel() and not self.tickets[key].done():
                    self.tickets[key].set_exception(WorkerError("the store stopped"))
            for lane in self.lanes:
                lane.close()

    def _handle(self, kind: str, *event) -> None:
        if kind == "submit":
            key, self.parts[key], self.sizes[key] = event
            self.pending.append(key)
        elif kind == "done":
            self._done(*event)
        elif kind == "dead":
            self._dead(event[0])

    def _hand_out(self) -> None:
        if not any(lane.alive for lane in self.lanes):
            for key in self.pending:
                why = "no worker process left"
                self.tickets[key].set_result(_failed(self.parts.pop(key).run_ids(), why))
                self.sizes.pop(key)
            self.pending.clear()
        while self.pending and not self.closing:
            free = [lane for lane in self.lanes if lane.alive and len(lane.running) < lane.capacity]
            if not free:
                return
            key = _next(self.pending, self.parts, self.running)
            if self.running and self.loaded + self.sizes[key] > self.budget:
                return  # a group is never split: it waits for room
            lane = min(free, key=lambda lane: len(lane.running) / lane.capacity)
            self.pending.remove(key)
            self.running[key], self.loaded = lane, self.loaded + self.sizes[key]
            self.started[key] = time.monotonic()
            lane.send(key, self.parts[key])

    def _patience(self) -> float | None:
        """Seconds until the first group in flight passes the deadline; None: none in flight."""
        if not self.started:
            return None
        return max(0.0, min(self.started.values()) + self.deadline - time.monotonic())

    def _overdue(self) -> None:
        """Each group past the deadline, named; its worker process killed (its "dead" event then
        fails its groups). A thread lane's group is left to its session's own timeouts."""
        now = time.monotonic()
        for key in [k for k, began in self.started.items() if now - began >= self.deadline]:
            del self.started[key]
            lane, names = self.running[key], self.parts[key].run_ids()
            print(
                f"storage_parallel: a group passed its deadline ({self.deadline:.0f} s): {names}",
                file=sys.stderr,
            )
            if isinstance(lane, ProcessLane):
                lane.process.kill()

    def _done(self, key, future: Future) -> None:
        self.started.pop(key, None)
        self.running.pop(key).running.discard(key)
        self.loaded -= self.sizes.pop(key)
        self.parts.pop(key)
        ticket, error = self.tickets[key], future.exception()
        if error is None:
            ticket.set_result(future.result())
        else:
            ticket.set_exception(error)

    def _dead(self, lane: ProcessLane) -> None:
        """A lane's process exited: its groups in flight fail, named; the others go on."""
        lane.alive = False
        lost = [key for key, owner in self.running.items() if owner is lane]
        if lost:
            names = [run for key in lost for run in self.parts[key].run_ids()]
            lane.process.join(10)  # its pipe closed as it died: reaped in a moment
            code = lane.process.exitcode
            print(f"storage_parallel: worker process died ({code}): {names}", file=sys.stderr)
        for key in lost:
            done = Future()
            done.set_result(_failed(self.parts[key].run_ids(), "its worker process died"))
            self._done(key, done)


def _timed(group: Group, outcome: Outcome, which: str) -> None:
    detail = {"runs": len(group.directories), "networks": ",".join(sorted(group.networks)) or "-"}
    detail |= {"retries": outcome.retries, "failed": int(outcome.error is not None)}
    timing.line("ingest", f"group {which}", outcome.seconds, **detail)


def store(
    groups: list[Group],
    credentials_file,
    *,
    connections: int | None = None,
    memory_budget: int | None = None,
    processes: int | None = None,
    pool: Pool | None = None,
    on_commit: Callable | None = None,
) -> list[Outcome]:
    """Store the groups (Group) over `connections` connections in `processes` processes (None:
    sized to the machine, sizing.derived), or with an open `pool`; each one's Outcome.

    on_commit(group, outcome) runs in this thread as each committed group finishes; a group
    running past DEADLINE seconds has its worker process killed (Pool). A stop
    (KeyboardInterrupt) hands out no more groups and waits for those in flight.
    """
    connections = _sized("connections", connections)
    memory_budget = _sized("memory_budget", memory_budget)
    if connections < 1:
        raise ValueError("at least one connection")
    if not groups:
        return []
    if pool is not None:
        return pool.wait(groups, pool.submit(groups), on_commit)
    connections = _fitting(groups, connections, memory_budget)
    with Pool(credentials_file, Options(connections, memory_budget, processes)) as own:
        lanes = len(own.lanes)
        print(
            f"storage_parallel: {len(groups)} groups over {own.connections} connections, "
            f"{lanes} process{'es' if lanes > 1 else ''}",
            file=sys.stderr,
        )
        return own.wait(groups, own.submit(groups), on_commit)
