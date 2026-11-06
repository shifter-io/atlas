"""Orchestrator runs: the barrier, the reaper, heartbeats, node tokens, work and progress."""

from datetime import UTC, datetime, time, timedelta

from orchestrator_cases import NOW, bearer, prepare, product_id, query, set_setting

from proxy_index.orchestrator import batches, nodework
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]


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
    work = await nodework.assignments(db, node_a, start)
    assert [w["batch_id"] for w in work["assignments"]] == [batch_id]
    await nodework.assignments(db, node_b, start)
    assert await nodework.progress(db, node_a, batch_id, {"requests": 0}, start) == {"stop": False}
    assert query(database, "SELECT status,started_at FROM test_batches") == [("running", start)]
    # census_started is per product, written when that product's run starts.
    alpha, beta = product_id(database, "alpha"), product_id(database, "beta")
    assert query(database, "SELECT kind,product_id,data FROM test_events") == [
        ("census_started", alpha, {"region": "fra1", "start_at": "2026-10-01T12:00:30+00:00"})
    ]
    await nodework.progress(
        db, node_a, batch_id, {"requests": 40, "successes": 30, "unique_ips": 20}, start
    )
    assert await nodework.fail_run(db, node_a, batch_id, "boom", start) == "failed"
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
    await nodework.fail_run(db, node_b, batch_id, "boom", start)
    assert query(database, "SELECT status,finished_at FROM test_batches") == [("failed", start)]
    rows = query(database, finished)
    assert [(r[0], r[1], r[3]["duration_s"]) for r in rows] == [(alpha, -1, 0.0), (beta, -1, None)]
    assert query(database, "SELECT count(*) FROM test_events") == [(3,)]
    # A terminal run cannot be failed twice.
    assert await nodework.fail_run(db, node_b, batch_id, "again", start) == "failed"


async def test_reaper_fails_unstarted_and_silent_runs(database, db):
    prepare(database, ("DE",), DE=time(12))
    a = issue_token(database, provider="alpha", product="res", region="fra1")
    b = issue_token(database, provider="beta", product="res", region="fra1")
    await batches.census_plan(db, NOW)
    batch_id = query(database, "SELECT batch_id FROM test_batches")[0][0]
    start = NOW + timedelta(seconds=30)
    await nodework.assignments(db, {"node_id": a["node_id"]}, start)
    await nodework.progress(db, {"node_id": a["node_id"]}, batch_id, {"requests": 1}, start)
    assert (await nodework.reap(db, start + timedelta(seconds=60)))["failed"] == {}
    # Beta never started; alpha went silent.
    result = await nodework.reap(db, start + timedelta(hours=1))
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
    assert (await nodework.mark_offline(db))["offline"] == []
    gone = await nodework.mark_offline(db, datetime.now(UTC) + timedelta(seconds=61))
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
