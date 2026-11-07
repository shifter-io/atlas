#!/usr/bin/env python3
"""SCALE-GEN-1b: what the hourly cycle measures, and its report (README.md, "Measurements").

Statement timings come from pg_stat_statements when the database has it loaded, otherwise from
`Tracer` (every psycopg execute, executemany and COPY of the process, by statement shape;
`measure.py trace OUT MODULE [ARGS]` runs a module under it). Table sizes add each partition to
its parent. Peak RSS is VmHWM from /proc (Linux), sampled while a child runs, with the child's
maximum resident size from wait4.
"""

from __future__ import annotations

import contextlib
import functools
import heapq
import inspect
import json
import math
import os
import re
import runpy
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg

TOP = 20  # statements kept per step
TEXT = 300  # characters kept of a statement
# Only when its view is on the search path: in a scratch schema of production's database
# (target.py) it is not, and its counters would mix in production's statements.
EXTENSION = (
    "SELECT 1 FROM pg_extension WHERE extname='pg_stat_statements' "
    "AND to_regclass('pg_stat_statements') IS NOT NULL "
    "AND current_setting('shared_preload_libraries') LIKE '%pg_stat_statements%'"
)
STATEMENTS = (
    "SELECT queryid::text,left(query,%s),calls,total_exec_time FROM pg_stat_statements "
    "WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database())"
)
# pg_partition_tree gives no row for a table that is not partitioned: that table counts alone.
SIZES = (
    "SELECT c.relname,sum(pg_total_relation_size(coalesce(t.relid,c.oid)))::bigint "
    "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
    "LEFT JOIN LATERAL pg_partition_tree(c.oid) t ON true "
    "WHERE n.nspname=current_schema() AND c.relkind IN ('r','p') AND NOT c.relispartition "
    "GROUP BY 1 ORDER BY 2 DESC,1"
)


def has_statements(conn) -> bool:
    """pg_stat_statements is installed in the database and loaded by the server."""
    return conn.execute(EXTENSION).fetchone() is not None


def snapshot(conn) -> dict[str, tuple]:
    """pg_stat_statements counters of this database: queryid -> (text, calls, total ms)."""
    rows = conn.execute(STATEMENTS, (TEXT,)).fetchall()
    return {qid: (text, calls, total) for qid, text, calls, total in rows}


def delta(before: dict, after: dict) -> list[dict]:
    """The statements that ran between two snapshots, slowest first."""
    found = {}
    for qid, (text, calls, total) in after.items():
        _, calls_before, total_before = before.get(qid, (text, 0, 0.0))
        if calls > calls_before:
            found[qid] = [text, calls - calls_before, (total - total_before) / 1000, None]
    return ranked(found.values())


def ranked(rows) -> list[dict]:
    """[text, calls, seconds, max seconds] rows as the report's top statements."""
    rows = sorted(rows, key=lambda row: (-row[2], row[0]))[:TOP]
    return [
        {"statement": t, "calls": c, "seconds": round(s, 3)}
        | ({"max_seconds": round(m, 3)} if m is not None else {})
        for t, c, s, m in rows
    ]


def merge(lists) -> list[dict]:
    """Statement lists of several parts of a step, added up by statement text."""
    found = {}
    for statements in lists:
        for row in statements:
            entry = found.setdefault(row["statement"], [row["statement"], 0, 0.0, None])
            entry[1] += row["calls"]
            entry[2] += row["seconds"]
            if row.get("max_seconds") is not None:
                entry[3] = max(entry[3] or 0.0, row["max_seconds"])
    return ranked(found.values())


SHAPE = 200  # characters kept of a statement's shape
SLOWEST = 5  # statements kept per hour, slowest first
STEP = 1.1  # a shape's times in buckets of a factor 1.1: its p95 is within 5%
# A quoted literal (or one cut off at the end) and a number not inside a name or a $n placeholder.
LITERAL = re.compile(r"'(?:[^']|'')*'|'.*$|(?<![\w$])\d+(?:\.\d+)?(?:e[+-]?\d+)?\b", re.S | re.I)


@functools.lru_cache(maxsize=4096)
def shape(text: str) -> str:
    """A statement's text without its values: blanks collapsed, quoted literals and numbers ?."""
    text = re.sub(r"\s+", " ", text[: SHAPE * 10]).strip()
    return LITERAL.sub("?", text)[:SHAPE]


def bucket(seconds: float) -> str:
    return str(max(0, round(math.log(max(seconds, 1e-6) / 1e-6, STEP))))


@functools.lru_cache(maxsize=4096)
def upsert(text: str) -> bool:
    """An INSERT ... ON CONFLICT DO UPDATE: its rowcount includes the rows it updated."""
    return re.search(r"\bDO\s+UPDATE\b", text, re.I) is not None


def blank(text: str) -> dict:
    """A statement shape with nothing counted yet."""
    return {"statement": text, "calls": 0, "seconds": 0.0, "max_seconds": 0.0, "rows": 0} | {
        "histogram": {}
    }


class Tracer:
    """Wall time and rows of every psycopg execute, executemany and COPY in this process, by
    statement shape (README.md, "Measurements")."""

    def __init__(self):
        self.shapes: dict[str, dict] = {}
        self.slowest: list[tuple] = []  # a heap of (seconds, shape, rows)
        self.lock = threading.Lock()
        self.saved: list[tuple] = []

    def __enter__(self) -> Tracer:
        for cls in (psycopg.Cursor, psycopg.AsyncCursor):
            for name in ("execute", "executemany", "copy"):
                real = getattr(cls, name)
                self.saved.append((cls, name, real))
                wrapped = self.wrap_copy(real, cls) if name == "copy" else self.wrap(real)
                setattr(cls, name, wrapped)
        return self

    def __exit__(self, *exc) -> None:
        for cls, name, real in reversed(self.saved):
            setattr(cls, name, real)
        self.saved.clear()

    def wrap(self, real):
        if inspect.iscoroutinefunction(real):

            async def traced_async(cursor, query, *args, **kwargs):
                start, done = time.perf_counter(), False
                try:
                    result = await real(cursor, query, *args, **kwargs)
                    done = True
                    return result
                finally:
                    self.add(cursor, query, start, done)

            return traced_async

        def traced(cursor, query, *args, **kwargs):
            start, done = time.perf_counter(), False
            try:
                result = real(cursor, query, *args, **kwargs)
                done = True
                return result
            finally:
                self.add(cursor, query, start, done)

        return traced

    def wrap_copy(self, real, cls):
        """A COPY is timed from its start to the end of its block; its rows are those copied."""
        if cls is psycopg.AsyncCursor:

            @contextlib.asynccontextmanager
            async def traced_async(cursor, statement, *args, **kwargs):
                start, done = time.perf_counter(), False
                try:
                    async with real(cursor, statement, *args, **kwargs) as copy:
                        yield copy
                    done = True
                finally:
                    self.add(cursor, statement, start, done)

            return traced_async

        @contextlib.contextmanager
        def traced(cursor, statement, *args, **kwargs):
            start, done = time.perf_counter(), False
            try:
                with real(cursor, statement, *args, **kwargs) as copy:
                    yield copy
                done = True
            finally:
                self.add(cursor, statement, start, done)

        return traced

    def add(self, cursor, query, start: float, done: bool) -> None:
        seconds = time.perf_counter() - start
        rows = max(cursor.rowcount, 0) if done else 0
        raw = _text(cursor, query)
        text = shape(raw)
        with self.lock:
            entry = self.shapes.setdefault(text, blank(text) | {"upsert": False})
            entry["upsert"] = entry["upsert"] or upsert(raw)  # seen in full, past the shape
            entry["calls"] += 1
            entry["seconds"] += seconds
            entry["rows"] += rows
            entry["max_seconds"] = max(entry["max_seconds"], seconds)
            key = bucket(seconds)
            entry["histogram"][key] = entry["histogram"].get(key, 0) + 1
            heapq.heappush(self.slowest, (seconds, text, rows))
            if len(self.slowest) > SLOWEST:
                heapq.heappop(self.slowest)

    def summary(self) -> dict:
        """The statement count, the seconds, the 5 slowest statements and every shape (its calls,
        seconds, maximum, rows and its times' histogram: replay_report.combine adds them up)."""
        with self.lock:
            shapes = [row | {"histogram": dict(row["histogram"])} for row in self.shapes.values()]
            slowest = sorted(self.slowest, reverse=True)
        shapes.sort(key=lambda row: (-row["seconds"], row["statement"]))
        found = [{"statement": t, "seconds": round(s, 4), "rows": r} for s, t, r in slowest]
        total = round(sum(row["seconds"] for row in shapes), 4)
        count = sum(row["calls"] for row in shapes)
        return {"count": count, "seconds": total, "slowest": found, "shapes": shapes}

    def report(self) -> list[dict]:
        return top(self.summary())


def top(summary: dict) -> list[dict]:
    """A Tracer.summary as the cycle report's top statements."""
    rows = summary["shapes"]
    return ranked([[r["statement"], r["calls"], r["seconds"], r["max_seconds"]] for r in rows])


def _text(cursor, query) -> str:
    if isinstance(query, bytes):
        return query.decode(errors="replace")
    if isinstance(query, str):
        return query
    try:
        return query.as_string(cursor)
    except (psycopg.Error, AttributeError, TypeError, ValueError):
        return type(query).__name__


def sizes(conn) -> dict:
    """The database's size, and each table of the session's schema (with its partitions and
    indexes) and their sum, in bytes; in a scratch schema the database also holds production."""
    total = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    tables = dict(conn.execute(SIZES).fetchall())
    return {"database_bytes": total, "tables_bytes": sum(tables.values()), "tables": tables}


def vm_hwm(pid: int) -> int | None:
    """A process's peak resident set (kB) from /proc, or None (gone, or not Linux)."""
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return None
    found = re.search(r"^VmHWM:\s+(\d+)\s+kB", status, re.MULTILINE)
    return int(found.group(1)) if found else None


def run_child(command: list[str], poll: float = 0.5, timeout=None, stop=None) -> dict:
    """Run a command (its stdout discarded); its exit code, seconds and peak RSS in kB. Its process
    group is killed past `timeout` s or once the Event `stop` is set (`killed`: why)."""
    start, guarded = time.monotonic(), timeout is not None or stop is not None
    child = subprocess.Popen(command, stdout=subprocess.DEVNULL, start_new_session=guarded)
    peak, killed = 0, None
    while True:
        pid, status, usage = os.wait4(child.pid, os.WNOHANG)
        if pid:
            break
        if guarded and killed is None and (killed := _overdue(start, timeout, stop)):
            os.killpg(child.pid, signal.SIGKILL)
        peak = max(peak, vm_hwm(child.pid) or 0)
        time.sleep(poll)
    child.returncode = os.waitstatus_to_exitcode(status)
    result = {
        "exit": child.returncode,
        "seconds": round(time.monotonic() - start, 3),
        "peak_rss_kb": max(peak, usage.ru_maxrss),  # ru_maxrss is kB on Linux
    }
    return result | ({"killed": killed} if killed else {})


def _overdue(start: float, timeout, stop) -> str | None:
    if stop is not None and stop.is_set():
        return "stopped"
    if timeout is not None and time.monotonic() - start > timeout:
        return "timeout"
    return None


def percentiles(values) -> dict:
    """Nearest-rank p50, p95 and the maximum of a list of seconds."""
    values = sorted(values)
    if not values:
        return {"count": 0, "p50": None, "p95": None, "max": None}

    def rank(share):
        return round(values[max(0, -(-len(values) * share // 100) - 1)], 3)

    return {"count": len(values), "p50": rank(50), "p95": rank(95), "max": round(values[-1], 3)}


def _mb(kb) -> str:
    return "" if kb is None else f"{kb / 1024:.0f}"


COLUMNS = (  # the report's hour table: (header, cell of (record, steps))
    ("Hour (UTC)", lambda r, s: r["start"]),
    ("Daily tests", lambda r, s: s["ingest"]["tests"]),
    ("Ingest s", lambda r, s: f"{s['ingest']['seconds']:.0f}"),
    ("Pulses done/planned", lambda r, s: f"{s['pulses']['uploads']}/{s['pulses']['planned']}"),
    ("Upload p50/p95 s", lambda r, s: "{p50}/{p95}".format(**s["pulses"]["latency"])),
    ("Release s", lambda r, s: f"{s['release']['seconds']:.1f}"),
    ("Calculate s", lambda r, s: f"{s['calculate']['seconds']:.0f}"),
    ("Calculate RSS MB", lambda r, s: _mb(s["calculate"].get("peak_rss_kb"))),
    ("siteapi s", lambda r, s: "" if "skipped" in s["siteapi"] else s["siteapi"]["seconds"]),
    ("siteapi RSS MB", lambda r, s: _mb(s["siteapi"].get("peak_rss_kb"))),
    ("Database GB", lambda r, s: f"{r['sizes']['database_bytes'] / 2**30:.1f}"),
    ("Tables GB", lambda r, s: f"{r['sizes'].get('tables_bytes', 0) / 2**30:.1f}"),
)


def table(header, rows) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|---" * len(header) + "|"]
    return lines + ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]


def markdown(report: dict) -> str:
    """One row per hour, then the last hour's slowest statements per step and largest tables."""
    lines = ["# Scale cycle report (SCALE-GEN-1b)", ""]
    lines.append(
        f"Database `{report['database']}`, schema `{report.get('schema')}`, statement timings "
        f"from {report['statements_from']}, pulse rate {report['pulse_rate']} runs/s, "
        f"written {report['written_at']}."
    )
    rows = [[cell(r, r["steps"]) for _, cell in COLUMNS] for r in report["hours"]]
    lines += ["", *table([name for name, _ in COLUMNS], rows)]
    if report.get("current"):
        lines += ["", f"Hour {report['current']['index']} is in progress."]
    if report["hours"]:
        last = report["hours"][-1]
        for name, step in last["steps"].items():
            found = [
                (s["seconds"], s["calls"], f"`{s['statement'][:160]}`") for s in step["statements"]
            ]
            if found:
                lines += ["", f"## Slowest statements: {name} ({last['start']})", ""]
                lines += table(("Seconds", "Calls", "Statement"), found[:10])
        tables = list(last["sizes"]["tables"].items())[:15]
        lines += ["", "## Largest tables", ""]
        lines += table(("Table", "GB"), [(n, f"{size / 2**30:.2f}") for n, size in tables])
    return "\n".join(lines) + "\n"


def write_report(directory: str | Path, report: dict) -> Path:
    """scale-report.json and scale-report.md in `directory`; returns the JSON's path."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "scale-report.json"
    path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    (directory / "scale-report.md").write_text(markdown(report))
    return path


def trace(argv: list[str]) -> int:
    """`trace OUT MODULE [ARGS]`: run a module as __main__ under a Tracer; its summary to OUT."""
    out, module, *rest = argv
    code = 0
    with Tracer() as tracer:
        sys.argv = [module, *rest]
        try:
            runpy.run_module(module, run_name="__main__", alter_sys=True)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else int(exc.code is not None)
        finally:
            Path(out).write_text(json.dumps(tracer.summary()))
    return code


if __name__ == "__main__":
    if sys.argv[1:2] != ["trace"] or len(sys.argv) < 4:
        sys.exit("usage: measure.py trace OUT MODULE [ARGS...]")
    sys.exit(trace(sys.argv[2:]))
