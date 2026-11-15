"""build()'s product groups measured in worker processes (CALC-SCALE-1, calculate.workers).

The hourly build measured every product group (product_windows.measure()) one after another
on the calculation's one connection: ~290 s of Python in production while the database ran
about one of the build's queries at a time. With calculate.workers N > 1 an incremental build
forks N workers once its inputs are read (no helper thread runs then: the prefetcher is not
started). Each takes a share of the products, opens its own connection importing the build's
snapshot (parallel_reads.snapshot_connection()) for its products' geo rows and missed windows'
inputs, measures their groups and writes nothing: it returns each group with its windows, its
fleet members and its lists to write (Incremental.deferred), in one pickle, so a window's list
reference is still the one its group holds. The calculation then writes the lists group by
group in the order one process would (the same list ids), puts back each group's fleet
members (their rows are its own bucket rows of the window) and goes on as before.
"""

from __future__ import annotations

import gc
import multiprocessing
import sys
from datetime import timedelta
from types import SimpleNamespace

from psycopg.rows import dict_row

from .. import sizing
from . import browser, parallel_reads, product_windows

MOST = 32  # calculate.workers at most
_STATE = {}  # what the forked workers inherit: the build's step, groups and snapshot


def measure(conn, mode, n=None):
    """An incremental build's Measure in calculate.workers (or n) workers; None measures in this
    process: one worker, or the full calculation (the oracle)."""
    if mode != "incremental":
        return None
    n = workers(sizing.limit(conn, "calculate.workers") if n is None else n)
    return Measure(conn, n) if n > 1 else None


def workers(value):
    """calculate.workers, checked: 1 measures in the calculation's own process."""
    if type(value) is not int or not 1 <= value <= MOST:
        raise ValueError(f"calculate.workers must be a whole number from 1 to {MOST}")
    return value


class Measure:
    """calculator._measure()'s measuring of every product's groups, in `n` forked workers (FORK
    False: the same shares one after another in this process, which the tests use)."""

    FORK = True

    def __init__(self, conn, n):
        self.conn, self.n = conn, workers(n)

    def __call__(self, step, groups, products):
        shares = _shares(products, self.n, step)
        exported = parallel_reads.export(self.conn)
        info = SimpleNamespace(dsn=self.conn.info.dsn, password=self.conn.info.password)
        _STATE.update(step=step, groups=groups, snapshot=(SimpleNamespace(info=info), exported))
        print(f"calculate: {len(shares)} workers for {len(products)} products", file=sys.stderr)
        try:
            found = self._run(shares)
        finally:
            _STATE.clear()
        measured = {key: result for part in found for key, result in part.items()}
        for keys in products:  # one process's order: the same list ids
            for key in keys:
                _apply(step, key, groups[key], measured[key])

    def _run(self, shares):
        if not self.FORK:
            return [_measured(share) for share in shares]
        gc.freeze()  # the workers leave the build's objects unscanned: fewer copied pages
        try:
            with multiprocessing.get_context("fork").Pool(len(shares)) as pool:
                return pool.map(_measured, shares)
        finally:
            gc.unfreeze()


def _shares(products, n, step):
    """The products' groups in at most n shares of about equal bucket rows, largest first."""
    weight = {tuple(keys): sum(len(step.bucket_groups[k]) + 1 for k in keys) for keys in products}
    shares = [[] for _ in range(min(n, len(products)))]
    loads = [0] * len(shares)
    for keys in sorted(products, key=lambda keys: -weight[tuple(keys)]):
        least = loads.index(min(loads))
        shares[least] += keys
        loads[least] += weight[tuple(keys)]
    return [share for share in shares if share]


def _measured(share):
    """A worker's share: {key: (group, {window label: fleet member}, its lists to write)}."""
    step, groups = _STATE["step"], _STATE["groups"]
    source, geo = step.source, step.geo_groups.geo
    kept = (source.conn, source.deferred, geo.conn)  # the build's: neither used nor closed here
    conn = parallel_reads.snapshot_connection(*_STATE["snapshot"])
    conn.row_factory = dict_row  # as the build's own connection reads
    source.conn, source.deferred, geo.conn = conn, {}, conn
    try:
        found = {}
        for key in share:
            p = groups[key]
            product_windows.measure(step, key, p)
            members = {}  # what measure() added to the build's state, taken back out
            for label in p["windows"]:
                members[label] = step.fleet_groups[p["cohort"], label].pop()[3]
                step.sets.pop((key, label))
            found[key] = (p, members, source.deferred.pop(key, None))
        return found
    finally:
        source.conn, source.deferred, geo.conn = kept
        conn.close()


def _apply(step, key, p, measured):
    """Put a worker's measured group back in the build: its windows (in the group the build
    holds), its lists written (Incremental.write_deferred()), its fleet members and IP sets, as
    product_windows.measure() leaves them."""
    found, members, unwritten = measured
    if found is not p:  # a worker's copy (fork); in this process (fork False) the group itself
        p.clear()
        p.update(found)
    if unwritten is not None:
        step.source.write_deferred(unwritten, step.build_id)
    raw, _ = browser.split(step.bucket_groups[key])
    for label, w in p["windows"].items():
        since = step.as_of - timedelta(hours=w["period"]["hours"])
        rows = [r for r in raw if since <= r["bucket_start"] < step.as_of]
        seen = step.source.seen(key, since, step.as_of)
        step.fleet_groups[(p["cohort"], label)].append((p, rows, seen, members[label]))
        step.sets[(key, label)] = set()
