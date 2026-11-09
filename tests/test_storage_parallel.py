"""INGEST-PARALLEL-1: an hour's groups stored over N connections at once store what storing them
one after the other stores; a deadlock is retried, a failing group fails alone, a stopped store
resumes to the same rows."""

from __future__ import annotations

import importlib
import json
import multiprocessing
import queue
import sys
import threading
import time
from pathlib import Path

import psycopg
import pytest
from estimates_cases import formula
from ingest_cases import daily_test, dump, pulse, run_id

from proxy_index import storage, storage_parallel
from proxy_index.estimates import compute
from proxy_index.storage import IngestBatchError, connect, seed
from proxy_index.storage_parallel import Group

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scale"))
measure = importlib.import_module("measure")

pytest_plugins = ["test_storage", "calculated_template"]

OVERLAP = (
    "INSERT INTO provider_overlap(provider_a,provider_b,shared_ips) VALUES (%s,%s,1) "
    "ON CONFLICT(provider_a,provider_b) DO UPDATE SET shared_ips=provider_overlap.shared_ips+1"
)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """Short retry pauses; the whole server free for the test's connections."""
    monkeypatch.setattr(storage_parallel, "BACKOFF", 0.01)
    monkeypatch.setattr(storage_parallel, "RESERVE", 0)
    monkeypatch.setattr(storage_parallel, "RESERVE_SHARE", 0)


def network(n: int) -> str:
    return f"net-{n // 2 % 3}"


def hour(tmp_path, tests=8, pulses=4):
    """An hour's groups: daily tests of 3 networks where tests 2k and 2k+1 share a network and
    each test shares 30% of its IPs with the next (so with another group, at its own network
    and at another); tests k and k+2 go together. Then the pulses, with geo rows, if any. Each
    directory is named after its run id."""
    daily = [
        daily_test(tmp_path / run_id("daily", n), n, ips=300, repeats=100, provider=network(n))
        for n in range(tests)
    ]
    groups = [
        Group([daily[n], daily[n + 2]], networks=frozenset({network(n), network(n + 2)}))
        for n in (0, 1, 4, 5)
        if n + 2 < tests
    ]
    found = [
        pulse(tmp_path / run_id("pulse", n), n, provider=f"net-{n % 3}") for n in range(pulses)
    ]
    return groups + ([Group(found, networks=frozenset(map(network, range(6))))] if found else [])


def test_groups_store_the_same_rows_over_1_4_and_20_connections(
    databases, database_template, tmp_path
):
    groups = hour(tmp_path)
    stored = {}
    for n in (1, 4, 20):
        database = databases(template=database_template)
        outcomes = storage_parallel.store(groups, database, connections=n)
        assert [o.error for o in outcomes] == [None] * len(groups)
        # The day's partitions are created before any import locks a table: no deadlock.
        assert sum(o.retries for o in outcomes) == 0
        with connect(database) as conn:
            stored[n] = dump(conn)
            ips = conn.execute("SELECT count(*),count(DISTINCT ip) FROM ip_addresses").fetchone()
        assert ips[0] == ips[1] > 0  # no IP twice
    assert stored[4] == stored[1] and stored[20] == stored[1]
    assert len(stored[1]["provider_overlap"]) == 3 and len(stored[1]["product_ips"]) > 2000
    serial = databases(template=database_template)
    for directory in [d for group in groups for d in group.directories]:
        storage.ingest(directory, serial)
    with connect(serial) as conn:
        assert dump(conn) == stored[20]  # and what importing them one at a time stores


def shapes(tracer) -> list[tuple[str, int]]:
    return sorted((row["statement"], row["calls"]) for row in tracer.summary()["shapes"])


def test_one_connection_runs_the_statements_of_ingest_many_and_estimate_runs(
    databases, calculated_template, tmp_path
):
    groups = hour(tmp_path, pulses=0)
    for group in groups:
        group.estimates = [(Path(d).name, "US", formula(US={})) for d in group.directories]
    serial, parallel = (databases(template=calculated_template) for _ in range(2))
    for database in (serial, parallel):
        seed(database)
    with measure.Tracer() as tracer:
        for group in groups:
            storage.ingest_many(group.directories, serial)
            compute.estimate_runs(serial, group.estimates)
    expected = shapes(tracer)
    with measure.Tracer() as tracer:
        outcomes = storage_parallel.store(groups, parallel, connections=1)
    assert shapes(tracer) == expected
    found = [(o.error, o.estimate_error, o.retries) for o in outcomes]
    assert found == [(None, None, 0)] * len(groups)
    with connect(serial) as one, connect(parallel) as other:
        assert dump(other) == dump(one)
        hours = "SELECT count(*) FROM estimated_hours"
        assert other.execute(hours).fetchone() == one.execute(hours).fetchone() != (0,)


def single(tmp_path, tests: int) -> list[Group]:
    """`tests` daily tests (as hour's), one a group."""
    return [
        Group(
            [
                daily_test(
                    tmp_path / run_id("daily", n), n, ips=300, repeats=100, provider=network(n)
                )
            ],
            networks=frozenset({network(n)}),
        )
        for n in range(tests)
    ]


@pytest.mark.parametrize("processes", [1, 4])
def test_a_stopped_store_resumes_to_the_rows_of_an_uninterrupted_one(
    databases, database_template, tmp_path, processes
):
    """Stopped (KeyboardInterrupt from on_commit) once a third of the groups committed: no more
    groups go out, those in flight finish, and the resumed store ends with the same rows."""
    groups = single(tmp_path, 12)
    whole, stopped = (databases(template=database_template) for _ in range(2))
    sizes = {"connections": 4, "processes": processes}
    storage_parallel.store(groups, whole, **sizes)
    committed = []

    def stop_after_four(group, outcome):
        committed.append([result["run_id"] for result in outcome.results])
        if len(committed) == 4:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        storage_parallel.store(groups, stopped, **sizes, on_commit=stop_after_four)
    with connect(stopped) as conn:
        before = {str(r) for (r,) in conn.execute("SELECT run_id FROM test_runs")}
    assert {r for ids in committed for r in ids} <= before and len(before) < 12
    outcomes = storage_parallel.store(groups, stopped, **sizes)
    imported = {r["run_id"] for o in outcomes for r in o.results if r["imported"]}
    assert imported == {r["run_id"] for o in outcomes for r in o.results} - before
    with connect(whole) as one, connect(stopped) as other:
        assert dump(other) == dump(one)


def test_a_failing_group_fails_alone_and_a_deadlock_is_retried(
    database, tmp_path, monkeypatch, capsys
):
    good = [Group([pulse(tmp_path / f"p{n}", n)], networks=frozenset({f"n{n}"})) for n in range(3)]
    bad = pulse(tmp_path / "p3", 3)
    path = bad / "summary.json"
    path.write_text(json.dumps(json.loads(path.read_text()) | {"status": "lost"}))
    real, raised = storage.ingest_many, []

    def deadlock_once(directories, credentials, **options):
        if directories == good[1].directories and not raised:
            raised.append(True)
            raise IngestBatchError([run_id("pulse", 1)]) from psycopg.errors.DeadlockDetected()
        return real(directories, credentials, **options)

    monkeypatch.setattr(storage, "ingest_many", deadlock_once)
    outcomes = storage_parallel.store([*good, Group([bad])], database, connections=4)
    assert [o.retries for o in outcomes] == [0, 1, 0, 0]
    assert [o.error for o in outcomes[:3]] == [None] * 3
    assert outcomes[3].error.run_ids == [run_id("pulse", 3)]
    assert isinstance(outcomes[3].error.__cause__, psycopg.errors.CheckViolation)
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM test_runs").fetchone()[0] == 3
    assert "over 4 connections" in capsys.readouterr().err


def overlap_in_two_orders(database, *, sort: bool) -> list[int]:
    """Two transactions each add one to the pairs (a,b) and (a,c), in opposite orders unless
    sorted, after each other's first update; the retries each took."""
    with connect(database) as conn:
        conn.execute("INSERT INTO providers VALUES ('a'),('b'),('c')")
        conn.execute("INSERT INTO provider_overlap VALUES ('a','b',1),('a','c',1)")
    pairs, ready = [("a", "b"), ("a", "c")], [threading.Event(), threading.Event()]
    retries = [0, 0]

    def side(n):
        own = sorted(pairs) if sort else pairs[::-1] if n else pairs
        pool, outcome = storage_parallel.Connections(database), storage_parallel.Outcome()

        def work(conn):
            with conn.transaction():
                conn.execute(OVERLAP, own[0])
                ready[n].set()
                ready[1 - n].wait(2)  # sorted, the other waits on this one's first pair
                conn.execute(OVERLAP, own[1])

        try:
            assert storage_parallel.retried(work, pool, outcome) == (None, None)
        finally:
            pool.close()
        retries[n] = outcome.retries

    threads = [threading.Thread(target=side, args=(n,)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    with connect(database) as conn:
        found = conn.execute("SELECT shared_ips FROM provider_overlap ORDER BY 1").fetchall()
    assert found == [(3,), (3,)]  # each pair counted once by each transaction
    return retries


def test_updates_in_opposite_orders_deadlock_and_are_retried_sorted_ones_do_not(
    databases, database_template
):
    unsorted = overlap_in_two_orders(databases(template=database_template), sort=False)
    assert sorted(unsorted) == [0, 1]  # one deadlocked, rolled back and went again
    assert overlap_in_two_orders(databases(template=database_template), sort=True) == [0, 0]


def test_the_caps_lower_n_and_never_split_a_group(database, tmp_path, monkeypatch, capsys):
    groups = [Group([pulse(tmp_path / f"p{n}", n) for n in range(k, k + 2)]) for k in (0, 2)]
    committed = []
    outcomes = storage_parallel.store(
        groups, database, connections=4, memory_budget=1, on_commit=lambda g, _: committed.append(g)
    )
    err = capsys.readouterr().err
    assert "warning: 1 connections, not 4" in err and "memory budget" in err
    assert "2 groups over 1 connections" in err
    assert committed == groups  # each whole, one after the other
    assert [[r["run_id"] for r in o.results] for o in outcomes] == [
        [run_id("pulse", 0), run_id("pulse", 1)],
        [run_id("pulse", 2), run_id("pulse", 3)],
    ]
    monkeypatch.setattr(storage_parallel, "RESERVE", 10**6)  # nothing left to spare
    storage_parallel.store(groups, database, connections=4)
    assert "warning: 1 connections, not 4: max_connections" in capsys.readouterr().err
    with pytest.raises(ValueError, match="at least one connection"):
        storage_parallel.store(groups, database, connections=0)
    assert storage_parallel.store([], database) == []


WAITING = (  # in this database only: other tests import at the same time
    "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted AND objsubid=2 "
    "AND classid::bigint=%s AND database=(SELECT oid FROM pg_database "
    "WHERE datname=current_database())"
)


def test_groups_of_two_networks_count_at_the_same_time(database, tmp_path):
    """While a group of net-a waits for its network's lock (taken elsewhere), a group of net-b
    goes through its own network step and the overlap step, and commits."""
    a, b = (  # pulses 0 and 5 share no IP: b never waits on a's rows
        Group([pulse(tmp_path / name, n, provider=name)], networks=frozenset({name}))
        for n, name in ((0, "net-a"), (5, "net-b"))
    )
    storage.ingest(pulse(tmp_path / "first", 2), database)  # the day's partitions
    order, lock = [], (storage.NETWORK_LOCK, "net-a")
    with connect(database) as blocker:
        blocker.autocommit = True
        blocker.execute("SELECT pg_advisory_lock(%s,hashtext(%s))", lock)
        release = threading.Timer(30, blocker.execute, ("SELECT pg_advisory_unlock_all()",))
        release.start()  # a stall fails the test instead of hanging it

        def committed(group, outcome):
            order.append(group.networks)
            if group is b:
                for _ in range(200):  # a reaches its network lock and waits there
                    if blocker.execute(WAITING, (storage.NETWORK_LOCK,)).fetchone()[0]:
                        break
                    time.sleep(0.05)
                assert blocker.execute(WAITING, (storage.NETWORK_LOCK,)).fetchone()[0] == 1
                blocker.execute("SELECT pg_advisory_unlock(%s,hashtext(%s))", lock)

        outcomes = storage_parallel.store([a, b], database, connections=2, on_commit=committed)
        release.cancel()
    assert order == [b.networks, a.networks]
    assert [o.error for o in outcomes] == [None, None]


def test_a_group_that_breaks_holding_its_locks_frees_them(database, tmp_path, monkeypatch):
    """An unexpected error after the locks are taken closes the group's connection, so the next
    group of its network (waiting for the same network lock) still finishes."""
    storage.ingest(pulse(tmp_path / "first", 2), database)  # the day's partitions
    a, b = (Group([pulse(tmp_path / f"p{n}", n, provider="net-a")]) for n in (0, 5))
    real = storage._scope_totals

    def broken(runs, scopes, new_ips):
        if runs[0].run_id == run_id("pulse", 0):
            raise RuntimeError("broken")
        return real(runs, scopes, new_ips)

    monkeypatch.setattr(storage, "_scope_totals", broken)
    with pytest.raises(RuntimeError, match="broken"):
        storage_parallel.store([a, b], database, connections=2)
    with connect(database) as conn:
        found = {str(r) for (r,) in conn.execute("SELECT run_id FROM test_runs")}
    assert run_id("pulse", 5) in found and run_id("pulse", 0) not in found


def test_the_import_locks_are_no_other_lock():
    from proxy_index import detect
    from proxy_index.archive import purge
    from proxy_index.calculated import rollups
    from proxy_index.orchestrator import batches

    assert storage.INGEST_LOCK == purge.INGEST_LOCK
    others = {detect.LOCK, rollups.LOCK, batches.PLAN_LOCK, storage.INGEST_LOCK}
    assert storage.OVERLAP_LOCK not in others and len(others) == 3
    assert storage.NETWORK_LOCK != storage.RUN_LOCK  # the two-integer keys


def test_groups_of_one_network_are_not_handed_out_together():
    parts = [Group([], networks=frozenset(n)) for n in ("a", "a", "b", "ab", "c")]
    running = [0]  # a part of network a is in flight
    assert storage_parallel._next([1, 2, 3, 4], parts, running) == 2
    running.append(2)
    assert storage_parallel._next([1, 3, 4], parts, running) == 4
    running.append(4)
    assert storage_parallel._next([1, 3], parts, running) == 1  # none free: the first


def test_groups_store_the_same_rows_over_1_2_and_4_processes(
    databases, database_template, tmp_path
):
    groups = hour(tmp_path)
    stored = {}
    for processes in (1, 2, 4):
        database = databases(template=database_template)
        outcomes = storage_parallel.store(groups, database, connections=8, processes=processes)
        assert [o.error for o in outcomes] == [None] * len(groups)
        assert [len(o.results) for o in outcomes] == [len(g.directories) for g in groups]
        with connect(database) as conn:
            stored[processes] = dump(conn)
    assert stored[2] == stored[1] and stored[4] == stored[1]
    assert len(stored[1]["provider_overlap"]) == 3 and len(stored[1]["product_ips"]) > 2000


def test_a_group_failing_in_a_worker_process_fails_alone_named(database, tmp_path, capsys):
    good = [Group([pulse(tmp_path / f"p{n}", n)], networks=frozenset({f"n{n}"})) for n in range(3)]
    bad = pulse(tmp_path / "p3", 3)
    path = bad / "summary.json"
    path.write_text(json.dumps(json.loads(path.read_text()) | {"status": "lost"}))
    groups = [*good, Group([bad])]
    outcomes = storage_parallel.store(groups, database, connections=4, processes=2)
    assert [o.error for o in outcomes[:3]] == [None] * 3
    assert outcomes[3].error.run_ids == [run_id("pulse", 3)]
    assert isinstance(outcomes[3].error.__cause__, storage_parallel.WorkerError)
    assert "CheckViolation" in str(outcomes[3].error.__cause__)
    assert "over 4 connections, 2 processes" in capsys.readouterr().err
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM test_runs").fetchone()[0] == 3


def test_a_worker_serves_groups_until_none_its_errors_described(database, tmp_path, monkeypatch):
    """serve, in a thread here: each group's outcome comes back with its errors as text, which
    restored turns back into the IngestBatchError naming its run ids."""
    good, bad = (pulse(tmp_path / f"p{n}", n) for n in (0, 1))

    def no_formula(*_, **__):
        raise ValueError("no formula")

    monkeypatch.setattr(compute, "estimate_runs", no_formula)
    path = bad / "summary.json"
    path.write_text(json.dumps(json.loads(path.read_text()) | {"status": "lost"}))
    mine, theirs = multiprocessing.Pipe()
    worker = threading.Thread(target=storage_parallel._serve, args=(theirs, database, 2))
    worker.start()
    mine.send(("good", Group([good], estimates=[(good.name, "US", {"version": 0})])))
    mine.send(("bad", Group([bad])))
    found = {}
    for _ in range(2):
        key, outcome, errors = mine.recv()
        found[key] = storage_parallel._restored(outcome, errors)
    mine.send(None)
    worker.join(30)
    assert not worker.is_alive()
    assert found["good"].error is None and found["good"].results[0]["imported"]
    assert str(found["good"].estimate_error) == "ValueError: no formula"
    assert found["bad"].error.run_ids == [run_id("pulse", 1)]
    assert "CheckViolation" in str(found["bad"].error.__cause__)


def test_a_dead_worker_process_fails_its_groups_named_and_the_others_finish(
    database, tmp_path, capsys
):
    """net-a's group waits on its network lock (held here) in one process; that process is
    killed: its group fails naming its run, the other process's group commits."""
    a, b = (
        Group([pulse(tmp_path / run_id("pulse", n), n, provider=name)], networks=frozenset({name}))
        for n, name in ((0, "net-a"), (5, "net-b"))
    )
    storage.ingest(pulse(tmp_path / "first", 2), database)  # the day's partitions
    lock = (storage.NETWORK_LOCK, "net-a")
    options = storage_parallel.Options(connections=2, processes=2)
    with connect(database) as blocker:
        blocker.autocommit = True
        blocker.execute("SELECT pg_advisory_lock(%s,hashtext(%s))", lock)
        try:
            with storage_parallel.Pool(database, options) as pool:
                tickets = pool.submit([a, b])
                assert tickets[1].result(60).error is None
                for _ in range(200):  # a reaches its network lock and waits there
                    if blocker.execute(WAITING, (storage.NETWORK_LOCK,)).fetchone()[0]:
                        break
                    time.sleep(0.05)
                key = tuple(map(str, a.directories))
                pool.running[key].process.kill()
                outcomes = pool.wait([a, b], tickets)
        finally:
            blocker.execute("SELECT pg_advisory_unlock_all()")
    assert outcomes[0].error.run_ids == [run_id("pulse", 0)]
    assert "died" in str(outcomes[0].error.__cause__) and outcomes[1].error is None
    assert f"worker process died (-9): ['{run_id('pulse', 0)}']" in capsys.readouterr().err
    with connect(database) as conn:
        found = {str(r) for (r,) in conn.execute("SELECT run_id FROM test_runs")}
    assert run_id("pulse", 5) in found


def test_a_group_sent_to_a_process_that_died_waits_for_its_lane_s_dead_event(database, tmp_path):
    """A lane whose process died before the pool knew it: sending raises nothing in the
    dispatcher, and the "dead" event that follows fails the group with the lane's others."""
    events = queue.Queue()
    lane = storage_parallel.ProcessLane(database, 1, events)
    lane.process.kill()
    lane.process.join(30)
    assert events.get(timeout=30) == ("dead", lane)
    lane.send("key", Group([pulse(tmp_path / run_id("pulse", 0), 0)]))
    assert lane.running == {"key"}
    lane.close()


def test_an_open_pool_stores_groups_handed_ahead_once(database, tmp_path, monkeypatch):
    """A pool open over several stores: the groups handed ahead (submit) are what a later store
    of the same groups waits for, stored once; groups go out one at a time when the memory
    budget holds only one, though the connections would take more."""
    groups = single(tmp_path, 4)
    sent, real = [], storage_parallel.ThreadLane.send

    def send(self, key, group):
        sent.append(len(self.running))  # groups already in flight
        real(self, key, group)

    monkeypatch.setattr(storage_parallel.ThreadLane, "send", send)
    options = storage_parallel.Options(connections=4, memory_budget=1, processes=1)
    with storage_parallel.Pool(database, options) as pool:
        pool.submit(groups[2:])  # handed ahead of their store
        committed = []
        first = storage_parallel.store(groups[:2], database, pool=pool)
        later = storage_parallel.store(
            groups[2:], database, pool=pool, on_commit=lambda g, _: committed.append(g)
        )
        assert not pool.tickets  # waited for: a later submit would store them again
    assert sent == [0, 0, 0, 0]  # the groups handed ahead, stored once
    # Stored before their store waited: each one's on_commit then, in no set order.
    assert sorted(str(g.directories) for g in committed) == sorted(
        str(g.directories) for g in groups[2:]
    )
    assert [r["imported"] for o in first + later for r in o.results] == [True] * 4


def test_the_default_processes_and_the_reserve(database, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(storage_parallel, "PROCESSES", 7)
    monkeypatch.setattr(storage_parallel.os, "cpu_count", lambda: 4)
    assert [storage_parallel.default_processes(n) for n in (1, 2, 25)] == [1, 2, 3]
    monkeypatch.setattr(storage_parallel.os, "cpu_count", lambda: 32)
    assert storage_parallel.default_processes(25) == 7
    monkeypatch.setattr(storage_parallel.os, "cpu_count", lambda: None)
    assert storage_parallel.default_processes(25) == 1
    monkeypatch.setattr(storage_parallel, "RESERVE_SHARE", 1.0)  # all of max_connections
    assert storage_parallel._capped(database, 4) == 1
    with connect(database) as conn:
        limit = int(conn.execute("SHOW max_connections").fetchone()[0])
    err = capsys.readouterr().err
    assert f"warning: 1 connections, not 4: max_connections {limit}" in err
    assert f"{limit} kept free" in err
