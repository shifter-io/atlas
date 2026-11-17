"""CALC-SCALE-2's measurement of the hourly incremental build against days of data, on the
synthetic data set of scripts/scale/generate.py (daily tests with their IP rows, hourly pulses
with their geo rows, one DB-IP release a day; README there) in a scratch database proxy_scale.

The data set's days are generated in steps up to each of --days. At each size it runs the
incremental build one hour before the last day's end (it records the memos) and then the hourly
build at its end, the one measured: its `timing:` lines, its wall time, and with --profile the
functions of the calculation by cumulative time (cProfile, one process: calculate.workers 1).
The daily tests' verdicts are stored as migrate_v15_v16.sql stores them (the generator writes
none). On a disposable server with PG_TEST_ADMIN_FILE set:
    python tests/calc_days_measure.py [--days 3 10 30] [--networks 4] [--countries 50]
        [--scale 1.0] [--profile] [--out DIR]
prints one JSON line per size and writes DIR/days-<n>.prof with --profile.
"""

from __future__ import annotations

import argparse
import contextlib
import cProfile
import io
import json
import os
import pstats
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atlas import timing  # noqa: E402
from atlas.calculated import store  # noqa: E402
from atlas.calculated.calculator import calculate  # noqa: E402
from atlas.providers.credentials import read_credentials  # noqa: E402

POLICY = ROOT / "configs" / "methodology.toml"
TOTAL_DAYS = 31
# The data set's last day ends at the next UTC midnight, so every generated run started in the
# last 31 days: the memos collect the ranges of older runs by the clock (hourly.COLLECT).
END = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
VERDICTS = (ROOT / "src" / "atlas" / "migrate_v15_v16.sql").read_text()
VERDICTS = VERDICTS[VERDICTS.index("INSERT INTO country_verdicts") :].split(";")[0]


def database(admin_file, out):
    """A credentials file of an empty proxy_scale database."""
    admin = read_credentials(admin_file, "postgres")
    with psycopg.connect(**admin, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS proxy_scale")
        conn.execute("CREATE DATABASE proxy_scale")
    path = out / "proxy_scale.toml"
    lines = [f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {v}" for k, v in admin.items()]
    lines = [line for line in lines if not line.startswith("dbname")] + ['dbname = "proxy_scale"']
    path.write_text("[postgres]\n" + "\n".join(lines) + "\n")
    path.chmod(0o600)
    return path


def grow(config, first, last, args):
    """Generate days first..last and store their daily tests' verdicts."""
    argv = ["--database", str(config), "--days", f"{first}..{last}", "--seed", "1"]
    argv += ["--networks", str(args.networks), "--countries", str(args.countries)]
    argv += ["--total-days", str(TOTAL_DAYS), "--end-date", END.date().isoformat()]
    argv += ["--scale", str(args.scale), "--jobs", str(args.jobs)]
    script = ROOT / "scripts" / "scale" / "generate.py"  # its own process: it spawns workers
    subprocess.run([sys.executable, str(script), *argv], check=True, stdout=subprocess.DEVNULL)
    with psycopg.connect(**read_credentials(config, "postgres"), autocommit=True) as conn:
        conn.execute(VERDICTS)
        conn.execute("ANALYZE")


def hourly(config, as_of, profile):
    """The incremental build at as_of: (seconds, its timing lines, its profile or None)."""
    err, profiler = io.StringIO(), cProfile.Profile() if profile else None
    began = time.monotonic()
    with contextlib.redirect_stderr(err):
        if profiler:
            profiler.enable()
        calculate(config, POLICY, as_of=as_of, publish=True, mode="incremental")
        if profiler:
            profiler.disable()
    return time.monotonic() - began, timing.lines(err.getvalue()), profiler


def rows(config):
    query = "SELECT (SELECT count(*) FROM ip_observations),(SELECT count(*) FROM run_geo_buckets)"
    with psycopg.connect(**read_credentials(config, "postgres")) as conn:
        return conn.execute(query).fetchone()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, nargs="+", default=[3, 10, 30])
    parser.add_argument("--networks", type=int, default=4)
    parser.add_argument("--countries", type=int, default=50)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--jobs", type=int, default=4, help="networks generated at once")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--out", type=Path, default=Path("calc-days"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    config = database(os.environ["PG_TEST_ADMIN_FILE"], args.out)
    done = 0
    for days in sorted(args.days):
        grow(config, done + 1, days, args)
        if not done:
            store.initialize(config)  # the calculated schema, on the generated storage schema
        done = days
        end = END - timedelta(days=TOTAL_DAYS - days)
        hourly(config, end - timedelta(hours=1), False)  # the build before records the memos
        seconds, lines, profiler = hourly(config, end, args.profile)
        observations, geo = rows(config)
        found = {"days": days, "observations": observations, "geo_rows": geo}
        found |= {"seconds": round(seconds, 1), "timing": lines}
        print(json.dumps(found), flush=True)
        if profiler:
            profiler.dump_stats(args.out / f"days-{days}.prof")
            text = io.StringIO()
            pstats.Stats(profiler, stream=text).sort_stats("cumulative").print_stats("atlas", 40)
            (args.out / f"days-{days}.txt").write_text(text.getvalue())


if __name__ == "__main__":
    main()
