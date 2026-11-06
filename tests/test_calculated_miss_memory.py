"""CALC-INC-1b: a missed window is counted in the database, so the incremental calculation's peak
Python memory does not follow the observations, on the scale generator's fixture (SCALE-GEN-1a).

The fixture is a slice of the full-size synthetic data: one network in two countries over two
days, each daily test with its formula's IPs (SCALE), so its observation rows outnumber its label
combinations. On a fresh calculated schema every window of the incremental build misses. The
incremental build's peak (tracemalloc) stays under BOUND, while the full build (the oracle, which
holds every observation row) peaks above three times it, and the two builds read back equal.
Measured on the CI PostgreSQL server (2026-11-05): 10,947 observations, incremental 10.9 MB,
full 63.7 MB.
"""

import json
import tracemalloc
from datetime import UTC, datetime

import pytest
from test_calculated_incremental import first_difference, without
from test_scale_gen import POLICY, run

from proxy_index.calculated import store
from proxy_index.calculated.calculator import calculate
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

SCALE = 0.05  # of each daily test's formula IPs
AS_OF = datetime(2026, 10, 3, tzinfo=UTC)  # the end of the generated days
BOUND = 16 * 2**20  # bytes: the incremental build's peak Python allocations


def peak(database, mode):
    """The build in mode at AS_OF and its peak Python allocations in bytes."""
    tracemalloc.start()
    try:
        payload = calculate(database, POLICY, as_of=AS_OF, mode=mode)
        return payload, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.slow
def test_missed_windows_keep_memory_under_a_bound(databases):
    config = databases()
    run(config, "1..2", networks="iproyal", countries="DE,US", scale=SCALE)
    store.initialize(config)
    with connect(config) as conn:
        rows = conn.execute("SELECT count(*) FROM ip_observations").fetchone()[0]
    new, incremental = peak(config, "incremental")
    full, whole = peak(config, "full")
    print(json.dumps({"observations": rows, "incremental": incremental, "full": whole}))
    assert incremental < BOUND and whole > 3 * BOUND, (rows, incremental, whole)
    a, b = (without(store.read(config, p["snapshot_id"])) for p in (new, full))
    assert json.dumps(a) == json.dumps(b), first_difference(a, b)
