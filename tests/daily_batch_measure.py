"""INGEST-BATCH-2's measurement: a daily campaign hour's database writes through a delayed link.

Each case stores `--daily` synthetic daily tests (ingest_cases.daily_test: 4,300 IPs each) and
their 24 estimated hours on a fresh database, through ingest_cases.Proxy:
- one by one, as `daily ingest` did before: storage.ingest, then main's estimate_run (one upsert
  per hour, kept below as it was);
- in batches of `--batch`, as it does now: storage.ingest_many, then compute.estimate_runs.
Both store the same rows (checked). On a disposable server with PG_TEST_ADMIN_FILE set:
    python tests/daily_batch_measure.py --delay 0.039 [--daily 442] [--batch 20] [--one-by-one 40]
prints one JSON line per case: wall time, statements, round trips and connections. One by one
stores only the first `--one-by-one` tests (default all); the rows are compared when it is all.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ingest_cases import Proxy, credentials_file, daily_test, dump, proxied, run_id  # noqa: E402

from proxy_index import storage  # noqa: E402
from proxy_index.estimates import compute  # noqa: E402

FORMULA = {
    "schema": compute.SCHEMA,
    "schema_version": 1,
    "formula_version": 1,
    "network_curve": {"requests": [50] * 24},
    "countries": {
        "US": {
            "curve": "country",
            "hour_factors": [(h + 1) / 24 for h in range(24)],
            "daily_factor": 3.0,
            "requests": [100 * (h + 1) for h in range(24)],
        }
    },
}
# main's estimate_run before INGEST-BATCH-2: a round trip for the run, one per stored hour.
OLD_MEASURED = """SELECT r.run_key,r.started_at,s.product_id,
 (SELECT count(DISTINCT ip_id) FROM ip_observations o WHERE o.run_key=r.run_key) AS ips,
 (SELECT coalesce(sum(count),0)::bigint FROM ip_observations o WHERE o.run_key=r.run_key)
 AS requests
FROM test_runs r JOIN scope_stats s USING(scope_id) WHERE r.run_id=%s"""
OLD_UPSERT = """INSERT INTO estimated_hours AS e(product_id,country_code,day,hour,run_key,
 measured_at,measured_ips,measured_requests,hour_factor,unique_ips,requests,day_unique_ips,curve,
 formula_version)
VALUES (%(product_id)s,%(country)s,%(day)s,%(hour)s,%(run_key)s,%(measured_at)s,%(ips)s,
 %(measured_requests)s,%(hour_factor)s,%(unique_ips)s,%(requests)s,%(day_unique_ips)s,%(curve)s,
 %(formula_version)s)
ON CONFLICT (product_id,country_code,day,hour) DO UPDATE SET run_key=EXCLUDED.run_key,
 measured_at=EXCLUDED.measured_at,measured_ips=EXCLUDED.measured_ips,
 measured_requests=EXCLUDED.measured_requests,hour_factor=EXCLUDED.hour_factor,
 unique_ips=EXCLUDED.unique_ips,requests=EXCLUDED.requests,
 day_unique_ips=EXCLUDED.day_unique_ips,curve=EXCLUDED.curve,
 formula_version=EXCLUDED.formula_version,computed_at=now()
WHERE e.released_at IS NULL AND e.measured_at <= EXCLUDED.measured_at"""


def old_estimate_run(credentials, run, country, formula) -> int:
    with storage.connect(credentials) as conn:
        run_key, measured_at, product_id, ips, requests = conn.execute(
            OLD_MEASURED, (run,)
        ).fetchone()
        common = {"product_id": product_id, "country": country, "run_key": run_key}
        common |= {"measured_at": measured_at, "ips": ips, "measured_requests": requests}
        common["formula_version"] = formula["formula_version"]
        found = compute.hours(formula, country, measured_at, ips, requests)
        rows = [common | row for row in found if row["estimated"]]
        return sum(conn.execute(OLD_UPSERT, row).rowcount for row in rows)


def one_by_one(directories, runs, credentials, batch):
    for directory, run in zip(directories, runs, strict=True):
        storage.ingest(directory, credentials)
        old_estimate_run(credentials, run, "US", FORMULA)


def batches(directories, runs, credentials, batch):
    for start in range(0, len(directories), batch):
        storage.ingest_many(directories[start : start + batch], credentials)
        chosen = runs[start : start + batch]
        compute.estimate_runs(credentials, [(run, "US", FORMULA) for run in chosen])


def estimated(conn) -> list:
    return conn.execute(
        "SELECT p.provider,p.name,e.country_code,e.day,e.hour,t.run_id,e.measured_at,"
        "e.measured_ips,e.measured_requests,e.hour_factor,e.unique_ips,e.requests,e.day_unique_ips,e.curve,"
        "e.formula_version FROM estimated_hours e JOIN products p USING(product_id) "
        "JOIN test_runs t USING(run_key) ORDER BY 1,2,3,4,5"
    ).fetchall()


def _measure(args) -> None:
    import helpers

    from proxy_index.providers.credentials import read_credentials

    admin = read_credentials(args.admin, "postgres")
    work = Path(tempfile.mkdtemp(prefix="ingest-batch-"))
    directories = [daily_test(work / f"d{i}", i) for i in range(args.daily)]
    runs = [run_id("daily", i) for i in range(args.daily)]
    stored, alone = {}, args.one_by_one or args.daily
    for name, case, count in (("batches", batches, args.daily), ("one by one", one_by_one, alone)):
        database = helpers.scratch_database(admin)
        values = admin | {"dbname": database}
        direct = credentials_file(values, work / f"{database}.toml")
        storage.initialize(direct)
        with Proxy(values["host"], values["port"], args.delay) as proxy:
            credentials = proxied(values, proxy, work)
            began = time.monotonic()
            case(directories[:count], runs[:count], credentials, args.batch)
            wall = time.monotonic() - began
        with storage.connect(direct) as conn:
            stored[name] = (dump(conn), estimated(conn))
        record = {"case": name, "tests": count, "batch": args.batch}
        record |= {"wall_seconds": round(wall, 2), "per_test": round(wall / count, 3)}
        record |= {"statements": proxy.statements(), "round_trips": proxy.round_trips()}
        record |= {"connections": len(proxy.frontends), "hours": len(stored[name][1])}
        print(json.dumps(record), flush=True)
        helpers.drop_databases(admin, [database])
    if alone == args.daily:
        print(json.dumps({"same_rows": stored["batches"] == stored["one by one"]}), flush=True)
    helpers.drop_queued()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--admin", default=os.environ.get("PG_TEST_ADMIN_FILE"))
    parser.add_argument("--delay", type=float, default=0.039, help="seconds each way")
    parser.add_argument("--daily", type=int, default=20)
    parser.add_argument("--batch", type=int, default=20)
    parser.add_argument("--one-by-one", type=int, default=None, help="tests stored one by one")
    sys.exit(_measure(parser.parse_args()))
