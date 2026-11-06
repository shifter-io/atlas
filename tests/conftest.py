"""Opt-in test timeline (CI_TIMELINE=1): one JSON line per test phase and per database span.

Each xdist worker appends to `$EV/timeline-<worker>.jsonl` (the current directory without
EV): the setup, call and teardown of every test with its start and end, and the spans that
create a scratch database (`helpers.scratch_database`) or initialise a schema
(`storage.initialize`, `calculated.store.initialize`). `scripts/ci/timeline.py` sums them
per worker. Without CI_TIMELINE nothing is recorded or wrapped.
"""

from __future__ import annotations

import functools
import os

from helpers import TIMELINE, timed, timeline_record

if TIMELINE:
    from proxy_index import storage
    from proxy_index.calculated import store

    def _timed(kind, function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            with timed(kind):
                return function(*args, **kwargs)

        return wrapper

    # Test modules import these names after this file runs, so they get the timed ones;
    # calculated.store bound storage.initialize on import, so its nested call is not counted.
    storage.initialize = _timed("initialize", storage.initialize)
    store.initialize = _timed("initialize", store.initialize)

    def pytest_runtest_logreport(report):
        timeline_record(
            {
                "kind": report.when,
                "nodeid": report.nodeid,
                "start": report.start,
                "stop": report.stop,
                "outcome": report.outcome,
            }
        )

    def pytest_sessionstart(session):
        timeline_record({"kind": "sessionstart", "pid": os.getpid()})

    def pytest_sessionfinish(session):
        timeline_record({"kind": "sessionfinish", "pid": os.getpid()})
