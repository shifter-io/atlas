"""One dropper process per test process, which drops its scratch databases (CI-90-6).

`queue` (helpers.drop_databases) hands the names to the dropper on its stdin and returns, so a
test's teardown never waits for its drops; they run while the next tests do, and PostgreSQL
still frees a finished test's pages soon (a dropped database's dirty pages are never written).
`wait`, at the session end (tests/conftest.py), closes its stdin and waits for the rest.
A process of its own, not a thread: a test may patch psycopg, time or subprocess while a drop
runs, and the drops must never go through its fakes. Run as a script it drops what it reads,
one JSON line {"admin": credentials, "names": [...]} at a time, with `drop`; it reports each
failed drop on stderr and exits 1 after the rest.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path

import psycopg
from psycopg import sql

# Bound on import: a test may patch subprocess.Popen while a teardown queues its drops.
_POPEN = subprocess.Popen
PROCESS: subprocess.Popen | None = None


class DropError(RuntimeError):
    """The dropper process failed to drop a queued database."""


def queue(admin: dict, names: list[str]) -> None:
    global PROCESS
    if PROCESS is None:
        PROCESS = _POPEN(
            [sys.executable, str(Path(__file__).resolve())],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    PROCESS.stdin.write(json.dumps({"admin": admin, "names": list(names)}) + "\n")
    PROCESS.stdin.flush()


def wait() -> None:
    """Wait for every queued drop; raise DropError when one failed."""
    global PROCESS
    if PROCESS is None:
        return
    process, PROCESS = PROCESS, None
    process.stdin.close()
    errors = process.stderr.read()
    if process.wait() != 0:
        raise DropError(f"the dropper failed (exit {process.returncode}): {errors.strip()}")


def serve(lines: Iterable[str], drop: Callable[[dict, list[str]], None]) -> int:
    """Drop each line's databases; 1 when any drop failed (the first 20 reported), else 0."""
    failed = 0
    for line in lines:
        job = json.loads(line)
        try:
            drop(job["admin"], job["names"])
        except psycopg.Error as error:
            failed += 1
            if failed <= 20:
                print(f"dropping {job['names']}: {error}", file=sys.stderr, flush=True)
    return int(failed > 0)


def drop(admin: dict, names: list[str]) -> None:
    """Drop `names` as `admin`; in the test timeline, the cluster's database count first."""
    import helpers  # in the dropper process; helpers imports this module

    with psycopg.connect(**admin, autocommit=True) as conn:
        if helpers.TIMELINE:
            count = conn.execute("SELECT count(*) FROM pg_database").fetchone()[0]
            helpers.timeline_record({"kind": "databases", "count": count, "start": time.time()})
        with helpers.timed("drop_database", databases=len(names)):
            for name in names:
                statement = sql.SQL("DROP DATABASE {} WITH (FORCE)")
                conn.execute(statement.format(sql.Identifier(name)))


if __name__ == "__main__":
    raise SystemExit(serve(sys.stdin, drop))
