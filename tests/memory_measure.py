"""CALC-SCALE-1's memory measurement of the calculation on scaled synthetic data.

scripts/scale/generate.py fills a scratch database proxy_scale (--days days of --countries
countries for every network: a daily test per network and country a day, a pulse an hour); then
the calculation runs as the orchestrator runs it, `atlas calculate --publish`, with
calculate.workers set to --workers: a first build that misses every list (a recount after a
deploy), an hourly build one hour later, and `calculate --full`. While each runs, every 0.2 s the
process tree's memory is read from /proc: the sum of each process's PSS (shared pages counted
once, as the systemd scope counts them), the main process's private memory and the largest
worker's. On a disposable server with PG_TEST_ADMIN_FILE set:
    python tests/memory_measure.py [--days 2] [--countries 60] [--workers 8]
prints one JSON line per build.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).parent))

from ingest_cases import credentials_file  # noqa: E402

from atlas.providers.credentials import read_credentials  # noqa: E402

ROOT = Path(__file__).parent.parent


def rollup(pid):
    """(PSS, private) of a process in MB, from /proc/<pid>/smaps_rollup; (0, 0) once gone."""
    try:
        text = Path(f"/proc/{pid}/smaps_rollup").read_text()
    except OSError:
        return 0, 0
    found = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if rest.strip().endswith("kB"):
            found[name] = int(rest.split()[0]) // 1024
    return found.get("Pss", 0), found.get("Private_Clean", 0) + found.get("Private_Dirty", 0)


def tree(pid):
    """pid and its descendants."""
    found, todo = [], [pid]
    while todo:
        p = todo.pop()
        found.append(p)
        for task in Path(f"/proc/{p}/task").glob("*") if Path(f"/proc/{p}").exists() else ():
            try:
                todo += [int(c) for c in (task / "children").read_text().split()]
            except OSError:
                pass
    return found


def watched(argv, env):
    """Run argv; its seconds, the tree's peak PSS sum and the peaks of the main process's and
    the largest child's private memory (MB)."""
    began, peaks = time.monotonic(), {"pss": 0, "main": 0, "worker": 0}
    proc = subprocess.Popen(argv, env=env, cwd=ROOT, stdout=subprocess.DEVNULL)

    def sample():
        while proc.poll() is None:
            pids = tree(proc.pid)
            sizes = {p: rollup(p) for p in pids}
            peaks["pss"] = max(peaks["pss"], sum(s[0] for s in sizes.values()))
            child = max((s[1] for p, s in sizes.items() if p != proc.pid), default=0)
            peaks["main"] = max(peaks["main"], sizes.get(proc.pid, (0, 0))[1])
            peaks["worker"] = max(peaks["worker"], child)
            time.sleep(0.2)

    thread = threading.Thread(target=sample)
    thread.start()
    code = proc.wait()
    thread.join()
    return {"exit": code, "seconds": round(time.monotonic() - began, 1)} | {
        f"peak_{k}_mb": v for k, v in peaks.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--countries", default="60")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    admin = read_credentials(os.environ["PG_TEST_ADMIN_FILE"], "postgres")
    with psycopg.connect(**admin, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS proxy_scale")
        conn.execute("CREATE DATABASE proxy_scale")
    database = credentials_file(admin | {"dbname": "proxy_scale"}, Path(tempfile.mkdtemp()) / "db")
    end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    python, env = sys.executable, os.environ.copy()
    generate = [python, "scripts/scale/generate.py", "--database", str(database)]
    generate += ["--countries", args.countries, "--total-days", str(args.days)]
    subprocess.run(
        [*generate, "--days", f"1..{args.days}", "--end-date", end.date().isoformat()],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    atlas = [str(Path(python).parent / "atlas")]  # the venv's console script
    subprocess.run([*atlas, "db-init", "--database-file", str(database)], cwd=ROOT, check=True)
    with psycopg.connect(**read_credentials(database, "postgres"), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES ('calculate.workers',%s,now()) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(args.workers),),
        )
    calculate = [*atlas, "calculate", "--database-file", str(database), "--publish"]
    for label, as_of, mode in (
        ("first build (every list missed)", end - timedelta(hours=1), "--incremental"),
        ("hourly build", end, "--incremental"),
        ("full recalculation (recount)", end, "--full"),
    ):
        found = watched([*calculate, "--as-of", as_of.isoformat(), mode], env)
        print(json.dumps({"build": label, "workers": args.workers} | found), flush=True)


if __name__ == "__main__":
    main()
