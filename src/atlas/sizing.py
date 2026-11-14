"""INGEST-SCALE-1: the hourly ingest's and the calculation's limits, sized to the machine they run
on (README "Hourly limits and timings"). Each is a setting (orchestrator/db.py DEFAULTS); one left
at None takes derived()'s default from the machine's CPUs and available memory, which on a small
machine (up to 31 CPUs and 32 GiB) are the fixed values used before (SMALL), and on the manager
server a quarter of its CPUs or an eighth of its memory each, so the APIs, Caddy, the orchestrator
and the other environment's jobs keep the rest. capped() lowers every set of connections opened at
once to what the server, and the login's role when it has a connection limit, have free beyond
their reserves (the database.* settings).
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from psycopg.rows import tuple_row

from .orchestrator.control import read_setting

GIB = 1 << 30
# The values used on every machine before INGEST-SCALE-1: the least derived() gives.
SMALL = {"processes": 7, "connections": 25, "memory": 4 * GIB, "reads": 8, "heavy": 2}
SMALL_SCOPE = 8 * GIB
INGEST = ("batch", "connections", "processes", "memory_budget")
CAPACITY = (
    "SELECT current_setting('max_connections')::int,"
    "(SELECT sum(numbackends) FROM pg_stat_database)::int,"
    "(SELECT rolconnlimit FROM pg_roles WHERE rolname=current_user),"
    "(SELECT count(*) FROM pg_stat_activity WHERE usename=current_user)::int"
)


@dataclass(frozen=True)
class Machine:
    """The CPUs and the available memory (bytes; 0 when unknown) of the machine."""

    cpus: int
    memory: int


def machine() -> Machine:
    return Machine(os.cpu_count() or 2, _available())


def _available() -> int:
    """MemAvailable (Linux), else the physical memory, else 0."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 0


def derived(found: Machine) -> dict:
    """The default of each machine-sized setting on the machine found."""
    processes = max(1, min(SMALL["processes"], found.cpus - 1), found.cpus // 4)
    reads = max(SMALL["reads"], found.cpus // 4)
    return {
        "ingest.processes": processes,
        "ingest.connections": max(SMALL["connections"], 3 * processes),
        "ingest.memory_budget": max(SMALL["memory"], found.memory // 8),
        "calculate.parallel_reads": reads,
        "calculate.heavy_parallel": max(SMALL["heavy"], reads // 4),
    }


def most_parallel_reads(found: Machine) -> int:
    return max(16, found.cpus // 2)


def limit(conn, key: str):
    """The setting key from conn's settings table, or derived() on this machine when None."""
    value = read_setting(conn, key)
    return derived(machine())[key] if value is None else value


def scope_memory(value: str | None) -> str | None:
    """The calculation scope's MemoryMax: ATLAS_CALCULATE_MEMORY_MAX as given, or with "auto"
    max(8G, an eighth of the available memory), in whole G."""
    if value != "auto":
        return value
    return f"{max(SMALL_SCOPE, machine().memory // 8) // GIB}G"


async def ingest_sizes(db, given: dict) -> dict:
    """The ingest's batch, connections, processes and memory_budget: those given (`daily ingest
    --batch` ...), else the ingest.* settings, else derived(); each a whole number of at least 1,
    printed."""
    from .orchestrator.db import setting

    async with db.connection() as conn:
        found = {n: given[n] if n in given else await setting(conn, f"ingest.{n}") for n in INGEST}
    here = machine()
    sizes = {n: derived(here)[f"ingest.{n}"] if v is None else v for n, v in found.items()}
    for name, value in sizes.items():
        if type(value) is not int or value < 1:
            raise ValueError(f"ingest.{name} must be a whole number of at least 1")
    print(
        f"ingest: batch {sizes['batch']}, {sizes['connections']} connections, "
        f"{sizes['processes']} processes, memory budget {sizes['memory_budget'] / GIB:.1f} GiB "
        f"(machine: {here.cpus} CPUs, {here.memory / GIB:.1f} GiB available)",
        file=sys.stderr,
    )
    return sizes


def reserves(conn) -> tuple[int, float, int]:
    """database.reserve, database.reserve_share and database.role_reserve."""
    names = ("reserve", "reserve_share", "role_reserve")
    return tuple(read_setting(conn, f"database.{name}") for name in names)


def capped(conn, wanted: int, name: str) -> int:
    """wanted, or fewer: the connections the server has free beyond its reserve and the login's
    role beyond its role_reserve (at least 1), with a warning from name when fewer."""
    cursor = conn.cursor(row_factory=tuple_row)
    most, used, role_limit, role_used = cursor.execute(CAPACITY).fetchone()
    reserve, share, role_reserve = reserves(conn)
    kept = max(reserve, math.ceil(most * share))
    free = [most - used - kept]
    if role_limit is not None and role_limit >= 0:  # -1: the role has no limit of its own
        free.append(role_limit - role_used - role_reserve)
    allowed = max(1, min(wanted, *free))
    if allowed < wanted:
        role = f"; role limit {role_limit}, {role_used} in use, {role_reserve} kept free"
        print(
            f"{name}: warning: {allowed} connections, not {wanted}: max_connections "
            f"{most}, {used} in use, {kept} kept free{role if len(free) > 1 else ''}",
            file=sys.stderr,
        )
    return allowed
