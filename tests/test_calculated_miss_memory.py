"""CALC-INC-1b: a missed window is counted in the database, so the incremental calculation's peak
Python memory does not follow the observations, on the scale generator's fixture (SCALE-GEN-1a).

The fixture is a slice of the full-size synthetic data: one network in two countries over two
days, each daily test with its formula's IPs (SCALE), so its observation rows outnumber its label
combinations. On a fresh calculated schema every window of the incremental build misses. The
incremental build's peak (tracemalloc) stays under BOUND, while the full build (the oracle, which
holds every observation row) peaks above three times it, and the two builds read back equal.
Measured on the CI PostgreSQL server (2026-11-05): 10,947 observations, incremental 10.9 MB,
full 63.7 MB.

One test measures the incremental peak, the other the full build's peak after an incremental
build and the equality of the two, each on its own copy of the fixture, so neither waits for
the other. tracemalloc counts only this process's allocations, so xdist runs them in parallel.
The full build reaches its peak in its first windows and tracing slows it about six times, so
its tracing stops once the peak passes three times BOUND: the peak asserted is a lower bound of
the whole build's (59 MB without coverage). No coverage (CI-90-3): line tracing under
tracemalloc made them three times slower in CI, and the other calculated and scale tests cover
every line and branch they do.
"""

import gc
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


def peak(database, mode, enough=None):
    """The build in mode at AS_OF and its peak Python allocations in bytes. With `enough`, the
    tracing stops at the first garbage collection after the peak passes it and the rest of the
    build runs untraced: the peak returned is then over `enough`, and at most the build's."""
    seen = [0]

    def check(phase, info):
        if phase == "stop" and tracemalloc.is_tracing():
            seen[0] = max(seen[0], tracemalloc.get_traced_memory()[1])
            if seen[0] > enough:
                tracemalloc.stop()

    tracemalloc.start()
    if enough is not None:
        gc.callbacks.append(check)
    try:
        payload = calculate(database, POLICY, as_of=AS_OF, mode=mode)
        if tracemalloc.is_tracing():
            seen[0] = max(seen[0], tracemalloc.get_traced_memory()[1])
        return payload, seen[0]
    finally:
        if enough is not None:
            gc.callbacks.remove(check)
        tracemalloc.stop()


def scale_fixture(databases):
    """A fresh database with the scale fixture and an empty calculated schema, and its
    observation count."""
    config = databases()
    run(config, "1..2", networks="iproyal", countries="DE,US", scale=SCALE)
    store.initialize(config)
    with connect(config) as conn:
        return config, conn.execute("SELECT count(*) FROM ip_observations").fetchone()[0]


@pytest.mark.no_cover
def test_missed_windows_keep_the_incremental_peak_under_a_bound(databases):
    config, rows = scale_fixture(databases)
    _, incremental = peak(config, "incremental")
    print(json.dumps({"observations": rows, "incremental": incremental}))
    assert incremental < BOUND, (rows, incremental)


@pytest.mark.no_cover
def test_the_full_build_peaks_above_the_bound_and_reads_back_equal(databases):
    """The full build after the incremental one, as the oracle: its peak is over three times
    BOUND (traced until it is) and the two builds read back equal."""
    config, rows = scale_fixture(databases)
    new = calculate(config, POLICY, as_of=AS_OF, mode="incremental")
    full, whole = peak(config, "full", enough=3 * BOUND)
    print(json.dumps({"observations": rows, "full": whole}))
    assert whole > 3 * BOUND, (rows, whole)
    a, b = (without(store.read(config, p["snapshot_id"])) for p in (new, full))
    assert json.dumps(a) == json.dumps(b), first_difference(a, b)
