"""Longest-first test order for xdist workers, and the opt-in test timeline (CI_TIMELINE=1).

Order: under `--dist worksteal` each worker reorders its collection the same way, so that
the scheduler's first even split (`spread`) hands every worker its share of the slow
tests in `tests/durations.json` (seconds per node id, `scripts/ci/timeline.py --durations`), each
worker's longest first. The tests themselves do not change; a test missing from the file
counts as fast.

Timeline: each xdist worker (or a run without xdist) appends to
`$EV/timeline-<worker>.jsonl` (the current directory without EV): the setup, call and
teardown of every test with its start and end, and the spans that create a scratch
database (`helpers.scratch_database`) or initialise a schema (`storage.initialize`,
`calculated.store.initialize`). The xdist controller and a pytest a test starts do not
record. `scripts/ci/timeline.py` sums it per worker. Without CI_TIMELINE nothing is
recorded or wrapped.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path

import helpers
import pytest

DURATIONS = Path(__file__).with_name("durations.json")
# The estimate for a test missing from DURATIONS.
FAST = 0.1


def chunks(count, workers):
    """The sizes of worksteal's first split of `count` tests over `workers` idle workers."""
    sizes = []
    for index in range(workers):
        sizes.append(count // (workers - index))
        count -= sizes[-1]
    return sizes


def spread(items, durations, workers):
    """`items` reordered so each of worksteal's first chunks gets an even share of the slow
    ones (the longest first, each to the least loaded chunk with room), longest first in
    its chunk, then fast tests in their collection order."""
    sizes = chunks(len(items), workers)
    load = [FAST * size for size in sizes]
    slow = [[] for _ in sizes]
    known = sorted(
        (item for item in items if item.nodeid in durations),
        key=lambda item: -durations[item.nodeid],
    )
    for item in known:
        room = [i for i, size in enumerate(sizes) if len(slow[i]) < size]
        target = min(room, key=lambda i: load[i])
        slow[target].append(item)
        load[target] += durations[item.nodeid] - FAST
    taken = {id(item) for item in known}
    fast = iter(item for item in items if id(item) not in taken)
    ordered = []
    for size, mine in zip(sizes, slow, strict=True):
        ordered += mine + [next(fast) for _ in range(size - len(mine))]
    return ordered


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    workers = getattr(config, "workerinput", {}).get("workercount")
    if not workers or config.getoption("dist", None) != "worksteal" or not DURATIONS.exists():
        return
    items[:] = spread(items, json.loads(DURATIONS.read_text()), workers)


if helpers.TIMELINE:
    from proxy_index import storage
    from proxy_index.calculated import store

    def _timed(kind, function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            with helpers.timed(kind):
                return function(*args, **kwargs)

        return wrapper

    # Test modules import these names after this file runs, so they get the timed ones;
    # calculated.store bound storage.initialize on import, so its nested call is not counted.
    storage.initialize = _timed("initialize", storage.initialize)
    store.initialize = _timed("initialize", store.initialize)

    def pytest_configure(config):
        # Only the process that runs the tests records: an xdist worker or a run without
        # xdist, never the controller nor a pytest started by a test (it inherits the owner).
        owner = os.environ.get("CI_TIMELINE_OWNER")
        runs_tests = hasattr(config, "workerinput") or config.getoption("dist", "no") == "no"
        if owner and owner != str(os.getpid()) or not runs_tests:
            helpers.TIMELINE = False
            return
        os.environ["CI_TIMELINE_OWNER"] = str(os.getpid())

    def pytest_runtest_logreport(report):
        helpers.timeline_record(
            {
                "kind": report.when,
                "nodeid": report.nodeid,
                "start": report.start,
                "stop": report.stop,
                "outcome": report.outcome,
            }
        )
