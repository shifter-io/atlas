"""INGEST-PARALLEL-1: an hour's groups of daily tests stored over N database connections at once.

A group is what one `storage.ingest_many` transaction and then one `compute.estimate_runs`
transaction store (daily.batches builds them). Each runs on one of N connections, a worker thread
owning its connection for the whole store (psycopg releases the GIL while it waits on the
network). Imports hold the ingest lock shared, each network's counts go under that network's lock
(storage.NETWORK_LOCK) and only the cross-network overlap, their last statement, under the
overlap lock (storage.OVERLAP_LOCK): N groups of different networks never wait for each other.

The main thread hands the groups out, never two of one network at once while a group of another
network can go instead, and reports each committed group (`on_commit`) as it finishes. A deadlock
(40P01) or serialization failure (40001) rolls back only its group, retried RETRIES times with
backoff on a new connection while the others go on; a group that still fails gets an
IngestBatchError naming its run ids, once the others finished, for the caller's own retry.

Bounds: N (default 25, any number the server's free slots allow) is capped so that RESERVE
(10) connection slots stay free for production, and so that N groups in flight fit the memory
budget (a group is never split: N goes down instead); the N used is printed. connections=1
stores the groups one after the other on one connection, with the statements of ingest_many and
estimate_runs.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from . import storage
from .estimates import compute

CONNECTIONS = 25
MEMORY_BUDGET = 4 << 30  # bytes
RESERVE = 10  # connection slots always left free for production
RETRIES = 3
BACKOFF = 0.5  # seconds before the first retry, doubled for each next one
EXPANSION = 20  # bytes in memory per byte of a run directory's files while it is stored
TRANSIENT = (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure)
ERRORS = (storage.IngestBatchError, OSError, KeyError, ValueError, ZeroDivisionError)
ERRORS += (psycopg.Error,)
CAPACITY = (
    "SELECT current_setting('max_connections')::int,"
    "(SELECT sum(numbackends) FROM pg_stat_database)::int"
)


@dataclass
class Options:
    """How many connections at once, and the memory the groups in flight may take (bytes)."""

    connections: int = CONNECTIONS
    memory_budget: int = MEMORY_BUDGET


@dataclass
class Group:
    """One transaction's run directories, the estimated hours to store for those with a formula
    ([(run_id, country, formula)], compute.estimate_runs) and the networks of its runs."""

    directories: list
    estimates: list = field(default_factory=list)
    networks: frozenset = frozenset()


@dataclass
class Outcome:
    """A group's ingest_many results, or its error after the retries; its estimates' error."""

    results: list = field(default_factory=list)
    error: storage.IngestBatchError | None = None
    estimate_error: Exception | None = None
    retries: int = 0


class Connections:
    """Each worker thread's own connection, opened when first needed; all closed at the end."""

    def __init__(self, credentials_file):
        self.credentials, self.local = credentials_file, threading.local()
        self.opened, self.lock = [], threading.Lock()

    def get(self):
        conn = getattr(self.local, "conn", None)
        if conn is None:
            conn = self.local.conn = storage.connect(self.credentials)
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
    """ingest_many, then estimate_runs, of one group on this thread's connection."""
    outcome = Outcome()
    directories, credentials = group.directories, pool.credentials
    results, error = retried(
        lambda conn: storage.ingest_many(directories, credentials, conn=conn), pool, outcome
    )
    if error is not None:
        if not isinstance(error, storage.IngestBatchError):  # a directory's own check
            failed = storage.IngestBatchError([Path(d).name for d in directories])
            failed.__cause__, error = error, failed
        outcome.error = error
        return outcome
    outcome.results = results
    if group.estimates:
        estimates = group.estimates
        _, outcome.estimate_error = retried(
            lambda conn: compute.estimate_runs(credentials, estimates, conn=conn), pool, outcome
        )
    return outcome


def _capped(credentials_file, wanted: int) -> int:
    """wanted, or fewer: the connections the server has free beyond its reserve (at least 1)."""
    with storage.connect(credentials_file) as conn:
        limit, used = conn.execute(CAPACITY).fetchone()
    allowed = max(1, min(wanted, limit - used - RESERVE))
    if allowed < wanted:
        print(
            f"storage_parallel: warning: {allowed} connections, not {wanted}: max_connections "
            f"{limit}, {used} in use, {RESERVE} kept free",
            file=sys.stderr,
        )
    return allowed


def _bytes(directory) -> int:
    return sum(path.stat().st_size for path in Path(directory).iterdir() if path.is_file())


def _fitting(groups: list[Group], connections: int, budget: int) -> int:
    """connections, or fewer: as many as the budget holds of the largest group in flight."""
    largest = max(sum(_bytes(d) for d in group.directories) for group in groups) * EXPANSION
    allowed = max(1, min(connections, budget // max(1, largest)))
    if allowed < connections:
        print(
            f"storage_parallel: warning: {allowed} connections, not {connections}: a group takes "
            f"about {largest / 2**20:.0f} MB of the {budget / 2**30:.1f} GB memory budget",
            file=sys.stderr,
        )
    return allowed


def _next(pending: list[int], parts: list[Group], running: dict) -> int:
    """The first pending group of no network a running group holds; else the first pending."""
    busy = frozenset().union(*(parts[k].networks for k in running.values()))
    return next((k for k in pending if not parts[k].networks & busy), pending[0])


def _run(executor, pool, parts: list[Group], connections: int, on_commit) -> list[Outcome]:
    """Hand the groups (parts) out, at most `connections` in flight; each one's Outcome."""
    found: list[Outcome] = [Outcome()] * len(parts)
    pending, running = list(range(len(parts))), {}
    while pending or running:
        while pending and len(running) < connections:
            k = _next(pending, parts, running)
            pending.remove(k)
            running[executor.submit(_store_group, parts[k], pool)] = k
        done, _ = wait(running, return_when=FIRST_COMPLETED)
        for future in done:
            k = running.pop(future)
            found[k] = future.result()
            if on_commit and found[k].error is None:
                on_commit(parts[k], found[k])
    return found


def store(
    groups: list[Group],
    credentials_file,
    *,
    connections: int = CONNECTIONS,
    memory_budget: int = MEMORY_BUDGET,
    on_commit: Callable | None = None,
) -> list[Outcome]:
    """Store the groups (Group) over `connections` connections at once; each one's Outcome.

    on_commit(group, outcome) runs in this thread as each committed group finishes. A stop
    (KeyboardInterrupt) hands out no more groups and waits for those in flight.
    """
    if connections < 1:
        raise ValueError("at least one connection")
    if not groups:
        return []
    connections = _fitting(groups, connections, memory_budget)
    connections = _capped(credentials_file, connections) if connections > 1 else 1
    print(f"storage_parallel: {len(groups)} groups over {connections} connections", file=sys.stderr)
    pool = Connections(credentials_file)
    try:
        with ThreadPoolExecutor(min(connections, len(groups))) as executor:
            return _run(executor, pool, groups, connections, on_commit)
    finally:
        pool.close()
