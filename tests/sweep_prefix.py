"""The shared prefixes of the hourly sweeps (CI-90-1): test_calculated_incremental's and
test_calc_fleet's.

A sweep checks every hourly cutoff of a day, and each check starts from what the day's earlier
incremental builds left (lists and memos). A prefix is that state after the day's first `start`
cutoffs: the sweep's history, its unasserted incremental builds (`replay`) and the late run where
the sweep adds it. Each prefix is a template database in the test PostgreSQL, built once per run
by the first worker that needs it, under an advisory lock on the admin connection, from the
prefix before it in its chain (the history first), so every prefix of a day comes from the same
chain of builds. A case copies its start's (`prefix`) instead of replaying. The days of a sweep
share its history. The process that runs the session drops them at its end (tests/conftest.py).

A prefix records the history's random state and ids (a comment on the history's template), so a
copy adds the late run as the history would have: the replays use no randomness.
"""

import json
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import psycopg
from helpers import drop_databases, scratch_database, shared_template, untraced
from psycopg import sql
from storage_cases import admin_credentials, credentials_file
from test_calculated_hourly_reads import HOUR, History

from atlas.calculated.calculator import calculate


@dataclass(frozen=True)
class Chain:
    """One day of a sweep: its history (async (database, tmp_path, end) -> History), its cutoffs
    (hours before the end, oldest first), its prefixes' starts (indexes into hours, 0 first: the
    history alone) and its late run (the cutoff it is added before, and an async function that
    adds it to a History)."""

    sweep: str
    day: int
    history: Callable
    hours: Sequence[int]
    starts: Sequence[int]
    late: tuple[int, Callable]

    def checked(self, start):
        """The cutoffs the case from start checks: up to the next prefix's start."""
        later = [s for s in self.starts if s > start]
        return self.hours[start : later[0] if later else len(self.hours)]


async def replay(database, policy, end, hours, late):
    """A sweep's incremental builds at its cutoffs hours (before end), unasserted, adding its late
    run (late: the cutoff and the coroutine function that adds it) where the sweep does. They
    leave the lists and memos the sweep's next build reads: the sweep's full builds add none an
    incremental build reads, so the next one has the same hits, misses and statements."""
    at, add = late
    for cutoff in hours:
        if cutoff == at:
            await add()
        calculate(database, policy, as_of=end - cutoff * HOUR, mode="incremental")


def restored(database, tmp_path, end, state):
    """The History of a copy of a prefix, as the history left it (`state`, from `_state`)."""
    h = History.__new__(History)
    h.database, h.tmp_path, h.end = database, tmp_path, end
    h.rng = random.Random()
    version, internal, gauss = state["rng"]
    h.rng.setstate((version, tuple(internal), gauss))
    h.releases, h.ips = state["releases"], state["ips"]
    return h


def _state(h):
    return json.dumps({"rng": h.rng.getstate(), "releases": h.releases, "ips": h.ips})


def name(chain, end, start=None):
    """The template of the chain's prefix at start, or its sweep's history (start None)."""
    stamp = f"{end:%Y%m%d%H}"
    if start is None:
        return shared_template(chain.sweep, "h", stamp)
    return shared_template(chain.sweep, str(chain.day), str(start), stamp)


def _exists(conn, database):
    query = "SELECT 1 FROM pg_database WHERE datname=%s"
    return conn.execute(query, (database,)).fetchone() is not None


async def _template(admin, target, source, directory, fill):
    """The template target, unless this run has it: a copy of source, filled by
    `await fill(credentials)` under a scratch name and renamed once complete, all under target's
    advisory lock. fill's result, a string, becomes the template's comment."""
    with psycopg.connect(**admin, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (target,))
        if _exists(conn, target):
            return
        scratch = scratch_database(admin, template=source)
        try:
            comment = await fill(credentials_file(admin, scratch, directory))
            rename = sql.SQL("ALTER DATABASE {} RENAME TO {}")
            conn.execute(rename.format(sql.Identifier(scratch), sql.Identifier(target)))
        except BaseException:
            drop_databases(admin, [scratch])
            raise
        if comment is not None:
            note = sql.SQL("COMMENT ON DATABASE {} IS {}")
            conn.execute(note.format(sql.Identifier(target), sql.Literal(comment)))


def _comment(admin, database):
    query = "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s"
    with psycopg.connect(**admin, autocommit=True) as conn:
        return conn.execute(query, (database,)).fetchone()[0]


async def history_state(chain, end, base, tmp_path):
    """The sweep's history template (built from base, the worker's storage template, if this
    run has none) and the state it records."""
    admin, target = admin_credentials(), name(chain, end)

    async def fill(database):
        return _state(await chain.history(database, tmp_path, end))

    await _template(admin, target, base, tmp_path, fill)
    return json.loads(_comment(admin, target))


async def prefix(chain, start, end, base, tmp_path, policy, databases):
    """A fresh copy of the chain's prefix at start, its credentials and its History: each
    prefix up to start is built first, each from the one before it, if this run has none."""
    admin = admin_credentials()
    state = await history_state(chain, end, base, tmp_path)
    late_at, add_late = chain.late
    for before, at in zip(chain.starts, chain.starts[1:], strict=False):
        if at > start:
            break

        async def fill(database, before=before, at=at):
            h = restored(database, tmp_path, end, state)
            with untraced():  # the cases' own builds cover these lines
                await replay(
                    database, policy, end, chain.hours[before:at], (late_at, lambda: add_late(h))
                )

        source = name(chain, end, before or None)
        await _template(admin, name(chain, end, at), source, tmp_path, fill)
    database = databases(template=name(chain, end, start or None))
    return database, restored(database, tmp_path, end, state)
