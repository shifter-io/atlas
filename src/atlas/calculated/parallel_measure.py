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

A forked worker shares the build's memory until it writes a page, and reading an object writes its
reference count: so the build makes each network's objects together (the runs and the pulse
buckets are read network by network, each group's bucket rows made group by group), and a worker
copies the pages of its own networks rather than of every one (CALC-SCALE-1: 2 GB a worker at 19 h
of data, when they were made in time order). Each pool prints a timing line "calculate workers"
with its workers' largest and total private memory.

A worker's snapshot holds what was committed when the build began, not the build's own writes.
Its reads (geo rollups, observations, enrichment, product IPs, runs) are all committed before the
build, except the geo rollup hours the build recounts in its own transaction (rollups.refresh():
what went stale since rollups.fill()); when it recounted any, the build measures in its own
process, where they are seen.

The network summaries (network.summaries(): Python over every bucket row of each network) are
made the same way, a share of the networks a worker; they read nothing from the database. Each
window's "_lead" (its headline group's window) is put back here, to the build's own group.
"""

from __future__ import annotations

import gc
import multiprocessing
import sys
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from psycopg.rows import dict_row

from .. import sizing, timing
from . import browser, network, parallel_reads, product_windows

MOST = 32  # calculate.workers at most
_STATE = {}  # what the forked workers inherit: the build's step, groups and snapshot


def measure(conn, mode, n=None):
    """An incremental build's (or a recount's) Measure in calculate.workers (or n) workers; None
    measures in this process: one worker, or the full calculation (the oracle)."""
    if mode == "full":
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
        recounted = step.geo_groups.geo.recounted
        if recounted:  # uncommitted in the build: a worker's snapshot would read them stale
            print(f"calculate: 1 process ({len(recounted)} geo hours recounted)", file=sys.stderr)
            for keys in products:
                for key in keys:
                    product_windows.measure(step, key, groups[key])
            return
        shares = _shares(products, self.n, step)
        exported = parallel_reads.export(self.conn)
        info = SimpleNamespace(dsn=self.conn.info.dsn, password=self.conn.info.password)
        _STATE.update(step=step, groups=groups, snapshot=(SimpleNamespace(info=info), exported))
        print(f"calculate: {len(shares)} workers for {len(products)} products", file=sys.stderr)
        try:
            found = self._run(_measured, shares)
        finally:
            _STATE.clear()
        measured = {key: result for part in found for key, result in part.items()}
        for keys in products:  # one process's order: the same list ids
            for key in keys:
                _apply(step, key, groups[key], measured[key])

    def networks(self, found, context):
        """network.summaries()'s entries of found ([(network key, its groups by window label)])
        in workers (network.summary()), with each window's "_lead" the build's own group's."""
        rows = context[0]  # bucket_groups
        weights = [
            sum(len(rows[p["product_id"], p["cohort"]]) + 1 for p in _groups(labels))
            for _, labels in found
        ]
        shares = _balanced(weights, self.n)
        print(f"calculate: {len(shares)} workers for {len(found)} networks", file=sys.stderr)
        _STATE.update(networks=found, context=context)
        try:
            parts = self._run(_summarized, shares)
        finally:
            _STATE.clear()
        summarized = {i: result for part in parts for i, result in part.items()}
        entries = []
        for i, (_, labels) in enumerate(found):
            entry, lead = summarized[i]
            group = next(p for p in _groups(labels) if p["id"] == lead)
            for label, w in entry["windows"].items():
                w["_lead"] = group["windows"].get(label, {})
            entries.append(entry)
        return entries

    def _run(self, work, shares):
        """[work(share) for share in shares], in forked workers (FORK); prints a timing line
        "calculate workers" with the workers' largest and total private memory (MB)."""
        began = time.monotonic()
        if not self.FORK:
            found = [_private(work, share) for share in shares]
        else:
            gc.freeze()  # the workers leave the build's objects unscanned: fewer copied pages
            try:
                with multiprocessing.get_context("fork").Pool(len(shares)) as pool:
                    found = pool.starmap(_private, [(work, share) for share in shares])
            finally:
                gc.unfreeze()
        private = [mb for _, mb in found]
        timing.line(
            "calculate",
            "workers",
            time.monotonic() - began,
            step=work.__name__.strip("_"),
            workers=len(shares),
            private_mb=max(private),
            private_total_mb=sum(private),
        )
        return [part for part, _ in found]


def _private(work, share):
    """(work(share), this process's private memory in MB once it is done)."""
    return work(share), private_mb()


def private_mb():
    """This process's private memory (MB): its pages no other process shares, as a forked
    worker's copies of the build's pages; from /proc/self/smaps_rollup (Linux), else 0."""
    try:
        text = Path("/proc/self/smaps_rollup").read_text()
    except OSError:
        return 0
    names = ("Private_Clean:", "Private_Dirty:")
    return sum(int(line.split()[1]) for line in text.splitlines() if line.startswith(names)) // 1024


def _shares(products, n, step):
    """The products' groups in at most n shares of about equal bucket rows, largest first."""
    weights = [sum(len(step.bucket_groups[key]) + 1 for key in keys) for keys in products]
    return [[key for i in share for key in products[i]] for share in _balanced(weights, n)]


def _groups(labels):
    """A network's groups (each once), from its groups by window label."""
    return list({p["id"]: p for ms in labels.values() for p in ms}.values())


def _balanced(weights, n):
    """The indexes of weights in at most n shares of about equal weight, largest first."""
    shares = [[] for _ in range(min(n, len(weights)))]
    loads = [0] * len(shares)
    for i in sorted(range(len(weights)), key=lambda i: -weights[i]):
        least = loads.index(min(loads))
        shares[least].append(i)
        loads[least] += weights[i]
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


def _summarized(share):
    """A worker's share of the networks: {index: (entry, its headline group's id)}; "_lead"
    left out (the build's own group's window is put back: Measure.networks())."""
    found, context = _STATE["networks"], _STATE["context"]
    summarized = {}
    for i in share:
        entry, lead = network.summary(found[i][1], context)
        for w in entry["windows"].values():
            w["_lead"] = None
        summarized[i] = (entry, lead)
    return summarized


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
