"""CALC-LOCK-1: two calculations never run at once (calculator.calculate() holds hold()).

Two locks, held from the calculation's start to its end, neither waited for:
- the database's: the session advisory lock KEY, on a connection of its own. A second calculation
  of the same database (the hourly job, `atlas calculate` from a shell, a backfill point, a second
  orchestrator) finds it taken. A killed calculation's idle lock session ends with its process.
- the server's: an flock on the lock file, $ATLAS_CALCULATE_LOCK (empty: none), else
  ~/atlas/calculate.lock when the host layout's ~/atlas exists (campaign/host_layout.py). The
  production and staging calculations of one server read different databases: only the file is
  shared by both.
Either taken raises Busy; `atlas calculate` then exits BUSY_EXIT, which the orchestrator's
calculate job records as skipped (jobs.run_calculation).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path

from ..storage import connect

# Not rollups.LOCK: the build's own transactions wait for that one, and would wait for this one.
KEY = 817452908
FILE_ENV = "ATLAS_CALCULATE_LOCK"
BUSY_EXIT = 75  # EX_TEMPFAIL


class Busy(RuntimeError):
    """Another calculation holds the database's or the server's lock."""


def lock_file() -> Path | None:
    """The server's lock file, or None (no server-wide lock)."""
    named = os.environ.get(FILE_ENV)
    if named is not None:
        return Path(named) if named else None
    home = Path.home() / "atlas"
    return home / "calculate.lock" if home.is_dir() else None


@contextlib.contextmanager
def hold(database) -> Iterator[None]:
    """Both locks until the block ends; Busy, at once, while another calculation holds one."""
    path = lock_file()
    with _flock(path) if path else contextlib.nullcontext(), connect(database) as conn:
        conn.autocommit = True  # the session lock stays; no transaction stays open with it
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (KEY,)).fetchone()[0]:
            raise Busy("Another calculation of this database is running")
        yield


@contextlib.contextmanager
def _flock(path: Path) -> Iterator[None]:
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy(f"Another calculation on this server holds {path.name}") from None
        yield
