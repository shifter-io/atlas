#!/usr/bin/env python3
"""SCALE-GEN-1b: what the hourly cycle measures, and its report (README.md, "Measurements").

Statement timings come from pg_stat_statements when the database has it loaded, otherwise from
`Tracer` (every psycopg execute of the process; `measure.py trace OUT MODULE [ARGS]` runs a module
under it). Table sizes add each partition to its parent. Peak RSS is VmHWM from /proc (Linux),
sampled while a child runs, with the child's maximum resident size from wait4.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import runpy
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg

TOP = 20  # statements kept per step
TEXT = 300  # characters kept of a statement
EXTENSION = (
    "SELECT 1 FROM pg_extension WHERE extname='pg_stat_statements' "
    "AND current_setting('shared_preload_libraries') LIKE '%pg_stat_statements%'"
)
STATEMENTS = (
    "SELECT queryid::text,left(query,%s),calls,total_exec_time FROM pg_stat_statements "
    "WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database())"
)
SIZES = (
    "SELECT c.relname,sum(pg_total_relation_size(t.relid))::bigint FROM pg_class c "
    "JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL pg_partition_tree(c.oid) t "
    "WHERE n.nspname='public' AND c.relkind IN ('r','p') AND NOT c.relispartition "
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


class Tracer:
    """Per-statement time of every psycopg execute and executemany in this process."""

    def __init__(self):
        self.found: dict[str, list] = {}
        self.lock = threading.Lock()
        self.saved: list[tuple] = []

    def __enter__(self) -> Tracer:
        for cls in (psycopg.Cursor, psycopg.AsyncCursor):
            for name in ("execute", "executemany"):
                real = getattr(cls, name)
                self.saved.append((cls, name, real))
                setattr(cls, name, self.wrap(real))
        return self

    def __exit__(self, *exc) -> None:
        for cls, name, real in reversed(self.saved):
            setattr(cls, name, real)
        self.saved.clear()

    def wrap(self, real):
        if inspect.iscoroutinefunction(real):

            async def traced_async(cursor, query, *args, **kwargs):
                start = time.perf_counter()
                try:
                    return await real(cursor, query, *args, **kwargs)
                finally:
                    self.add(query, time.perf_counter() - start)

            return traced_async

        def traced(cursor, query, *args, **kwargs):
            start = time.perf_counter()
            try:
                return real(cursor, query, *args, **kwargs)
            finally:
                self.add(query, time.perf_counter() - start)

        return traced

    def add(self, query, seconds: float) -> None:
        text = query if isinstance(query, str) else repr(query)
        text = re.sub(r"\s+", " ", text).strip()[:TEXT]
        with self.lock:
            entry = self.found.setdefault(text, [text, 0, 0.0, 0.0])
            entry[1] += 1
            entry[2] += seconds
            entry[3] = max(entry[3], seconds)

    def report(self) -> list[dict]:
        with self.lock:
            return ranked([list(row) for row in self.found.values()])


def sizes(conn) -> dict:
    """The database's size and each table's (with its partitions and indexes), in bytes."""
    total = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    return {"database_bytes": total, "tables": dict(conn.execute(SIZES).fetchall())}


def vm_hwm(pid: int) -> int | None:
    """A process's peak resident set (kB) from /proc, or None (gone, or not Linux)."""
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return None
    found = re.search(r"^VmHWM:\s+(\d+)\s+kB", status, re.MULTILINE)
    return int(found.group(1)) if found else None


def run_child(command: list[str], poll: float = 0.5) -> dict:
    """Run a command (its stdout discarded); its exit code, seconds and peak RSS in kB."""
    start = time.monotonic()
    child = subprocess.Popen(command, stdout=subprocess.DEVNULL)
    peak = 0
    while True:
        pid, status, usage = os.wait4(child.pid, os.WNOHANG)
        if pid:
            break
        peak = max(peak, vm_hwm(child.pid) or 0)
        time.sleep(poll)
    child.returncode = os.waitstatus_to_exitcode(status)
    return {
        "exit": child.returncode,
        "seconds": round(time.monotonic() - start, 3),
        "peak_rss_kb": max(peak, usage.ru_maxrss),  # ru_maxrss is kB on Linux
    }


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
)


def table(header, rows) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|---" * len(header) + "|"]
    return lines + ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]


def markdown(report: dict) -> str:
    """One row per hour, then the last hour's slowest statements per step and largest tables."""
    lines = ["# Scale cycle report (SCALE-GEN-1b)", ""]
    lines.append(
        f"Database `{report['database']}`, statement timings from {report['statements_from']}, "
        f"pulse rate {report['pulse_rate']} runs/s, written {report['written_at']}."
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
    """`trace OUT MODULE [ARGS]`: run a module as __main__ under a Tracer; timings to OUT."""
    out, module, *rest = argv
    code = 0
    with Tracer() as tracer:
        sys.argv = [module, *rest]
        try:
            runpy.run_module(module, run_name="__main__", alter_sys=True)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else int(exc.code is not None)
        finally:
            Path(out).write_text(json.dumps(tracer.report()))
    return code


if __name__ == "__main__":
    if sys.argv[1:2] != ["trace"] or len(sys.argv) < 4:
        sys.exit("usage: measure.py trace OUT MODULE [ARGS...]")
    sys.exit(trace(sys.argv[2:]))
