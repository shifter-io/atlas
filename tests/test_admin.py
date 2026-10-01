"""Admin JSON API (/admin/api): auth rules, every reader's shape, and the actions."""

import base64
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_orchestrator import bearer, prepare, product_id, query

from milf_framework.orchestrator import app as app_module
from milf_framework.orchestrator import batches
from milf_framework.orchestrator.admin import routes, views
from milf_framework.orchestrator.app import build_app
from milf_framework.orchestrator.db import Database
from milf_framework.orchestrator.nodes import issue_token
from milf_framework.regions import REGION_IDS
from milf_framework.storage import connect

pytest_plugins = ["test_storage"]

CREDENTIAL = "ops:s3cret"
AUTH = {"Authorization": "Basic " + base64.b64encode(CREDENTIAL.encode()).decode()}
FRA1 = REGION_IDS["fra1"]


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()


async def serve(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.fixture
async def admin(database, db):
    client = await serve(
        build_app(database, with_scheduler=False, db=db, admin_credential=CREDENTIAL)
    )
    yield client
    await client.close()


async def get(client, path, expect=200, **params):
    reply = await client.get(f"/admin/api{path}", headers=AUTH, params=params)
    assert reply.status == expect, await reply.text()
    return await reply.json()


async def post(client, path, body=None, expect=200):
    reply = await client.post(f"/admin/api{path}", json=body or {}, headers=AUTH)
    assert reply.status == expect, await reply.text()
    return await reply.json()


async def census(database, db, *countries, providers=("alpha", "beta")):
    """A census batch for DE (fra1) with one node per provider, alpha running, beta queued."""
    now = datetime.now(UTC)
    prepare(database, countries or ("DE",), DE=now.time())
    tokens = {p: issue_token(database, provider=p, product="res", region="fra1") for p in providers}
    await batches.census_plan(db, now)
    batch_id = query(database, "SELECT batch_id FROM test_batches WHERE kind='census'")[0][0]
    alpha = {"node_id": tokens["alpha"]["node_id"]}
    await batches.assignments(db, alpha, now + timedelta(minutes=1))
    counters = {
        "requests": 120,
        "successes": 100,
        "unique_ips": 80,
        "bytes_up": 1000,
        "bytes_down": 9000,
    }
    await batches.progress(db, alpha, batch_id, counters)
    return batch_id, tokens


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
    from milf_framework.cli import main

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


# --- readers --------------------------------------------------------------------------------


async def test_dashboard_shape(database, db, admin):
    batch_id, _ = await census(database, db)
    now = datetime.now(UTC)
    await batches.pulse_plan(db, now)
    with connect(database) as conn:
        conn.execute(
            "UPDATE batch_runs br SET status='done',requests=100,successes=90 FROM test_batches b "
            "WHERE b.batch_id=br.batch_id AND b.kind='pulse' AND b.target_id='generic_http'"
        )
        conn.execute("INSERT INTO job_runs(job,status,error) VALUES ('calculate','failed','x')")
        conn.execute(
            "INSERT INTO alerts(kind,severity,message,dedupe_key) VALUES "
            "('a','minor','minor one','a'),('b','critical','critical one','b')"
        )
    body = await get(admin, "/dashboard")
    assert set(body) == {
        "now",
        "census",
        "pulse",
        "heatmap",
        "alerts",
        "jobs",
        "failed_jobs",
        "upcoming",
        "nodes",
        "open_outages",
    }
    assert body["open_outages"] == 0
    (batch,) = body["census"]
    assert batch["batch_id"] == batch_id and batch["status"] == "running"
    assert batch["region"] == "fra1" and batch["country"] and batch["runs_total"] == 2
    assert (batch["requests"], batch["unique_ips"], batch["runs_open"]) == (120, 80, 2)
    alpha, beta = batch["runs"]
    assert alpha["label"] == "alpha/res" and alpha["status"] == "running"
    assert alpha["ratio"] == 100 / 80 and alpha["success_rate"] == 100 / 120
    assert alpha["silent_s"] is not None and alpha["host"] is None
    assert beta["status"] == "queued" and beta["ratio"] is None
    # Pulse: one row per target and slot, summed over countries and products.
    targets = {row["target_id"]: row for row in body["pulse"]}
    assert set(targets) == {"generic_http", "generic_https"}
    assert targets["generic_http"]["runs_done"] == 2 and targets["generic_http"]["requests"] == 200
    heat = body["heatmap"]
    assert heat["slot_minutes"] == 30 and len(heat["slots"]) == 48
    fra1 = next(row for row in heat["regions"] if row["region"] == "fra1")
    assert fra1["rates"][-1] == pytest.approx(180 / 200) and fra1["requests"][-1] == 200
    assert fra1["rates"][0] is None
    assert [a["severity"] for a in body["alerts"]] == ["critical", "minor"]
    assert body["failed_jobs"] == 1 and body["jobs"][0]["job"] == "calculate"
    (upcoming,) = body["upcoming"]
    assert upcoming["country_code"] == "DE" and upcoming["region"] == "fra1"
    assert len(upcoming["census_time"]) == 5 and upcoming["in_s"] > 80_000
    assert body["nodes"] == {"online": 0, "offline": 2, "missing": 2 * 7}


async def test_batches_list_filters_and_detail(database, db, admin):
    batch_id, _ = await census(database, db)
    await batches.pulse_plan(db, datetime.now(UTC))
    body = await get(admin, "/batches")
    assert body["filters"] == {"kind": None, "status": None, "country": None, "limit": 100}
    assert len(body["batches"]) == 3
    census_rows = (await get(admin, "/batches", kind="census"))["batches"]
    assert [r["batch_id"] for r in census_rows] == [batch_id]
    row = census_rows[0]
    assert (row["runs_total"], row["runs_open"], row["requests"], row["bytes_up"]) == (
        2,
        2,
        120,
        1000,
    )
    assert row["success_rate"] == 100 / 120 and row["duration_s"] >= 0
    assert (
        len(
            (await get(admin, "/batches", kind="pulse", status="scheduled", country="de"))[
                "batches"
            ]
        )
        == 2
    )
    assert (await get(admin, "/batches", country="FR"))["batches"] == []
    assert len((await get(admin, "/batches", limit="1"))["batches"]) == 1
    await get(admin, "/batches", 400, kind="weekly")
    await get(admin, "/batches", 400, status="lost")
    await get(admin, "/batches", 400, country="DEU")
    await get(admin, "/batches", 400, limit="0")
    await get(admin, "/batches", 400, limit="many")

    detail = await get(admin, f"/batches/{batch_id}")
    assert detail["kind"] == "census" and detail["target_url"].startswith("http://")
    assert [r["label"] for r in detail["runs"]] == ["alpha/res", "beta/res"]
    assert [e["kind"] for e in detail["events"]] == ["census_started"]
    assert detail["events"][0]["provider"] == "alpha"
    await get(admin, "/batches/999999", 404)


async def test_schedule_reader_and_country_edits(database, db, admin):
    prepare(database, ("DE", "FR"))
    body = await get(admin, "/schedule")
    assert set(body) == {"now", "regions", "countries", "targets"}
    de = next(c for c in body["countries"] if c["country_code"] == "DE")
    assert de["region"] == "fra1" and de["enabled"] and len(de["census_time"]) == 5
    assert sum(c["enabled"] for c in body["countries"]) == 2
    fra1 = next(r for r in body["regions"] if r["slug"] == "fra1")
    assert fra1["enabled_countries"] == 1 and fra1["countries"] > 1
    http = next(t for t in body["targets"] if t["target_id"] == "generic_http")
    assert http["census"] and http["pulse_interval_minutes"] == 30

    assert await post(admin, "/schedule/country", {"cc": "de", "census_time": "07:05"}) == {
        "country_code": "DE",
        "census_time": "07:05",
        "enabled": True,
    }
    assert (await post(admin, "/schedule/country", {"cc": "DE", "enabled": False}))[
        "census_time"
    ] == "07:05"
    assert query(
        database, "SELECT census_time::text,enabled FROM countries WHERE country_code='DE'"
    ) == [("07:05:00", False)]
    for bad in (
        {"cc": "DE", "census_time": "24:00"},
        {"cc": "DE", "census_time": "7:05"},
        {"cc": "DE", "census_time": 705},
        {"cc": "DE", "enabled": "yes"},
        {"cc": "DEU", "enabled": True},
        {"enabled": True},
        {"cc": "DE"},
        {"cc": "DE", "enabled": True, "region": "lon1"},
    ):
        await post(admin, "/schedule/country", bad, 400)
    await post(admin, "/schedule/country", {"cc": "QQ", "enabled": True}, 404)


async def test_target_edits_are_validated(database, db, admin):
    prepare(database)
    changed = await post(
        admin,
        "/schedule/target",
        {
            "target_id": "generic_https",
            "pulse_enabled": False,
            "pulse_interval_minutes": 15,
            "pulse_requests": "50",
            "pulse_concurrency": 10,
            "census_concurrency": 500,
        },
    )
    assert changed == {
        "target_id": "generic_https",
        "census": False,
        "census_concurrency": 500,
        "pulse_enabled": False,
        "pulse_interval_minutes": 15,
        "pulse_requests": 50,
        "pulse_concurrency": 10,
    }
    for bad in (
        {"target_id": "generic_http"},
        {"pulse_requests": 5},
        {"target_id": "generic_http", "pulse_interval_minutes": 0},
        {"target_id": "generic_http", "pulse_interval_minutes": 1441},
        {"target_id": "generic_http", "pulse_requests": 1.5},
        {"target_id": "generic_http", "pulse_concurrency": True},
        {"target_id": "generic_http", "pulse_enabled": 1},
        {"target_id": "generic_http", "url": "http://evil.test/"},
    ):
        await post(admin, "/schedule/target", bad, 400)
    await post(admin, "/schedule/target", {"target_id": "nope", "pulse_requests": 5}, 404)
    async with db.connection() as conn:
        with pytest.raises(ValueError):
            await views.set_target(conn, "generic_http", {"url": "x"})


async def test_nodes_grid_includes_missing_cells(database, admin):
    prepare(database)
    alpha = issue_token(database, provider="alpha", product="res", region="fra1")
    issue_token(database, provider="beta", product="res", region="lon1")
    reply = await admin.post(
        "/v1/heartbeat",
        json={"open_conns": 3, "req_per_s": 2.5, "rss_mb": 90},
        headers=bearer(alpha["token"]),
    )
    assert reply.status == 200
    body = await get(admin, "/nodes")
    assert body["offline_after_s"] == 60 and body["products"] == ["alpha/res", "beta/res"]
    assert body["regions"][:2] == ["lon1", "fra1"] and len(body["regions"]) == 8
    assert len(body["cells"]) == 16
    cell = {(c["label"], c["region"]): c for c in body["cells"]}
    online = cell[("alpha/res", "fra1")]
    assert online["status"] == "online" and online["node_id"] == alpha["node_id"]
    assert online["open_conns"] == 3 and 0 <= online["heartbeat_age_s"] < 60
    assert cell[("beta/res", "lon1")]["status"] == "offline"
    assert cell[("beta/res", "lon1")]["heartbeat_age_s"] is None
    missing = cell[("alpha/res", "lon1")]
    assert missing["status"] == "missing" and missing["node_id"] is None


async def test_traffic_sums_and_filters(database, db, admin):
    batch_id, _ = await census(database, db)
    await batches.pulse_plan(db, datetime.now(UTC))
    with connect(database) as conn:
        conn.execute(
            "UPDATE batch_runs br SET status='done',requests=100,bytes_up=500,bytes_down=1500 "
            "FROM test_batches b WHERE b.batch_id=br.batch_id AND b.kind='pulse'"
        )
    body = await get(admin, "/traffic")
    assert body["unit"] == "GB = 10^9 bytes"
    rows = {(r["test"], r["label"]): r for r in body["rows"]}
    # The queued beta census run has sent nothing and is left out.
    assert set(rows) == {("census", "alpha/res"), ("pulse", "alpha/res"), ("pulse", "beta/res")}
    alpha_census = rows[("census", "alpha/res")]
    assert (alpha_census["runs"], alpha_census["requests"], alpha_census["bytes"]) == (
        1,
        120,
        10_000,
    )
    assert alpha_census["gb"] == 10_000 / 1e9
    assert alpha_census["avg_bytes_per_request"] == 10_000 / 120
    # Two pulse targets x DE for each provider.
    assert rows[("pulse", "beta/res")]["runs"] == 2 and rows[("pulse", "beta/res")]["bytes"] == 4000
    assert body["by_test"]["pulse"]["requests"] == 400
    assert body["total"]["bytes_up"] == 1000 + 4 * 500
    assert body["choices"]["providers"] == ["alpha", "alpha/res", "beta", "beta/res"]
    assert "fra1" in body["choices"]["regions"]

    def keys(report):
        return sorted((r["test"], r["label"]) for r in report["rows"])

    assert keys(await get(admin, "/traffic", test="census")) == [("census", "alpha/res")]
    assert keys(await get(admin, "/traffic", provider="beta")) == [("pulse", "beta/res")]
    assert keys(await get(admin, "/traffic", provider="beta/other")) == []
    assert (await get(admin, "/traffic", region="lon1"))["rows"] == []
    future = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    assert (await get(admin, "/traffic", **{"from": future}))["rows"] == []
    windowed = await get(admin, "/traffic", **{"from": "2020-01-01", "to": future})
    assert windowed["filters"]["from"] == "2020-01-01T00:00:00+00:00"
    assert windowed["total"] == body["total"]
    empty = await get(admin, "/traffic", test="census", provider="nobody")
    assert empty["total"]["avg_bytes_per_request"] is None
    await get(admin, "/traffic", 400, **{"from": "yesterday"})
    await get(admin, "/traffic", 400, **{"from": "2026-02-01", "to": "2026-01-01"})
    await get(admin, "/traffic", 400, test="weekly")
    assert batch_id


async def test_jobs_reader_and_run_now(database, db, admin):
    prepare(database)
    body = await get(admin, "/jobs")
    assert body["jobs"] == [] and body["recent"] == []
    assert {j["job"] for j in body["schedule"]} >= {"census_plan", "pulse_plan", "reap"}
    ran = await post(admin, "/jobs/run", {"job": "reap"})
    assert ran["ran"] == "reap" and ran["result"] == {"failed": {}, "closed": 0}
    assert ran["last"]["status"] == "ok"
    body = await get(admin, "/jobs")
    (latest,) = body["jobs"]
    assert latest["job"] == "reap" and latest["status"] == "ok" and latest["failures_24h"] == 0
    assert latest["last_ok_at"] is not None and len(body["recent"]) == 1
    await post(admin, "/jobs/run", {"job": "nope"}, 404)
    await post(admin, "/jobs/run", {}, 400)


async def test_jobs_without_a_scheduler(database, db):
    app = build_app(database, with_scheduler=False, db=db, admin_credential=CREDENTIAL)
    del app[routes.SCHEDULER_KEY]
    client = await serve(app)
    try:
        assert (await get(client, "/jobs"))["schedule"] == []
        assert (await get(client, "/health"))["scheduler"] is False
        await post(client, "/jobs/run", {"job": "reap"}, 503)
    finally:
        await client.close()


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
    await batches.pulse_plan(db, datetime.now(UTC))
    batch_id = query(database, "SELECT min(batch_id) FROM test_batches")[0][0]
    assert (await post(admin, f"/batches/{batch_id}/kill"))["runs_killed"] == 1
    (event,) = query(
        database, "SELECT kind,product_id,data->>'reason',data->>'status' FROM test_events"
    )
    assert event == ("pulse_finished", None, "operator", "killed")
    with pytest.raises(ValueError):
        await views.stop_batch(db, batch_id, "done")


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
