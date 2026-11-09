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
an IngestBatchError naming its run ids, for the caller's own retry.

Bounds: N (default 25) is capped so that a quarter of max_connections (at least RESERVE, 20)
stays free for production, and so that N groups in flight fit the memory budget (a group is
never split: N goes down instead, and the groups in flight over all lanes never take more than
the budget); the N used is printed. connections=1 stores the groups one after the other on one
connection, with the statements of ingest_many and estimate_runs.
"""

from __future__ import annotations

import math
import multiprocessing
import os
import queue
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from . import storage
from .storage_worker import Connections, Group, Outcome, failure, restored, serve, store_group

CONNECTIONS = 25
PROCESSES = 7  # at most, and one core fewer than the machine's
MEMORY_BUDGET = 4 << 30  # bytes
RESERVE, RESERVE_SHARE = 20, 0.25  # of max_connections, always left free for production
EXPANSION = 20  # bytes in memory per byte of a run directory's files while it is stored
CAPACITY = (
    "SELECT current_setting('max_connections')::int,"
    "(SELECT sum(numbackends) FROM pg_stat_database)::int"
)


@dataclass
class Options:
    """How many connections at once over how many processes (None: default_processes), the
    memory the groups in flight may take (bytes), and an open Pool to store with, if any."""

    connections: int = CONNECTIONS
    memory_budget: int = MEMORY_BUDGET
    processes: int | None = None
    pool: Pool | None = None


class ThreadLane:
    """N worker threads of this process (P = 1): #764's thread pool."""

    def __init__(self, credentials_file, capacity: int, events: queue.Queue):
        self.connections = Connections(credentials_file)
        self.capacity, self.events, self.running, self.alive = capacity, events, set(), True
        self.executor = ThreadPoolExecutor(capacity)

    def send(self, key, group: Group) -> None:
        self.running.add(key)
        future = self.executor.submit(store_group, group, self.connections)
        future.add_done_callback(lambda done: self.events.put(("done", key, done)))

    def close(self) -> None:
        self.executor.shutdown()
        self.connections.close()


class ProcessLane:
    """A worker process (serve) of `capacity` threads; a thread here reads what it sends back."""

    def __init__(self, credentials_file, capacity: int, events: queue.Queue):
        context = multiprocessing.get_context("spawn")  # never a fork of a threaded process
        self.conn, theirs = context.Pipe()
        args = (theirs, credentials_file, capacity)
        self.process = context.Process(target=serve, args=args, daemon=True)
        self.process.start()
        theirs.close()
        self.capacity, self.events, self.running, self.alive = capacity, events, set(), True
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            while True:
                key, outcome, errors = self.conn.recv()
                done = Future()
                done.set_result(restored(outcome, errors))
                self.events.put(("done", key, done))
        except (EOFError, OSError):  # it exited, or died
            self.events.put(("dead", self))

    def send(self, key, group: Group) -> None:
        self.running.add(key)
        self.conn.send((key, group))

    def close(self) -> None:
        try:
            self.conn.send(None)
        except OSError:
            pass  # already dead
        self.process.join()
        self.conn.close()


def default_processes(connections: int) -> int:
    return max(1, min(PROCESSES, (os.cpu_count() or 2) - 1, connections))


def _capped(credentials_file, wanted: int) -> int:
    """wanted, or fewer: the connections the server has free beyond its reserve (at least 1)."""
    with storage.connect(credentials_file) as conn:
        limit, used = conn.execute(CAPACITY).fetchone()
    reserve = max(RESERVE, math.ceil(limit * RESERVE_SHARE))
    allowed = max(1, min(wanted, limit - used - reserve))
    if allowed < wanted:
        print(
            f"storage_parallel: warning: {allowed} connections, not {wanted}: max_connections "
            f"{limit}, {used} in use, {reserve} kept free",
            file=sys.stderr,
        )
    return allowed


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
        wanted = options.connections
        self.connections = _capped(credentials_file, wanted) if wanted > 1 else 1
        lanes = min(options.processes or default_processes(self.connections), self.connections)
        self.events, self.budget, self.lock = queue.Queue(), options.memory_budget, threading.Lock()
        if lanes == 1:
            self.lanes = [ThreadLane(credentials_file, self.connections, self.events)]
        else:
            n = self.connections
            shares = [n // lanes + (k < n % lanes) for k in range(lanes)]
            self.lanes = [ProcessLane(credentials_file, share, self.events) for share in shares]
        self.tickets, self.parts, self.sizes, self.pending, self.running = {}, {}, {}, [], {}
        self.loaded, self.closing = 0, False
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
        """The groups' outcomes; on_commit(group, outcome) in this thread as each one commits."""
        found, order = {}, {ticket: k for k, ticket in enumerate(tickets)}
        for ticket in as_completed(tickets):
            k = order[ticket]
            found[k] = ticket.result()
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
                kind, *event = self.events.get()
                if kind == "submit":
                    key, self.parts[key], self.sizes[key] = event
                    self.pending.append(key)
                elif kind == "done":
                    self._done(*event)
                elif kind == "dead":
                    self._dead(event[0])
        finally:  # closed, or broken: no ticket is left unresolved
            for key in [*self.pending, *self.running]:
                if not self.tickets[key].cancel() and not self.tickets[key].done():
                    self.tickets[key].set_exception(WorkerError("the store stopped"))
            for lane in self.lanes:
                lane.close()

    def _hand_out(self) -> None:
        if not any(lane.alive for lane in self.lanes):
            for key in self.pending:
                why = "no worker process left"
                self.tickets[key].set_result(failure(self.parts.pop(key).run_ids(), why))
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
            lane.send(key, self.parts[key])

    def _done(self, key, future: Future) -> None:
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
            done.set_result(failure(self.parts[key].run_ids(), "its worker process died"))
            self._done(key, done)


def store(
    groups: list[Group],
    credentials_file,
    *,
    connections: int = CONNECTIONS,
    memory_budget: int = MEMORY_BUDGET,
    processes: int | None = None,
    pool: Pool | None = None,
    on_commit: Callable | None = None,
) -> list[Outcome]:
    """Store the groups (Group) over `connections` connections in `processes` processes (default
    default_processes), or with an open `pool`; each one's Outcome.

    on_commit(group, outcome) runs in this thread as each committed group finishes. A stop
    (KeyboardInterrupt) hands out no more groups and waits for those in flight.
    """
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
