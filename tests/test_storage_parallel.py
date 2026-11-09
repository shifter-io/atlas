"""INGEST-PARALLEL-1: an hour's groups stored over N connections at once store what storing them
one after the other stores; a deadlock is retried, a failing group fails alone, a stopped store
resumes to the same rows."""

from __future__ import annotations

import importlib
import json
import sys
import threading
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


def test_a_stopped_store_resumes_to_the_rows_of_an_uninterrupted_one(
    databases, database_template, tmp_path
):
    groups = hour(tmp_path, tests=8, pulses=0)
    assert len(groups) == 4
    whole, stopped = (databases(template=database_template) for _ in range(2))
    storage_parallel.store(groups, whole, connections=2)
    committed = []

    def stop_after_two(group, outcome):
        committed.append([result["run_id"] for result in outcome.results])
        if len(committed) == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        storage_parallel.store(groups, stopped, connections=2, on_commit=stop_after_two)
    with connect(stopped) as conn:
        before = {str(r) for (r,) in conn.execute("SELECT run_id FROM test_runs")}
    assert {r for ids in committed for r in ids} <= before and len(before) < 8
    outcomes = storage_parallel.store(groups, stopped, connections=2)
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


def test_the_caps_split_groups_and_spare_connections(database, tmp_path, monkeypatch, capsys):
    groups = [Group([pulse(tmp_path / f"p{n}", n) for n in range(k, k + 2)]) for k in (0, 2)]
    monkeypatch.setattr(storage_parallel, "RESERVE", 10**6)  # nothing left to spare
    parts = []
    outcomes = storage_parallel.store(
        groups, database, connections=4, memory_budget=1, on_commit=lambda p, _: parts.append(p)
    )
    err = capsys.readouterr().err
    assert "warning: 1 connections, not 4" in err and "at most 1 runs a group" in err
    assert len(parts) == 4  # each run its own transaction, as the budget allows
    assert [[r["run_id"] for r in o.results] for o in outcomes] == [
        [run_id("pulse", 0), run_id("pulse", 1)],
        [run_id("pulse", 2), run_id("pulse", 3)],
    ]
    with pytest.raises(ValueError, match="at least one connection"):
        storage_parallel.store(groups, database, connections=0)
    assert storage_parallel.store([], database) == []


def test_the_import_locks_are_no_other_lock():
    from proxy_index import detect
    from proxy_index.archive import purge
    from proxy_index.calculated import rollups
    from proxy_index.orchestrator import batches

    assert storage.INGEST_LOCK == purge.INGEST_LOCK
    others = {detect.LOCK, rollups.LOCK, batches.PLAN_LOCK, storage.INGEST_LOCK}
    assert storage.OVERLAP_LOCK not in others and len(others) == 3


def test_groups_of_one_network_are_not_handed_out_together():
    parts = [Group([], networks=frozenset(n)) for n in ("a", "a", "b", "ab", "c")]
    running = {"f0": 0}  # a part of network a is in flight
    assert storage_parallel._next([1, 2, 3, 4], parts, running) == 2
    running["f2"] = 2
    assert storage_parallel._next([1, 3, 4], parts, running) == 4
    running["f4"] = 4
    assert storage_parallel._next([1, 3], parts, running) == 1  # none free: the first
