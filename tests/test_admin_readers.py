"""Admin JSON API (/admin/api): every reader's shape and the schedule edits."""

from datetime import timedelta

import pytest
from admin_cases import CREDENTIAL, census, get, plan_pulses, post, serve
from orchestrator_cases import bearer, prepare, query

from proxy_index.orchestrator.admin import routes, writers
from proxy_index.orchestrator.app import build_app
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.storage import connect

pytest_plugins = ["test_storage", "admin_cases"]


# --- readers --------------------------------------------------------------------------------


async def test_dashboard_shape(database, db, admin, monkeypatch):
    batch_id, _ = await census(database, db)
    await plan_pulses(db, monkeypatch)
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
    await plan_pulses(db)
    body = await get(admin, "/batches")
    assert body["filters"] == {"kind": None, "status": None, "country": None, "limit": 100}
    # The census, two pulses and the load wave in fra1's reference country (DE).
    assert len(body["batches"]) == 4
    load = (await get(admin, "/batches", kind="load"))["batches"]
    assert [(r["kind"], r["country_code"]) for r in load] == [("load", "DE")]
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
    assert set(body) == {"now", "regions", "countries", "targets", "scheduler"}
    assert body["scheduler"] == {"census": True, "pulse": True, "census_every_minutes": None}
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
            await writers.set_target(conn, "generic_http", {"url": "x"})


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
    now = await plan_pulses(db)
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
    future = (now + timedelta(days=1)).date().isoformat()
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
