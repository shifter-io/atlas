"""CALC-SCALE-1 (#1125): `calculate --full` is a recount (incremental.Recount): the incremental
calculation reusing no stored list, so every window is counted again in the database and every
list written or verified, and it equals the Python oracle (`calculate --oracle`, mode "full").
Its memory does not follow the window's observation rows, which the oracle holds in Python (56 GB
at ~10 M rows in production): adding observation rows grows the oracle's peak, not the recount's.

CI-BUDGET-2: the oracle's growth and the recount's are two tests, each on its own fixture with its
two traced builds, so xdist runs them in parallel (together they took 90-95 s in the merge queue).
The recount's bound is a tenth of the least the oracle grows by, as when one test compared the two.
No coverage, like test_calculated_miss_memory (CI-90-3): line tracing under tracemalloc slows them
several times, and the other calculated tests (test_calc*, test_estimates*) cover every line and
branch they do.
"""

import json
import tracemalloc
from datetime import UTC, datetime

import pytest
from calculated_cases import lenient_policy
from psycopg.rows import dict_row
from test_calculated_hourly_reads import HOUR, PRODUCT_IPS, floor_hour
from test_calculated_incremental import first_difference, history, without

from atlas.calculated import incremental, store
from atlas.calculated.calculator import calculate
from atlas.calculated.policy import load
from atlas.storage import connect

pytest_plugins = ["test_storage"]

ROWS = 20_000
# %(n)s new IPs observed once each by run %(run)s (a new one: a stored run's rows never change),
# in its first bucket, and their requests, so the bucket's successes cover its observations.
MORE = """WITH r AS (SELECT run_key,min(bucket_start) AS at FROM ip_observations
 WHERE run_key=%(run)s GROUP BY 1),
ips AS (INSERT INTO ip_addresses(ip)
 SELECT ('10.'||(g/65536)||'.'||(g/256%%256)||'.'||(g%%256))::inet FROM generate_series(1,%(n)s) g
 RETURNING ip_id)
INSERT INTO ip_observations(bucket_start,run_key,ip_id,count,first_seen,last_seen)
SELECT r.at,r.run_key,ips.ip_id,1,r.at,r.at FROM r CROSS JOIN ips"""
REQUESTS = """UPDATE run_buckets SET completed=completed+%(n)s,successes=successes+%(n)s
WHERE run_key=%(run)s AND bucket_start=(SELECT min(bucket_start) FROM ip_observations
 WHERE run_key=%(run)s)"""


@pytest.mark.asyncio
async def test_a_recount_counts_every_window_again_and_equals_the_oracle(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    calculate(database, policy, as_of=end, mode="incremental")  # its signed lists are stored
    config, method_id = load(policy)
    with connect(database) as conn:
        conn.row_factory = dict_row
        hourly = incremental.read(conn, config, method_id, end, "incremental")
        again = incremental.read(conn, config, method_id, end, "recount")
    assert hourly.source.hits and again.source.hits == {}
    groups = {r["group"] for r in again.runs}
    assert set(again.source.missed) == groups and len(groups) > 3
    assert all(len(hours) == len(config["windows"]) for hours in again.source.missed.values())
    assert again.fingerprint != hourly.fingerprint  # it does not reuse the hourly build
    new = calculate(database, policy, as_of=end, mode="recount")
    full = calculate(database, policy, as_of=end, mode="full")
    a, b = (without(store.read(database, p["snapshot_id"])) for p in (new, full))
    assert json.dumps(a) == json.dumps(b), first_difference(a, b)


def peak(database, policy, as_of, mode):
    """The Python memory peak (tracemalloc) of one build."""
    tracemalloc.start()
    try:
        calculate(database, policy, as_of=as_of, mode=mode)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


async def growth(database, tmp_path, mode):
    """How much a `mode` build's peak grows when ROWS observation rows are added to its window."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    h = await history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    before = peak(database, policy, end - HOUR, mode)
    run = await h.daily("alpha", "us", HOUR // 2)  # in the next build's window only
    with connect(database) as conn:
        conn.execute(MORE, {"n": ROWS, "run": run})
        conn.execute(REQUESTS, {"n": ROWS, "run": run})
        conn.execute(PRODUCT_IPS)
    return peak(database, policy, end, mode) - before


@pytest.mark.no_cover
@pytest.mark.asyncio
async def test_the_oracles_memory_follows_the_observation_rows(database, tmp_path):
    grown = await growth(database, tmp_path, "full")
    assert grown > ROWS * 1_000, grown  # the oracle holds every row in Python


@pytest.mark.no_cover
@pytest.mark.asyncio
async def test_a_recounts_memory_does_not_follow_the_observation_rows(database, tmp_path):
    grown = await growth(database, tmp_path, "recount")
    assert grown < ROWS * 1_000 / 10, grown  # a tenth of the oracle's least growth (above)
