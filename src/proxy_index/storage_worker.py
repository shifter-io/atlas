"""INGEST-PROCESSES-1: one group's store (storage_parallel), in a worker thread of this process or
of a worker process (serve).

A group is one `storage.ingest_many` transaction and then one `compute.estimate_runs`
transaction, on the thread's own connection. A deadlock (40P01) or serialization failure (40001)
rolls back only its group, retried RETRIES times with backoff on a new connection. A worker
process sends each outcome back with its errors described (their type and message), so nothing
it sends needs its exceptions to pickle.
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from . import storage
from .estimates import compute

RETRIES = 3
BACKOFF = 0.5  # seconds before the first retry, doubled for each next one
TRANSIENT = (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure)
ERRORS = (storage.IngestBatchError, OSError, KeyError, ValueError, ZeroDivisionError)
ERRORS += (psycopg.Error,)


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


def store_group(group: Group, pool: Connections) -> Outcome:
    """ingest_many, then estimate_runs, of one group on this thread's connection."""
    outcome = Outcome()
    directories, credentials = group.directories, pool.credentials
    results, error = retried(
        lambda conn: storage.ingest_many(directories, credentials, conn=conn), pool, outcome
    )
    if error is not None:
        if not isinstance(error, storage.IngestBatchError):  # a directory's own check
            failed = storage.IngestBatchError(group.run_ids())
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


def _described(error: BaseException | None) -> str | None:
    return None if error is None else f"{type(error).__name__}: {error}"


def failure(run_ids: list[str], why: str) -> Outcome:
    error = storage.IngestBatchError(run_ids)
    error.__cause__ = WorkerError(why)
    return Outcome(error=error)


def serve(conn, credentials_file, threads: int) -> None:
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
            outcome = failure(group.run_ids(), _described(error))
        error = outcome.error
        broke = (error.run_ids, _described(error.__cause__ or error)) if error else None
        errors = (broke, _described(outcome.estimate_error))
        outcome.error = outcome.estimate_error = None  # sent described: they need not pickle
        with lock:
            conn.send((key, outcome, errors))

    with ThreadPoolExecutor(threads) as executor:
        while (item := conn.recv()) is not None:
            key, group = item
            future = executor.submit(store_group, group, pool)
            future.add_done_callback(lambda done, key=key, group=group: send(key, group, done))
    pool.close()


def restored(outcome: Outcome, errors) -> Outcome:
    broke, estimate = errors
    if broke:
        outcome.error = failure(*broke).error
    outcome.estimate_error = WorkerError(estimate) if estimate else None
    return outcome
