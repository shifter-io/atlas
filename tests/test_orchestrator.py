"""Orchestrator: census/pulse planning, the barrier, the node API, jobs and the scheduler."""

import asyncio
import io
import json
import tarfile
from datetime import UTC, datetime, time, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer
from helpers import FakeTransport
from test_storage import measurement

from proxy_index.orchestrator import batches
from proxy_index.orchestrator.app import build_app
from proxy_index.orchestrator.archive import ArchiveError, pack, unpack
from proxy_index.orchestrator.db import Database, put_setting
from proxy_index.orchestrator.jobs import calculate, detect, retention
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.orchestrator.scheduler import Scheduler, Task
from proxy_index.regions import REGION_IDS
from proxy_index.storage import connect, ingest, seed

pytest_plugins = ["test_storage"]

NOW = datetime(2026, 10, 1, 11, 59, 30, tzinfo=UTC)
FRA1, LON1 = REGION_IDS["fra1"], REGION_IDS["lon1"]


def prepare(config, countries=("DE",), **times):
    """Seeded reference data with only `countries` enabled; times maps code -> census time."""
    seed(config)
    with connect(config) as conn:
        conn.execute("UPDATE countries SET enabled = country_code = ANY(%s)", (list(countries),))
        for code, value in times.items():
            conn.execute("UPDATE countries SET census_time=%s WHERE country_code=%s", (value, code))


def query(config, statement, params=None):
    with connect(config) as conn:
        return conn.execute(statement, params).fetchall()


def product_id(config, provider, product="res"):
    return query(
        config, "SELECT product_id FROM products WHERE provider=%s AND name=%s", (provider, product)
    )[0][0]


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()


@pytest.fixture
async def client(database, db):
    app = build_app(database, with_scheduler=False, db=db)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client
    await test_client.close()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# --- planning -------------------------------------------------------------------------------


async def test_census_plan_due_slots_region_routing_and_no_node(database, db):
    prepare(database, ("DE", "FR", "AT"), DE=time(12), FR=time(12), AT=time(12, 30))
    alpha = issue_token(database, provider="alpha", product="res", region="fra1")
    beta = issue_token(database, provider="beta", product="res", region="prod-lon1-beta")
    assert beta["region"] == "lon1"

    result = await batches.census_plan(db, NOW)
    assert result == {"created": 2, "skipped": 0, "deferred": 0, "no_node": 2}
    rows = query(
        database,
        "SELECT batch_id,country_code,region_id,target_id,scheduled_for,start_at,status "
        "FROM test_batches ORDER BY country_code",
    )
    slot = datetime(2026, 10, 1, 12, tzinfo=UTC)
    # Only the census target, and each country is tested from its own region.
    assert [r[1:] for r in rows] == [
        ("DE", FRA1, "generic_http", slot, slot + timedelta(seconds=30), "scheduled"),
        ("FR", LON1, "generic_http", slot, slot + timedelta(seconds=30), "scheduled"),
    ]
    runs = query(
        database,
        "SELECT b.country_code,p.provider,r.node_id,r.status,r.stop_reason,r.error "
        "FROM batch_runs r JOIN test_batches b USING(batch_id) JOIN products p USING(product_id) "
        "ORDER BY 1,2",
    )
    assert runs == [
        ("DE", "alpha", alpha["node_id"], "queued", None, None),
        ("DE", "beta", None, "failed", "no_node", "no_node"),
        ("FR", "alpha", None, "failed", "no_node", "no_node"),
        ("FR", "beta", beta["node_id"], "queued", None, None),
    ]
    alerts = query(database, "SELECT dedupe_key,occurrences FROM alerts ORDER BY 1")
    assert alerts == sorted(
        [
            (f"no_node:{product_id(database, 'alpha')}:{LON1}", 1),
            (f"no_node:{product_id(database, 'beta')}:{FRA1}", 1),
        ]
    )
    events = query(database, "SELECT kind,region_id,signal,data->>'kind' FROM test_events")
    assert sorted(events) == sorted(
        [("no_node", FRA1, -1, "census"), ("no_node", LON1, -1, "census")]
    )

    # Idempotent: the same tick again creates nothing and writes no second event.
    assert (await batches.census_plan(db, NOW + timedelta(seconds=20)))["created"] == 0
    assert len(query(database, "SELECT 1 FROM test_batches")) == 2
    assert len(query(database, "SELECT 1 FROM test_events")) == 2

    # Half an hour on, Austria's slot is due and nothing else.
    later = await batches.census_plan(db, NOW + timedelta(minutes=30))
    assert later["created"] == 1
    newer = query(
        database, "SELECT country_code FROM test_batches WHERE batch_id>%s", (rows[-1][0],)
    )
    assert newer == [("AT",)]
    # Beta still has no fra1 node: the open alert counts it again, the tape stays quiet.
    assert query(
        database, "SELECT occurrences FROM alerts WHERE dedupe_key LIKE %s", (f"no_node:%:{FRA1}",)
    ) == [(2,)]
    assert len(query(database, "SELECT 1 FROM test_events")) == 2


async def test_census_plan_wraps_midnight_and_skips_without_nodes(database, db):
    prepare(database, ("DE",), DE=time(0, 0, 20))
    evening = datetime(2026, 10, 1, 23, 59, 50, tzinfo=UTC)
    assert (await batches.census_plan(db, evening))["reason"] == "no registered nodes"
    issue_token(database, provider="alpha", product="res", region="fra1")
    assert (await batches.census_plan(db, evening))["created"] == 1
    assert query(database, "SELECT scheduled_for FROM test_batches") == [
        (datetime(2026, 10, 2, 0, 0, 20, tzinfo=UTC),)
    ]


async def test_census_overlap_is_skipped(database, db):
    prepare(database, ("DE",), DE=time(12))
    issue_token(database, provider="alpha", product="res", region="fra1")
    await batches.census_plan(db, NOW)
    # The operator moves the slot while the first census is still open.
    with connect(database) as conn:
        conn.execute("UPDATE countries SET census_time='12:10' WHERE country_code='DE'")
    # Before the new start_at (12:10:30) the first census may still finish: deferred.
    assert await batches.census_plan(db, NOW + timedelta(minutes=10)) == {
        "created": 0,
        "skipped": 0,
        "deferred": 1,
        "no_node": 0,
    }
    assert len(query(database, "SELECT 1 FROM test_batches")) == 1
    # Still open at start_at: skipped.
    assert await batches.census_plan(db, NOW + timedelta(minutes=11)) == {
        "created": 0,
        "skipped": 1,
        "deferred": 0,
        "no_node": 0,
    }
    rows = query(database, "SELECT status,finished_at IS NOT NULL FROM test_batches ORDER BY 1")
    assert rows == [("scheduled", False), ("skipped", True)]
    events = query(database, "SELECT kind,country_code,data->>'reason' FROM test_events")
    assert events == [("census_skipped", "DE", "previous census still scheduled")]
    skipped = query(database, "SELECT batch_id FROM test_batches WHERE status='skipped'")[0][0]
    assert query(database, "SELECT 1 FROM batch_runs WHERE batch_id=%s", (skipped,)) == []


async def test_pulse_plan_intervals_new_targets_and_idempotence(database, db):
    prepare(database, ("DE", "AT"))
    issue_token(database, provider="alpha", product="res", region="fra1")
    with connect(database) as conn:
        conn.execute("UPDATE targets SET pulse_interval_minutes=60 WHERE target_id='generic_https'")

    def slots():
        return query(
            database,
            "SELECT target_id,scheduled_for,count(*),min(start_at),max(start_at) "
            "FROM test_batches WHERE kind='pulse' GROUP BY 1,2 ORDER BY 2,1",
        )

    at = datetime(2026, 10, 1, 12, 31, tzinfo=UTC)

    def at15(minutes, seconds=0):  # at + pulse.lead_seconds (15 s) + the given time
        return at + timedelta(minutes=minutes, seconds=15 + seconds)

    first = await batches.pulse_plan(db, at)
    # One load wave, in fra1's reference country (its default, DE: no history yet).
    assert first == {
        "targets": ["generic_http", "generic_https"],
        "created": 4,
        "load": 1,
        "no_node": 0,
    }
    # Each region's countries are spread over the slot, one at a time: generic_http's slot
    # opened on time (AT, DE 15 minutes apart, none before now + lead); generic_https's half an
    # hour late, so over the rest of it, half a step after generic_http's.
    assert slots() == [
        ("generic_https", datetime(2026, 10, 1, 12, tzinfo=UTC), 2, at15(7, 15), at15(21, 45)),
        ("generic_http", datetime(2026, 10, 1, 12, 30, tzinfo=UTC), 2, at15(0), at15(14)),
    ]
    assert (await batches.pulse_plan(db, at))["created"] == 0
    assert (await batches.pulse_plan(db, at + timedelta(minutes=14)))["created"] == 0
    top = datetime(2026, 10, 1, 13, tzinfo=UTC)
    assert (await batches.pulse_plan(db, top))["targets"] == ["generic_http", "generic_https"]
    # A target added to the table joins at its own interval with no restart.
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO targets(target_id,url,pulse_interval_minutes,pulse_requests) "
            "VALUES ('probe_x','http://example.test/',15,10)"
        )
    nxt = await batches.pulse_plan(db, top + timedelta(minutes=16))
    assert nxt["targets"] == ["probe_x"]
    assert (await batches.pulse_plan(db, top + timedelta(minutes=31)))["targets"] == [
        "probe_x",
        "generic_http",
    ]
    assert len(slots()) == 7
    runs = query(database, "SELECT DISTINCT status FROM batch_runs")
    assert runs == [("queued",)]


async def test_pulse_slots_add_a_load_wave_per_region(database, db, client):
    """Every pulse slot of the census target opens one 'load' batch per runner region, in the
    region's reference country of the day (here the defaults: no history yet), with the same
    scheduled_for, load.delay_seconds after the pulses; its runs fire census_concurrency
    requests at census concurrency, counted like a pulse, and send only the country (the
    node's proxy factory)."""
    prepare(database, ("DE", "AT", "GB"))
    a = issue_token(database, provider="alpha", product="res", region="fra1")
    # Inside the slot's grace, so the slot is spread whole whatever the wall clock says.
    at = datetime(2026, 10, 1, 12, 0, 30, tzinfo=UTC)
    result = await batches.pulse_plan(db, at)
    assert (result["created"], result["load"]) == (6, 2)
    assert query(
        database,
        "SELECT r.slug,l.country_code,l.basis,l.p50_ms,l.candidates FROM load_references l "
        "JOIN runner_regions r USING(region_id) ORDER BY 1",
    ) == [("fra1", "DE", "default", None, 0), ("lon1", "GB", "default", None, 0)]
    loads = query(
        database,
        "SELECT l.country_code,l.target_id,l.scheduled_for=p.scheduled_for,"
        "l.start_at-(SELECT min(f.start_at) FROM test_batches f WHERE f.kind='pulse' "
        "AND f.region_id=l.region_id AND f.target_id=l.target_id) "
        "FROM test_batches l JOIN test_batches p ON p.kind='pulse' "
        "AND p.country_code=l.country_code AND p.target_id=l.target_id WHERE l.kind='load' "
        "ORDER BY 1",
    )
    # Same slot; they start a minute after the region's first pulse so it stays light.
    assert loads == [
        ("DE", "generic_http", True, timedelta(seconds=60)),
        ("GB", "generic_http", True, timedelta(seconds=60)),
    ]
    assert (await batches.pulse_plan(db, at))["load"] == 0  # idempotent
    work = await (await client.get("/v1/work", headers=bearer(a["token"]))).json()
    (load,) = [w for w in work["assignments"] if w["kind"] == "load"]  # GB: no node in lon1
    assert (load["country_code"], load["target_id"]) == ("DE", "generic_http")
    assert (load["concurrency"], load["requests"], load["stop_rule"]) == (1000, 1000, "count")
    assert load["min_sample"] is None and load["stall_seconds"] is None
    pulse = next(w for w in work["assignments"] if w["kind"] == "pulse")
    assert (pulse["concurrency"], pulse["requests"]) == (25, 100)
    # load.enabled switches the wave off.
    await set_setting(db, "load.enabled", False)
    later = await batches.pulse_plan(db, at + timedelta(hours=1))
    assert later["load"] == 0 and later["created"] in (0, 6)
    with pytest.raises(ValueError, match="two-letter"):
        batches._countries({"fra1": "Germany"}, "load.reference_defaults")


class SlowTransport(FakeTransport):
    async def execute(self, target, *, context=None):
        from proxy_index.models import Outcome

        return Outcome(30.2, ip="8.8.4.4", status=200, response_body_bytes=8)


async def pulse_history(database, tmp_path, country, at, transport):
    """An ingested census-target pulse of alpha/res in `country`, scheduled at `at`."""
    directory = await measurement(
        tmp_path,
        provider="alpha",
        product="res",
        requested_country=country.lower(),
        transport_factory=transport,
    )
    with connect(database) as conn:
        batch_id = conn.execute(
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at,status) SELECT 'pulse',%s,region_id,'generic_http',%s,%s,'done' "
            "FROM countries WHERE country_code=%s RETURNING batch_id",
            (country, at, at, country),
        ).fetchone()[0]
    key = ingest(directory, database, batch_id=batch_id)["run_key"]
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,status,started_at,finished_at,run_key,"
            "requests,successes) SELECT %s,product_id,'done',%s,%s,%s,3,3 FROM products "
            "WHERE provider='alpha' AND name='res'",
            (batch_id, at, at + timedelta(minutes=1), key),
        )
    return key


async def test_the_reference_is_the_fastest_clean_country_of_the_region(database, db, tmp_path):
    prepare(database, ("DE", "AT"))
    issue_token(database, provider="alpha", product="res", region="fra1")
    await set_setting(db, "load.reference_min_successes", 1)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    await pulse_history(database, tmp_path, "DE", yesterday, SlowTransport)
    fast = await pulse_history(database, tmp_path, "AT", yesterday, FakeTransport)
    # The runs ran just now (scheduled yesterday): choose for tomorrow, whose trailing week
    # holds them all.
    today = datetime.now(UTC) + timedelta(days=1)
    async with db.transaction() as conn:
        (choice,) = await batches.load_references(conn, today)
    assert (choice["country_code"], choice["basis"], choice["p50_ms"]) == ("AT", "fastest", 11)
    assert (choice["successes"], choice["candidates"]) == (3, 2)
    # Chosen once a day: a new override waits for tomorrow.
    await set_setting(db, "load.reference_overrides", {"fra1": "de"})
    async with db.transaction() as conn:
        assert (await batches.load_references(conn, today))[0]["country_code"] == "AT"
        tomorrow = await batches.load_references(conn, today + timedelta(days=1))
    assert (tomorrow[0]["country_code"], tomorrow[0]["basis"]) == ("DE", "override")
    # A pulse that ran during a census of the same product (another country) never counts.
    (run,) = query(
        database, "SELECT started_at,finished_at FROM test_runs WHERE run_key=%s", (fast,)
    )
    with connect(database) as conn:
        census = conn.execute(
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at,status) SELECT 'census','GB',region_id,'generic_http',%s,%s,'done' "
            "FROM countries WHERE country_code='GB' RETURNING batch_id",
            (yesterday, yesterday),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,status,started_at,finished_at) "
            "SELECT %s,product_id,'done',%s,%s FROM products WHERE provider='alpha'",
            (census, run[0], run[1]),
        )
    day_start = today.replace(hour=0, minute=0, second=0, microsecond=0)
    async with db.connection() as conn:
        stats = await batches._reference_latency(conn, day_start, 7)
    assert stats == {"DE": {"p50": 31, "successes": 3}}


async def finish_pulse(db, node, batch_id, successes, now):
    summary = {
        "status": "completed",
        "completed": 100,
        "successes": successes,
        "stop_reason": "requests",
    }
    return await batches.finish_run(db, node, batch_id, summary, None, now)


@pytest.mark.parametrize("spoiled", ["no_success", "stalled", "failed"])
async def test_a_spoiled_census_reruns_once_a_day_when_a_pulse_gets_through(database, db, spoiled):
    """A census that ended because the provider was down is rerun for that product alone at the
    first pulse that sees it up again in the country, once per UTC day. A census our side
    failed (here the runner failed) is not the provider's and is not rerun."""
    prepare(database, ("DE",))
    a = issue_token(database, provider="alpha", product="res", region="fra1")
    b = issue_token(database, provider="beta", product="res", region="fra1")
    node = {"node_id": a["node_id"]}
    alpha, beta = product_id(database, "alpha"), product_id(database, "beta")
    day = datetime.now(UTC).replace(hour=3, minute=0, second=0, microsecond=0)
    with connect(database) as conn:
        census = conn.execute(
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at,status,finished_at) VALUES ('census','DE',%s,'generic_http',%s,%s,'done',%s) "
            "RETURNING batch_id",
            (FRA1, day, day, day + timedelta(minutes=20)),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,node_id,status,stop_reason,error,"
            "successes,finished_at) VALUES (%s,%s,%s,%s,%s,%s,0,%s)",
            (
                census,
                alpha,
                a["node_id"],
                "failed" if spoiled == "failed" else "done",
                None if spoiled == "failed" else spoiled,
                "run failed" if spoiled == "failed" else None,
                day + timedelta(minutes=20),
            ),
        )
        conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,node_id,status,stop_reason,successes,"
            "finished_at) VALUES (%s,%s,%s,'done','unique_ratio',900,%s)",
            (census, beta, b["node_id"], day + timedelta(minutes=20)),
        )
    pulses = []
    for i in range(3):
        at = day + timedelta(hours=1, minutes=30 * i)
        await batches.pulse_plan(db, at)
        pulses.append(
            query(
                database,
                "SELECT batch_id FROM test_batches WHERE kind='pulse' AND target_id='generic_http' "
                "AND scheduled_for<=%s ORDER BY scheduled_for DESC LIMIT 1",
                (at,),
            )[0][0]
        )
        await batches.assignments(db, node, at + timedelta(minutes=1))
    # Still down: no rerun. Then up: one rerun for alpha alone. Up again: nothing more.
    await finish_pulse(db, node, pulses[0], 0, day + timedelta(hours=1, minutes=1))
    assert query(database, "SELECT count(*) FROM test_batches WHERE retry_of IS NOT NULL") == [(0,)]
    up = day + timedelta(hours=1, minutes=31)
    await finish_pulse(db, node, pulses[1], 97, up)
    if spoiled == "failed":
        assert query(database, "SELECT count(*) FROM test_batches WHERE retry_of IS NOT NULL") == [
            (0,)
        ]
        return
    retries = query(
        database,
        "SELECT b.batch_id,b.retry_of,b.country_code,b.target_id,b.start_at-b.scheduled_for,"
        "array_agg(br.product_id),array_agg(br.status) FROM test_batches b JOIN batch_runs br "
        "USING(batch_id) WHERE b.retry_of IS NOT NULL GROUP BY 1",
    )
    assert [r[1:] for r in retries] == [
        (census, "DE", "generic_http", timedelta(seconds=5), [alpha], ["queued"])
    ]
    events = query(
        database, "SELECT kind,product_id,data FROM test_events WHERE kind='census_retry'"
    )
    assert [(e[0], e[1], e[2]["retry_of"]) for e in events] == [("census_retry", alpha, census)]
    # The rerun is spoiled too; another pulse gets through the same day: still only one rerun.
    with connect(database) as conn:
        conn.execute(
            "UPDATE batch_runs SET status='done',stop_reason='no_success',finished_at=%s "
            "WHERE batch_id=%s",
            (up + timedelta(minutes=12), retries[0][0]),
        )
        conn.execute("UPDATE test_batches SET status='done' WHERE batch_id=%s", (retries[0][0],))
    await finish_pulse(db, node, pulses[2], 100, day + timedelta(hours=2, minutes=1))
    assert query(database, "SELECT count(*) FROM test_batches WHERE retry_of IS NOT NULL") == [(1,)]


# --- barrier --------------------------------------------------------------------------------


async def test_barrier_waits_for_every_product(database, db):
    prepare(database, ("DE",), DE=time(12))
    a = issue_token(database, provider="alpha", product="res", region="fra1")
    b = issue_token(database, provider="beta", product="res", region="fra1")
    await batches.census_plan(db, NOW)
    batch_id = query(database, "SELECT batch_id FROM test_batches")[0][0]
    node_a = {"node_id": a["node_id"]}
    node_b = {"node_id": b["node_id"]}
    start = NOW + timedelta(seconds=60)
    work = await batches.assignments(db, node_a, start)
    assert [w["batch_id"] for w in work["assignments"]] == [batch_id]
    await batches.assignments(db, node_b, start)
    assert await batches.progress(db, node_a, batch_id, {"requests": 0}, start) == {"stop": False}
    assert query(database, "SELECT status,started_at FROM test_batches") == [("running", start)]
    # census_started is per product, written when that product's run starts.
    alpha, beta = product_id(database, "alpha"), product_id(database, "beta")
    assert query(database, "SELECT kind,product_id,data FROM test_events") == [
        ("census_started", alpha, {"region": "fra1", "start_at": "2026-10-01T12:00:30+00:00"})
    ]
    await batches.progress(
        db, node_a, batch_id, {"requests": 40, "successes": 30, "unique_ips": 20}, start
    )
    assert await batches.fail_run(db, node_a, batch_id, "boom", start) == "failed"
    assert query(database, "SELECT status FROM test_batches") == [("running",)]
    assert query(
        database,
        "SELECT requests,unique_ips,error FROM batch_runs WHERE node_id=%s",
        (a["node_id"],),
    ) == [(40, 20, "boom")]
    finished = (
        "SELECT product_id,signal,severity,data FROM test_events "
        "WHERE kind='census_finished' ORDER BY event_id"
    )
    ((product, signal, severity, data),) = query(database, finished)
    assert (product, signal, severity) == (alpha, -1, "minor")
    assert data == {
        "region": "fra1",
        "requests": 40,
        "successes": 30,
        "unique_ips": 20,
        "success_rate": 0.75,
        "duration_s": 0.0,
        "stop_reason": "error",
        "status": "failed",
        "bytes_up": 0,
        "bytes_down": 0,
        "error": "boom",
    }
    # Every run is terminal: the batch closes as failed; beta never started.
    await batches.fail_run(db, node_b, batch_id, "boom", start)
    assert query(database, "SELECT status,finished_at FROM test_batches") == [("failed", start)]
    rows = query(database, finished)
    assert [(r[0], r[1], r[3]["duration_s"]) for r in rows] == [(alpha, -1, 0.0), (beta, -1, None)]
    assert query(database, "SELECT count(*) FROM test_events") == [(3,)]
    # A terminal run cannot be failed twice.
    assert await batches.fail_run(db, node_b, batch_id, "again", start) == "failed"


async def test_reaper_fails_unstarted_and_silent_runs(database, db):
    prepare(database, ("DE",), DE=time(12))
    a = issue_token(database, provider="alpha", product="res", region="fra1")
    b = issue_token(database, provider="beta", product="res", region="fra1")
    await batches.census_plan(db, NOW)
    batch_id = query(database, "SELECT batch_id FROM test_batches")[0][0]
    start = NOW + timedelta(seconds=30)
    await batches.assignments(db, {"node_id": a["node_id"]}, start)
    await batches.progress(db, {"node_id": a["node_id"]}, batch_id, {"requests": 1}, start)
    assert (await batches.reap(db, start + timedelta(seconds=60)))["failed"] == {}
    # Beta never started; alpha went silent.
    result = await batches.reap(db, start + timedelta(hours=1))
    assert result == {"failed": {"not_started": 1, "stale": 1}, "closed": 1}
    reaped = query(
        database, "SELECT kind,signal,data->>'status' FROM test_events WHERE kind='census_finished'"
    )
    assert reaped == [("census_finished", -1, "failed")] * 2
    assert query(database, "SELECT node_id,error FROM batch_runs ORDER BY node_id") == [
        (a["node_id"], "stale"),
        (b["node_id"], "not_started"),
    ]
    assert query(database, "SELECT status FROM test_batches") == [("failed",)]


async def test_nodes_go_offline_and_come_back(database, db, client):
    prepare(database)
    a = issue_token(database, provider="alpha", product="res", region="fra1")
    response = await client.post(
        "/v1/heartbeat",
        json={"open_conns": 3, "req_per_s": 1.5, "rss_mb": 80},
        headers=bearer(a["token"]),
    )
    assert response.status == 200
    assert query(database, "SELECT status,open_conns,req_per_s,rss_mb FROM nodes") == [
        ("online", 3, 1.5, 80)
    ]
    assert (await batches.mark_offline(db))["offline"] == []
    gone = await batches.mark_offline(db, datetime.now(UTC) + timedelta(seconds=61))
    assert gone == {"offline": [a["node_id"]]}
    assert query(database, "SELECT kind,signal FROM test_events") == [("node_offline", -1)]
    assert query(database, "SELECT acknowledged_at IS NULL FROM alerts") == [(True,)]
    await client.post("/v1/heartbeat", json={}, headers=bearer(a["token"]))
    assert query(database, "SELECT acknowledged_at IS NULL FROM alerts") == [(False,)]
    bad = await client.post("/v1/heartbeat", json={"rss_mb": -1}, headers=bearer(a["token"]))
    assert bad.status == 400


# --- node API -------------------------------------------------------------------------------


async def test_api_requires_a_node_token_and_matching_registration(database, client):
    prepare(database)
    a = issue_token(database, provider="alpha", product="res", region="fra1")
    assert (await client.get("/health")).status == 200
    assert (await client.get("/v1/work")).status == 401
    assert (await client.get("/v1/work", headers=bearer("nope"))).status == 401
    assert (await client.get("/v1/work", headers={"Authorization": "Basic x"})).status == 401
    register = {"provider": "alpha", "product": "res", "region": "lon1", "version": "1"}
    wrong = await client.post("/v1/register", json=register, headers=bearer(a["token"]))
    assert wrong.status == 403
    other = await client.post(
        "/v1/register",
        json=register | {"region": "fra1", "product": "dc"},
        headers=bearer(a["token"]),
    )
    assert other.status == 403
    ok = await client.post(
        "/v1/register",
        json={"provider": "alpha", "product": "res", "host": "prod-fra1-alpha-1", "version": "9"},
        headers=bearer(a["token"]),
    )
    assert await ok.json() == {"node_id": a["node_id"], "region": "fra1"}
    assert query(database, "SELECT status,host,version FROM nodes") == [
        ("online", "prod-fra1-alpha-1", "9")
    ]
    # Rotation invalidates the old token.
    rotated = issue_token(database, provider="alpha", product="res", region="fra1")
    assert rotated["node_id"] == a["node_id"] and rotated["rotated"]
    assert (await client.get("/v1/work", headers=bearer(a["token"]))).status == 401
    assert (await client.get("/v1/work", headers=bearer(rotated["token"]))).status == 200


async def test_work_progress_and_kill(database, db, client):
    now = datetime.now(UTC)
    prepare(database, ("DE",), DE=(now + timedelta(seconds=5)).time())
    a = issue_token(database, provider="alpha", product="res", region="fra1")
    await set_setting(db, "census.min_sample", 50)
    await batches.census_plan(db, now)
    batch_id = query(database, "SELECT batch_id FROM test_batches")[0][0]
    headers = bearer(a["token"])
    work = await (await client.get("/v1/work", headers=headers)).json()
    assert work["kill"] == []
    (assignment,) = work["assignments"]
    start_at = datetime.fromisoformat(assignment.pop("start_at"))
    assert start_at == query(database, "SELECT start_at FROM test_batches")[0][0]
    assert assignment == {
        "batch_id": batch_id,
        "kind": "census",
        "product_id": product_id(database, "alpha"),
        "provider": "alpha",
        "product": "res",
        "country_code": "DE",
        "region": "fra1",
        "target_id": "generic_http",
        "target_url": "http://ip-info.com/myip",
        "timeout_ms": 15000,
        "concurrency": 1000,
        "stop_rule": "unique_ratio",
        "requests": None,
        "min_sample": 50,
        "stall_seconds": 600,
    }
    assert query(database, "SELECT status,started_at FROM batch_runs") == [("running", None)]
    # Not started yet, so a restarted node is handed it again.
    again = await (await client.get("/v1/work", headers=headers)).json()
    assert [w["batch_id"] for w in again["assignments"]] == [batch_id]

    progress = {
        "batch_id": batch_id,
        "counters": {"requests": 10, "successes": 9, "unique_ips": 9, "bytes_up": 100},
    }
    reply = await client.post("/v1/progress", json=progress, headers=headers)
    assert await reply.json() == {"stop": False}
    assert query(database, "SELECT requests,successes,unique_ips,bytes_up FROM batch_runs") == [
        (10, 9, 9, 100)
    ]
    assert (await (await client.get("/v1/work", headers=headers)).json())["assignments"] == []
    bad = await client.post(
        "/v1/progress", json={"batch_id": batch_id, "counters": {"requests": "x"}}, headers=headers
    )
    assert bad.status == 400
    missing = await client.post(
        "/v1/progress", json={"batch_id": 999999, "counters": {}}, headers=headers
    )
    assert missing.status == 404

    # The operator kills the batch: progress says stop and /v1/work lists it.
    with connect(database) as conn:
        conn.execute("UPDATE test_batches SET status='killed',finished_at=now()")
        conn.execute("UPDATE batch_runs SET status='killed',finished_at=now()")
    reply = await client.post("/v1/progress", json=progress, headers=headers)
    assert await reply.json() == {"stop": True}
    assert (await (await client.get("/v1/work", headers=headers)).json())["kill"] == [batch_id]
    # Results of a killed run are discarded.
    late = await client.post(f"/v1/results?batch_id={batch_id}", data=b"x", headers=headers)
    assert await late.json() == {"ok": True, "ingested": False, "status": "killed"}


async def set_setting(db, key, value):
    async with db.connection() as conn:
        await put_setting(conn, key, value)


async def planned_run(database, db, client, *, provider="fixture", product="residential"):
    """A census batch for DE with one node (fixture/residential in fra1) already running it."""
    now = datetime.now(UTC)
    prepare(database, ("DE",), DE=now.time())
    node = issue_token(database, provider=provider, product=product, region="fra1")
    await batches.census_plan(db, now)
    batch_id = query(database, "SELECT batch_id FROM test_batches")[0][0]
    headers = bearer(node["token"])
    await client.get("/v1/work", headers=headers)
    return batch_id, headers


def archive_bytes(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


async def test_results_upload_ingests_and_links_the_run(database, db, client, tmp_path):
    batch_id, headers = await planned_run(database, db, client)
    directory = await measurement(tmp_path, requests=3)
    upload = pack(directory, tmp_path / "run.tar.gz").read_bytes()
    reply = await client.post(f"/v1/results?batch_id={batch_id}", data=upload, headers=headers)
    assert reply.status == 200, await reply.text()
    body = await reply.json()
    assert body["ingested"] and body["status"] == "done" and body["imported"]
    run = query(
        database,
        "SELECT r.status,r.run_key,r.requests,r.successes,r.unique_ips,r.stop_reason,t.batch_id "
        "FROM batch_runs r JOIN test_runs t USING(run_key)",
    )
    assert run == [("done", body["run_key"], 3, 3, 1, "requests", batch_id)]
    assert query(database, "SELECT status FROM test_batches") == [("done",)]
    ((product, signal, data),) = query(
        database, "SELECT product_id,signal,data FROM test_events WHERE kind='census_finished'"
    )
    assert product == product_id(database, "fixture", "residential") and signal == 0
    assert data["requests"] == 3 and data["unique_ips"] == 1 and data["region"] == "fra1"
    assert data["status"] == "done" and data["stop_reason"] == "requests"
    assert data["success_rate"] == 1.0 and data["duration_s"] >= 0
    # A retry after a lost response is acknowledged without a second import.
    retry = await client.post(f"/v1/results?batch_id={batch_id}", data=upload, headers=headers)
    assert (await retry.json())["run_key"] == body["run_key"]


@pytest.mark.parametrize(
    "member",
    [
        ("../evil.json", tarfile.REGTYPE, b"{}"),
        ("/etc/evil", tarfile.REGTYPE, b"{}"),
        ("sub/summary.json", tarfile.REGTYPE, b"{}"),
        ("summary.json", tarfile.SYMTYPE, None),
        ("summary.json", tarfile.LNKTYPE, None),
        ("folder", tarfile.DIRTYPE, None),
    ],
)
async def test_results_reject_unsafe_archives(database, db, client, tmp_path, member):
    batch_id, headers = await planned_run(database, db, client)
    name, kind, data = member
    info = tarfile.TarInfo(name)
    info.type = kind
    if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
        info.linkname = "/etc/passwd"
    if data is not None:
        info.size = len(data)
    reply = await client.post(
        f"/v1/results?batch_id={batch_id}", data=archive_bytes([(info, data)]), headers=headers
    )
    assert reply.status == 400
    assert not (tmp_path.parent / "evil.json").exists()
    assert query(database, "SELECT status,error LIKE 'bad archive:%%' FROM batch_runs") == [
        ("failed", True)
    ]
    assert query(database, "SELECT kind FROM alerts") == [("results_rejected",)]
    assert query(database, "SELECT status FROM test_batches") == [("failed",)]


def census_summary(started, *, successes, stalled_at=None):
    summary = {
        "status": "completed",
        "started_at": started.isoformat(),
        "completed": 500,
        "successes": successes,
        "unique_ips": successes,
        "stop_reason": "stalled" if stalled_at else "unique_ratio",
    }
    if stalled_at:
        summary["outage_started_at"] = stalled_at.isoformat()
    return summary


async def test_stalled_census_opens_an_outage_and_a_census_with_successes_closes_it(
    database, db, client
):
    batch_id, _ = await planned_run(database, db, client)
    ((node_id,),) = query(database, "SELECT node_id FROM nodes")
    node = {"node_id": node_id}
    alpha = product_id(database, "fixture", "residential")
    began = datetime.now(UTC).replace(microsecond=0)
    down_at = began + timedelta(minutes=5)
    summary = census_summary(began, successes=120, stalled_at=down_at)
    finished = began + timedelta(minutes=15)
    assert await batches.finish_run(db, node, batch_id, summary, None, finished) == "done"
    # Replaying the upload changes nothing.
    assert await batches.finish_run(db, node, batch_id, summary, None, finished) == "done"
    assert query(
        database,
        "SELECT o.product_id,o.country_code,r.slug,o.source,o.batch_id,o.started_at,o.ended_at "
        "FROM outages o JOIN runner_regions r USING(region_id)",
    ) == [(alpha, "DE", "fra1", "census", batch_id, down_at, None)]
    events = query(
        database,
        "SELECT kind,signal,severity,batch_id,country_code,data FROM test_events "
        "WHERE kind IN ('census_finished','down','recovered') ORDER BY event_id",
    )
    assert [(e[0], e[1]) for e in events] == [("census_finished", -1), ("down", -1)]
    assert events[0][5]["stop_reason"] == "stalled"
    kind, _, severity, event_batch, country, data = events[1]
    assert (severity, event_batch, country) == ("critical", batch_id, "DE")
    assert data == {
        "region": "fra1",
        "since": down_at.isoformat(),
        "source": "census",
        "batch_id": batch_id,
        "requests": 500,
        "successes": 120,
    }
    # The next day's census gets through: the outage ends when it started, once.
    tomorrow = finished + timedelta(days=1)
    with connect(database) as conn:
        conn.execute("UPDATE countries SET census_time=%s", (tomorrow.time(),))
    await batches.census_plan(db, tomorrow)
    ((second,),) = query(database, "SELECT max(batch_id) FROM test_batches")
    assert second != batch_id
    await batches.assignments(db, node, tomorrow)
    restart = tomorrow + timedelta(minutes=1)
    healthy = census_summary(restart, successes=400)
    for _ in range(2):
        await batches.finish_run(db, node, second, healthy, None, tomorrow + timedelta(hours=1))
    assert query(database, "SELECT started_at,ended_at FROM outages") == [(down_at, restart)]
    recovered = query(
        database,
        "SELECT signal,batch_id,data FROM test_events WHERE kind IN ('recovered','down') "
        "ORDER BY event_id",
    )[1:]
    assert len(recovered) == 1
    signal, event_batch, data = recovered[0]
    assert (signal, event_batch) == (1, second)
    assert data["source"] == "census" and data["since"] == down_at.isoformat()
    assert data["duration_s"] == (restart - down_at).total_seconds()
    assert data["outage_batch_id"] == batch_id


async def test_results_reject_foreign_and_invalid_runs(database, db, client, tmp_path):
    batch_id, headers = await planned_run(database, db, client, product="datacenter")
    directory = await measurement(tmp_path, requests=2)  # fixture/residential, not ours
    upload = pack(directory, tmp_path / "run.tar.gz").read_bytes()
    reply = await client.post(f"/v1/results?batch_id={batch_id}", data=upload, headers=headers)
    assert reply.status == 400
    assert "another provider or product" in (await reply.json())["error"]
    assert (await client.post("/v1/results?batch_id=x", data=b"", headers=headers)).status == 400


async def test_results_ingest_errors_fail_the_run(database, db, client, tmp_path):
    batch_id, headers = await planned_run(database, db, client)
    directory = await measurement(tmp_path, requests=2)
    summary = json.loads((directory / "summary.json").read_text())
    summary["successes"] += 1
    (directory / "summary.json").write_text(json.dumps(summary))
    upload = pack(directory, tmp_path / "run.tar.gz").read_bytes()
    reply = await client.post(f"/v1/results?batch_id={batch_id}", data=upload, headers=headers)
    assert reply.status == 422
    assert query(database, "SELECT status FROM batch_runs") == [("failed",)]
    assert query(database, "SELECT kind FROM alerts") == [("ingest_failed",)]
    conflict = await client.post(f"/v1/results?batch_id={batch_id}", data=upload, headers=headers)
    assert conflict.status == 409


async def test_failed_endpoint(database, db, client):
    batch_id, headers = await planned_run(database, db, client)
    reply = await client.post(
        "/v1/failed", json={"batch_id": batch_id, "error": "proxy down"}, headers=headers
    )
    assert await reply.json() == {"ok": True, "status": "failed"}
    assert query(database, "SELECT error FROM batch_runs") == [("proxy down",)]
    assert (
        await client.post("/v1/failed", json={"batch_id": batch_id}, headers=headers)
    ).status == 400
    assert (
        await client.post("/v1/failed", json={"batch_id": 424242, "error": "x"}, headers=headers)
    ).status == 404
    assert (await client.post("/v1/failed", data=b"[", headers=headers)).status == 400


def test_archive_limits(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "summary.json").write_text("{}" + " " * 1000)
    (tmp_path / "src" / "ips.jsonl.gz").write_bytes(b"x")
    archive = pack(tmp_path / "src", tmp_path / "a.tar.gz")
    (tmp_path / "out").mkdir()
    assert sorted(unpack(archive, tmp_path / "out", max_bytes=10_000)) == [
        "ips.jsonl.gz",
        "summary.json",
    ]
    (tmp_path / "small").mkdir()
    with pytest.raises(ArchiveError, match="extraction limit"):
        unpack(archive, tmp_path / "small", max_bytes=100)
    (tmp_path / "junk.tar.gz").write_bytes(b"not a tarball")
    (tmp_path / "junk").mkdir()
    with pytest.raises(ArchiveError, match="Unreadable"):
        unpack(tmp_path / "junk.tar.gz", tmp_path / "junk", max_bytes=100)
    info = tarfile.TarInfo("ips.jsonl.gz")
    info.size = 1
    (tmp_path / "nosummary.tar.gz").write_bytes(archive_bytes([(info, b"x")]))
    (tmp_path / "ns").mkdir()
    with pytest.raises(ArchiveError, match="no summary"):
        unpack(tmp_path / "nosummary.tar.gz", tmp_path / "ns", max_bytes=100)


# --- jobs and scheduler ---------------------------------------------------------------------


async def test_scheduler_records_runs_failures_and_skips(database, db):
    scheduler = Scheduler(db)
    calls = []

    async def ok():
        calls.append("ok")
        return {"n": 1}

    async def broken():
        raise RuntimeError("nope")

    async def idle():
        return {"skipped": "nothing to do"}

    scheduler.add("ok", ok, every_s=60)
    scheduler.add("broken", broken, every_s=60)
    scheduler.add("idle", idle, every_s=60)
    assert await scheduler.run_now("ok") == {"n": 1}
    assert await scheduler.run_now("broken") is None
    assert await scheduler.run_now("broken") is None
    await scheduler.run_now("idle")
    scheduler.tasks["ok"].running = True
    assert await scheduler.run_now("ok") is None
    with pytest.raises(KeyError):
        await scheduler.run_now("missing")
    rows = query(database, "SELECT job,status,error FROM job_runs ORDER BY id")
    assert rows == [
        ("ok", "ok", None),
        ("broken", "failed", "RuntimeError: nope"),
        ("broken", "failed", "RuntimeError: nope"),
        ("idle", "skipped", "nothing to do"),
        ("ok", "skipped", "still running"),
    ]
    assert query(database, "SELECT kind,dedupe_key,occurrences FROM alerts") == [
        ("job_failed", "job_failed:broken", 2)
    ]
    assert [t["job"] for t in scheduler.upcoming()] == ["broken", "idle", "ok"]


def test_task_schedules():
    at = datetime(2026, 10, 1, 12, 0, 30, tzinfo=UTC)

    async def job():
        return None

    assert Task("a", job, every_s=60, aligned=True).schedule(at) == datetime(
        2026, 10, 1, 12, 1, tzinfo=UTC
    )
    assert Task("b", job, every_s=15).schedule(at) == at + timedelta(seconds=15)
    assert Task("c", job, at_minute=0).schedule(at) == datetime(2026, 10, 1, 13, tzinfo=UTC)
    assert Task("d", job, at_utc_minute=30).schedule(at) == datetime(2026, 10, 2, 0, 30, tzinfo=UTC)


async def test_scheduler_loop_fires_due_jobs(db):
    scheduler = Scheduler(db)
    fired = []

    async def job():
        fired.append(1)

    scheduler.add("fast", job, every_s=0.05)
    scheduler.start()
    for _ in range(100):
        if len(fired) >= 2:
            break
        await asyncio.sleep(0.05)
    await scheduler.stop()
    assert len(fired) >= 2


async def test_calculate_is_debounced_until_a_batch_finishes(database, db, monkeypatch):
    calls = []

    def fake(database_file, methodology, *, publish):
        calls.append(publish)
        return {"snapshot_id": "s1", "as_of": "now"}

    import sys
    import types

    # A stand-in module: the job imports the calculator lazily, at run time.
    module = types.ModuleType("proxy_index.calculated.calculator")
    module.calculate = fake
    monkeypatch.setitem(sys.modules, "proxy_index.calculated.calculator", module)
    assert (await calculate(db, database, "m.toml"))["skipped"].startswith("no batch")
    prepare(database, ("DE",), DE=time(12))
    issue_token(database, provider="alpha", product="res", region="fra1")
    await batches.census_plan(db, NOW)
    with connect(database) as conn:
        conn.execute("UPDATE test_batches SET status='done',finished_at=now()")
    assert await calculate(db, database, "m.toml") == {"snapshot_id": "s1", "as_of": "now"}
    with connect(database) as conn:
        conn.execute("INSERT INTO job_runs(job,status,started_at) VALUES ('calculate','ok',now())")
        conn.execute("UPDATE test_batches SET finished_at=now()+interval '1 second'")
    assert (await calculate(db, database, "m.toml")) == {"skipped": "debounced"}
    assert calls == [True]


async def test_detect_job_is_optional(database, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "proxy_index.detect", None)
    assert (await detect(database))["skipped"].startswith("proxy_index.detect")

    class Module:
        @staticmethod
        def detect(conn, now):
            assert now.tzinfo is UTC
            return 3

    monkeypatch.setitem(sys.modules, "proxy_index.detect", Module)
    assert await detect(database) == {"events": 3}


async def test_retention_drops_old_partitions_and_job_runs(database, db):
    old = datetime(2026, 1, 15, tzinfo=UTC)
    recent = datetime(2026, 9, 20, tzinfo=UTC)
    for moment in (old, recent):
        await db.ensure_partition("test_events", moment)
    with connect(database) as conn:
        for moment in (old, recent, datetime(2026, 7, 1, tzinfo=UTC)):
            if moment.month == 7:
                conn.execute(
                    "CREATE TABLE test_events_2026_07 PARTITION OF test_events "
                    "FOR VALUES FROM ('2026-07-01') TO ('2026-08-01')"
                )
            conn.execute("INSERT INTO test_events(at,kind) VALUES (%s,'x')", (moment,))
        conn.execute(
            "INSERT INTO job_runs(job,status,started_at) VALUES ('a','ok',%s),('b','ok',%s)",
            (datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC)),
        )
    result = await retention(db, datetime(2026, 10, 1, tzinfo=UTC))
    assert result["dropped_partitions"] == ["test_events_2026_01"]
    assert result["events_deleted"] == 1  # July 1 is past the 90-day cutoff of July 3
    assert result["job_runs_deleted"] == 1
    assert query(database, "SELECT at FROM test_events") == [(recent,)]


async def test_reaper_gives_uploads_longer(database, db):
    prepare(database, ("DE",), DE=time(12))
    a = issue_token(database, provider="alpha", product="res", region="fra1")
    await batches.census_plan(db, NOW)
    batch_id = query(database, "SELECT batch_id FROM test_batches")[0][0]
    node = {"node_id": a["node_id"]}
    start = NOW + timedelta(seconds=30)
    await batches.assignments(db, node, start)
    assert (await batches.begin_upload(db, node, batch_id, start))["status"] == "running"
    assert (await batches.reap(db, start + timedelta(minutes=20)))["failed"] == {}
    assert (await batches.reap(db, start + timedelta(minutes=46)))["failed"] == {"stale": 1}


def test_cli_node_token_and_dev_bind(database, capsys):
    from proxy_index.cli import main

    prepare(database)
    assert (
        main(
            [
                "node-token",
                "--database-file",
                str(database),
                "--provider",
                "alpha",
                "--product",
                "res",
                "--region",
                "prod-fra1-a",
            ]
        )
        == 0
    )
    issued = json.loads(capsys.readouterr().out)
    assert issued["region"] == "fra1" and len(issued["token"]) > 30 and not issued["rotated"]
    assert query(database, "SELECT token_hash<>%s FROM nodes", (issued["token"],)) == [(True,)]
    assert (
        main(["orchestrator", "--dev", "--host", "0.0.0.0", "--database-file", str(database)]) == 2
    )
    assert (
        main(
            [
                "node-token",
                "--database-file",
                str(database),
                "--provider",
                "alpha",
                "--product",
                "res",
                "--region",
                "mars1",
            ]
        )
        == 2
    )
