"""build()'s product groups measured in worker processes (CALC-SCALE-1, calculate.workers).

The hourly build measured every product group (product_windows.measure()) one after another
on the calculation's one connection: ~290 s of Python in production while the database ran
about one of the build's queries at a time. With calculate.workers N > 1 an incremental build
forks N workers once its inputs are read (no helper thread runs then: the prefetcher is not
started). A product is a task, taken one at a time by the next free worker, at most 2 x N tasks
out at once (a worker holds one network at a time, and finished results wait for at most that
many): it opens its own connection importing the build's snapshot
(parallel_reads.snapshot_connection()) for the product's geo rows and missed windows' inputs,
measures its groups and writes nothing: it returns each group with its windows, its fleet members
and its lists to write (Incremental.deferred), in one pickle, so a window's list reference is
still the one its group holds. The calculation writes each product's lists as its result comes
back, in the products' order, as one process would (the same list ids); once the workers are gone
it puts back each group's windows and fleet members (their rows are its own bucket rows of the
window) and goes on as before. A worker measures each group inside underload.slot_memo(), so its
windows count each time slot's rows once (CALC-PERF-2).

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
made the same way, a network a task; they read nothing from the database. Each
window's "_lead" (its headline group's window) is put back here, to the build's own group.
"""

from __future__ import annotations

import gc
import multiprocessing
import os
import signal
import sys
import time
from collections import deque
from datetime import timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace

from psycopg.rows import dict_row

from .. import sizing, timing
from . import browser, network, parallel_reads, product_windows, underload
from .aggregate import in_window

MOST = 32  # calculate.workers at most
_END = object()  # no task left (_bounded())
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
        if not products:
            return
        exported = parallel_reads.export(self.conn)
        info = SimpleNamespace(dsn=self.conn.info.dsn, password=self.conn.info.password)
        _STATE.update(step=step, groups=groups, snapshot=(SimpleNamespace(info=info), exported))
        n = min(self.n, len(products))
        print(f"calculate: {n} workers for {len(products)} products", file=sys.stderr)
        measured = {}
        try:  # a product a task, its lists written as it comes back, in the build's order
            for keys, part in zip(products, self._run(_measured, products, n), strict=True):
                for key in keys:  # one process's order: the same list ids
                    group, members, unwritten = part[key]
                    if unwritten is not None:
                        step.source.write_deferred(unwritten, step.build_id)
                    measured[key] = (group, members)
        finally:
            _STATE.clear()
        for keys in products:  # the workers are gone: no page of theirs is copied
            for key in keys:
                _apply(step, key, groups[key], measured[key])

    def networks(self, found, context):
        """network.summaries()'s entries of found ([(network key, its groups by window label)])
        in workers (network.summary()), with each window's "_lead" the build's own group's."""
        if not found:
            return []
        n = min(self.n, len(found))
        print(f"calculate: {n} workers for {len(found)} networks", file=sys.stderr)
        _STATE.update(networks=found, context=context)
        try:  # a network a task
            parts = list(self._run(_summarized, [[i] for i in range(len(found))], n))
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

    def _run(self, work, tasks, n):
        """work(task) of each task, in order, each as it comes back from n forked workers taking
        one task at a time (FORK); then a timing line "calculate workers" with the workers'
        largest private memory (MB) and the total of each worker's largest."""
        began, private = time.monotonic(), {}
        if not self.FORK:
            found = (_private(work, task) for task in tasks)
        else:
            gc.freeze()  # the workers leave the build's objects unscanned: fewer copied pages
            pool = _pool(n)
            found = _bounded(pool, partial(_private, work), tasks, 2 * n)
        try:
            for part, (pid, mb) in found:
                private[pid] = max(private.get(pid, 0), mb)
                yield part
            if self.FORK:  # each worker ends at the end of its queue: no signal needed
                pool.close()
                pool.join()
        finally:
            if self.FORK:  # after an error: the workers left are killed (SIGTERM)
                pool.terminate()
                gc.unfreeze()
        timing.line(
            "calculate",
            "workers",
            time.monotonic() - began,
            step=work.__name__.strip("_"),
            workers=n,
            private_mb=max(private.values(), default=0),
            private_total_mb=sum(private.values()),
        )


def _pool(n):
    """n forked workers that a SIGTERM ends whatever this process does on one (PUBLIC-CI-FORK-
    HANG-1): a fork copies this process's handler, and terminate() holds the workers' task queue
    and waits for each worker to end, so a worker whose inherited handler returns (asyncio's,
    which only wakes this process's loop) waited forever. SIGTERM is blocked over the fork,
    so a worker is forked with it blocked and takes it only once _started() has made it fatal."""
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        return multiprocessing.get_context("fork").Pool(n, _started, (blocked,))
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)


def _started(mask):
    """A forked worker's start: SIGTERM ends it, no signal wakes the build's loop (the wakeup
    fd it inherited), then this process's own signal mask (`mask`, from before _pool()) without
    SIGTERM, also when the build had it blocked."""
    signal.set_wakeup_fd(-1)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_SETMASK, mask - {signal.SIGTERM})


def _bounded(pool, work, tasks, most):
    """work(task) of each task from pool, in order, with at most `most` tasks out at once: a
    result waiting behind a slower earlier one holds back the next submissions."""
    tasks, out = iter(tasks), deque()
    while True:
        while len(out) < most and (task := next(tasks, _END)) is not _END:
            out.append(pool.apply_async(work, (task,)))
        if not out:
            return
        yield out.popleft().get()


def _private(work, task):
    """(work(task), (this process, its private memory in MB once it is done))."""
    return work(task), (os.getpid(), private_mb())


def private_mb():
    """This process's private memory (MB): its pages no other process shares, as a forked
    worker's copies of the build's pages; from /proc/self/smaps_rollup (Linux), else 0."""
    try:
        text = Path("/proc/self/smaps_rollup").read_text()
    except OSError:
        return 0
    names = ("Private_Clean:", "Private_Dirty:")
    return sum(int(line.split()[1]) for line in text.splitlines() if line.startswith(names)) // 1024


def _groups(labels):
    """A network's groups (each once), from its groups by window label."""
    return list({p["id"]: p for ms in labels.values() for p in ms}.values())


def _measured(share):
    """A worker's task, one product's groups: {key: (group, {window label: fleet member}, its
    lists to write)}."""
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
            with underload.slot_memo():  # its windows share their time slots' counts
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
    """Put a worker's measured group back in the build, its lists already written: its windows
    (in the group the build holds), its fleet members and IP sets, as product_windows.measure()
    leaves them."""
    found, members = measured
    if found is not p:  # a worker's copy (fork); in this process (fork False) the group itself
        p.clear()
        p.update(found)
    raw, _ = browser.split(step.bucket_groups[key])
    for label, w in p["windows"].items():
        since = step.as_of - timedelta(hours=w["period"]["hours"])
        rows = in_window(raw, since, step.as_of, w["period"]["hours"])
        seen = step.source.seen(key, since, step.as_of)
        step.fleet_groups[(p["cohort"], label)].append((p, rows, seen, members[label]))
        step.sets[(key, label)] = set()
