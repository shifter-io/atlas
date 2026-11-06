"""Admin JSON API (/admin/api): auth rules and the actions."""

import base64

import pytest
from admin_cases import AUTH, CREDENTIAL, FRA1, census, get, plan_pulses, post, serve
from aiohttp import web
from orchestrator_cases import bearer, prepare, product_id, query

from proxy_index.orchestrator import app as app_module
from proxy_index.orchestrator import batches
from proxy_index.orchestrator.admin import routes, writers
from proxy_index.orchestrator.app import build_app
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.storage import connect

pytest_plugins = ["test_storage", "admin_cases"]


# --- auth -----------------------------------------------------------------------------------


async def test_basic_auth_guards_admin_only(admin):
    assert (await admin.get("/admin/api/health")).status == 401
    reply = await admin.get("/admin/api/dashboard")
    assert reply.status == 401 and reply.headers["WWW-Authenticate"].startswith("Basic ")
    wrong = base64.b64encode(b"ops:nope").decode()
    assert (
        await admin.get("/admin/api/health", headers={"Authorization": f"Basic {wrong}"})
    ).status == 401
    for header in ("Basic !!!not-base64", "Bearer " + CREDENTIAL, "Basic"):
        assert (
            await admin.get("/admin/api/health", headers={"Authorization": header})
        ).status == 401
    body = await get(admin, "/health")
    assert body["ok"] and body["scheduler"] and "census_plan" in body["jobs"]
    # The node API and the load-balancer probe are not behind the admin's auth.
    assert (await admin.get("/health")).status == 200
    assert (await admin.get("/v1/work")).status == 401


def remote(address):
    @web.middleware
    async def middleware(request, handler):
        return await handler(request.clone(remote=address))

    return middleware


@pytest.mark.parametrize(
    ("dev", "address", "status"),
    [(True, None, 200), (True, "203.0.113.9", 503), (False, None, 503), (True, "::1", 200)],
)
async def test_no_password_is_loopback_only_in_dev(database, db, dev, address, status):
    app = build_app(database, with_scheduler=False, db=db, dev=dev, admin_credential=None)
    if address:
        app.middlewares.insert(0, remote(address))
    client = await serve(app)
    try:
        reply = await client.get("/admin/api/health")
        assert reply.status == status
        if status == 503:
            assert "ADMIN_PASSWORD" in (await reply.json())["error"]
    finally:
        await client.close()


def test_credentials_from_env_and_serve_refuses_without_password(database, monkeypatch):
    assert routes.credentials_from_env({}) is None
    assert routes.credentials_from_env({"ADMIN_USER": "x"}) is None
    assert routes.credentials_from_env({"ADMIN_PASSWORD": "pw"}) == "admin:pw"
    assert routes.credentials_from_env({"ADMIN_USER": "u", "ADMIN_PASSWORD": "pw"}) == "u:pw"
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="ADMIN_PASSWORD"):
        app_module.serve(str(database))
    from proxy_index.cli import main

    assert main(["orchestrator", "--database-file", str(database)]) == 2
    # The environment is read when no credential is passed.
    monkeypatch.setenv("ADMIN_USER", "env")
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    app = build_app(str(database), with_scheduler=False)
    assert app[routes.AUTH_KEY] == routes.AdminAuth("env:pw", False)


async def test_actions_require_json(admin):
    reply = await admin.post("/admin/api/alerts/1/ack", data="x=1", headers=AUTH)
    assert reply.status == 415
    bad = await admin.post(
        "/admin/api/alerts/1/ack", data="{nope", headers=AUTH | {"Content-Type": "application/json"}
    )
    assert bad.status == 400
    array = await admin.post("/admin/api/alerts/1/ack", json=[1], headers=AUTH)
    assert array.status == 400


# --- actions --------------------------------------------------------------------------------


async def test_kill_stops_the_batch_and_its_runs(database, db, admin):
    batch_id, tokens = await census(database, db)
    done = issue_token(database, provider="gamma", product="res", region="fra1")
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,node_id,status,requests) "
            "VALUES (%s,%s,%s,'done',7)",
            (batch_id, product_id(database, "gamma"), done["node_id"]),
        )
    assert await post(admin, f"/batches/{batch_id}/kill") == {
        "batch_id": batch_id,
        "status": "killed",
        "runs_killed": 2,
    }
    (batch,) = query(
        database,
        "SELECT status,finished_at IS NOT NULL FROM test_batches WHERE batch_id=%s",
        (batch_id,),
    )
    assert batch == ("killed", True)
    runs = query(
        database,
        "SELECT p.provider,r.status,r.stop_reason,r.finished_at IS NOT NULL "
        "FROM batch_runs r JOIN products p USING(product_id) ORDER BY 1",
    )
    assert runs == [
        ("alpha", "killed", "operator", True),
        ("beta", "killed", "operator", True),
        ("gamma", "done", None, False),
    ]
    # Each killed run ends like any other: census_finished, stop_reason operator.
    events = query(
        database,
        "SELECT kind,product_id,data->>'stop_reason',data->>'status' "
        "FROM test_events WHERE kind<>'census_started' ORDER BY event_id",
    )
    assert sorted(events) == sorted(
        [
            ("census_finished", product_id(database, "alpha"), "operator", "killed"),
            ("census_finished", product_id(database, "beta"), "operator", "killed"),
        ]
    )
    # The node is told to stop.
    progress = await admin.post(
        "/v1/progress",
        json={"batch_id": batch_id, "counters": {}},
        headers=bearer(tokens["alpha"]["token"]),
    )
    assert await progress.json() == {"stop": True}
    work = await admin.get("/v1/work", headers=bearer(tokens["alpha"]["token"]))
    assert (await work.json())["kill"] == [batch_id]
    # A closed batch cannot be killed or skipped again.
    assert "already killed" in (await post(admin, f"/batches/{batch_id}/kill", expect=409))["error"]
    await post(admin, f"/batches/{batch_id}/skip", expect=409)
    await post(admin, "/batches/999999/kill", expect=404)


async def test_skip_census_writes_census_skipped(database, db, admin):
    batch_id, _ = await census(database, db)
    assert (await post(admin, f"/batches/{batch_id}/skip"))["status"] == "skipped"
    assert query(database, "SELECT status FROM test_batches") == [("skipped",)]
    assert query(database, "SELECT DISTINCT status,stop_reason FROM batch_runs") == [
        ("killed", "operator")
    ]
    (skipped,) = query(
        database,
        "SELECT batch_id,country_code,region_id,severity,data "
        "FROM test_events WHERE kind='census_skipped'",
    )
    assert skipped[:4] == (batch_id, "DE", FRA1, "minor")
    assert skipped[4] == {
        "region": "fra1",
        "reason": "operator",
        "status": "skipped",
        "runs_killed": 2,
        "requests": 120,
        "successes": 100,
        "unique_ips": 80,
    }
    assert len(query(database, "SELECT 1 FROM test_events WHERE kind='census_finished'")) == 2


async def test_kill_pulse_writes_pulse_finished(database, db, admin):
    prepare(database)
    issue_token(database, provider="alpha", product="res", region="fra1")
    await plan_pulses(db)
    batch_id = query(database, "SELECT min(batch_id) FROM test_batches")[0][0]
    assert (await post(admin, f"/batches/{batch_id}/kill"))["runs_killed"] == 1
    (event,) = query(
        database, "SELECT kind,product_id,data->>'reason',data->>'status' FROM test_events"
    )
    assert event == ("pulse_finished", None, "operator", "killed")
    with pytest.raises(ValueError):
        await writers.stop_batch(db, batch_id, "done")


async def test_acknowledge_alert(database, admin):
    with connect(database) as conn:
        alert_id = conn.execute(
            "INSERT INTO alerts(kind,message) VALUES ('k','m') RETURNING alert_id"
        ).fetchone()[0]
    assert await post(admin, f"/alerts/{alert_id}/ack") == {
        "alert_id": alert_id,
        "acknowledged": True,
        "already": False,
    }
    assert (await post(admin, f"/alerts/{alert_id}/ack"))["already"] is True
    assert (await get(admin, "/dashboard"))["alerts"] == []
    await post(admin, "/alerts/999999/ack", expect=404)


async def test_scheduler_switch_action(database, db, admin):
    prepare(database, ("DE",))
    issue_token(database, provider="alpha", product="res", region="fra1")
    assert await post(admin, "/schedule/scheduler", {"pulse": False}) == {
        "census": True,
        "pulse": False,
        "census_every_minutes": None,
    }
    assert (await get(admin, "/schedule"))["scheduler"]["pulse"] is False
    assert (await batches.pulse_plan(db))["skipped"] == "pulse scheduler is off"
    assert query(database, "SELECT count(*) FROM test_batches") == [(0,)]
    both = await post(admin, "/schedule/scheduler", {"census": False, "pulse": True})
    assert (both["census"], both["pulse"]) == (False, True)
    for bad in ({}, {"census": "off"}, {"pulse": 1}, {"both": True}):
        await post(admin, "/schedule/scheduler", bad, 400)
