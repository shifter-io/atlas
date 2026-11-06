"""The browser pulse planner (orchestrator/browser.py, K-BP5): per-target slots in the browser
region, its runs and alerts, browser node tokens, and census and pulse left unchanged."""

import json
from datetime import timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer
from orchestrator_browser_cases import (
    HOUR,
    HOURLY,
    LEAD,
    SLOW,
    browser_batches,
    browser_nodes,
    configure,
    planned_runs,
    settings_file,
    settings_text,
    targets,
)
from orchestrator_cases import bearer, prepare, product_id, query

from proxy_index import report_html
from proxy_index.browser import settings as browser_settings
from proxy_index.browser.contracts import (
    BROWSER_REGION,
    BROWSER_REGION_CLOUD,
    BROWSER_REGION_ID,
    BROWSER_REGION_PATTERN,
    RUN_TARGETS,
)
from proxy_index.cli import main
from proxy_index.orchestrator import browser, nodework
from proxy_index.orchestrator.app import build_app
from proxy_index.orchestrator.browser import ALERT, LATE_END, browser_plan, slot_starts
from proxy_index.orchestrator.db import Database
from proxy_index.orchestrator.jobs import register_jobs
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.orchestrator.scheduler import Scheduler
from proxy_index.regions import REGION_IDS, region_id, resolve_region
from proxy_index.storage import connect, initialize

pytest_plugins = ["test_storage"]


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()


@pytest.fixture
async def client(database, db):
    test_client = TestClient(TestServer(build_app(database, with_scheduler=False, db=db)))
    await test_client.start_server()
    yield test_client
    await test_client.close()


@pytest.fixture(autouse=True)
def fresh_cache():
    browser._LOADED.clear()
    yield
    browser._LOADED.clear()


# --- regions ----------------------------------------------------------------------------------


def test_the_browser_region_is_a_fallback_beside_the_cloud_regions():
    assert resolve_region("browser") == resolve_region(" Browser-1 ") == BROWSER_REGION
    assert resolve_region("prod-fra1-shifter") == "fra1"
    # A cloud pattern wins; the browser region is only the fallback.
    assert resolve_region("browser-fra1") == "fra1"
    with pytest.raises(ValueError, match="matches 0 regions"):
        resolve_region("mini-one")
    assert region_id(BROWSER_REGION) == BROWSER_REGION_ID == 20
    assert region_id("fra1") == REGION_IDS["fra1"] == 2
    with pytest.raises(KeyError):
        region_id("mars1")
    assert BROWSER_REGION not in REGION_IDS and len(REGION_IDS) == 8


# --- slots ------------------------------------------------------------------------------------


def test_on_time_slots_spread_each_target_over_its_own_interval():
    for now in (HOUR, HOUR + timedelta(seconds=60)):
        starts = slot_starts(targets(), now, LEAD)
        assert [(t, slot) for t, slot, _ in starts] == [(t, HOUR) for t, _ in targets()]
        # Target i of 8 starts i/8 of its own interval into the slot, after the lead.
        assert [start for _, _, start in starts] == [
            HOUR + LEAD + timedelta(minutes=minutes) * i / 8
            for i, (_, minutes) in enumerate(targets())
        ]
    # The next, odd hour: the hourly targets have a new slot, the 120-minute ones do not.
    odd = slot_starts(targets(), HOUR + timedelta(hours=1), LEAD)
    assert {slot for _, slot, _ in odd[:6]} == {HOUR + timedelta(hours=1)}
    assert {slot for _, slot, _ in odd[6:]} == {HOUR}


def test_a_late_slot_spreads_over_what_is_left_of_it():
    now = HOUR + timedelta(minutes=30)
    starts = slot_starts(targets(), now, LEAD)
    assert len(starts) == 8
    # 20 minutes are left of the hourly slots (minus LATE_END): i/8 of that apart from now.
    assert [start for _, _, start in starts[:6]] == [
        now + LEAD + timedelta(minutes=20) * i / 8 for i in range(6)
    ]
    end = HOUR + timedelta(hours=1) - LATE_END
    assert all(now < start <= end for _, _, start in starts[:6])
    # One second past the grace is late; 55 minutes late leaves nothing of an hourly slot.
    late = slot_starts(targets(), HOUR + timedelta(seconds=61), LEAD)
    assert late[1][2] == HOUR + timedelta(seconds=61) + LEAD + timedelta(minutes=49, seconds=-1) / 8
    assert slot_starts(targets(slow=0), HOUR + timedelta(minutes=55), LEAD) == []
    # A start that would fall after the slot end minus LATE_END waits for the next slot.
    almost = slot_starts(targets(slow=0), HOUR + timedelta(minutes=49, seconds=30), LEAD)
    assert [target for target, _, _ in almost] == []
    some = slot_starts(targets(slow=0), HOUR + timedelta(minutes=48), LEAD)
    # Starts 20 s apart from 49:00; the one exactly at the slot end minus LATE_END still opens.
    assert [target for target, _, _ in some] == ["h0", "h1", "h2", "h3"]


# --- planning ---------------------------------------------------------------------------------


async def test_browser_plan_opens_each_target_on_its_own_interval(database, db, tmp_path):
    prepare(database)
    nodes = browser_nodes(database)
    await configure(db, settings_file(tmp_path))

    result = await browser_plan(db, HOUR)
    assert result == {"opened": list(RUN_TARGETS), "created": 8, "networks": 2}
    rows = browser_batches(database)
    # One US batch per target in the browser region, every/8 apart within each target's slot.
    assert [r[1:6] for r in rows] == [
        (
            target,
            "US",
            BROWSER_REGION_ID,
            HOUR,
            HOUR + LEAD + timedelta(minutes=120 if target in SLOW else 60) * i / 8,
        )
        for i, target in enumerate(RUN_TARGETS)
    ]
    runs = query(
        database,
        "SELECT b.target_id,p.provider,r.node_id,r.status FROM batch_runs r "
        "JOIN test_batches b USING(batch_id) JOIN products p USING(product_id) "
        "WHERE b.kind='browser' ORDER BY b.start_at,p.provider",
    )
    assert runs == [
        (target, provider, nodes[provider]["node_id"], "queued")
        for target in RUN_TARGETS
        for provider in ("alpha", "beta")
    ]
    # A second tick in the same slot opens nothing.
    again = await browser_plan(db, HOUR + timedelta(seconds=30))
    assert again == {"opened": [], "created": 0, "networks": 2}
    # The next hour opens only the hourly targets; the following even hour all eight.
    hour = await browser_plan(db, HOUR + timedelta(hours=1))
    assert hour["opened"] == list(HOURLY) and hour["created"] == 6
    even = await browser_plan(db, HOUR + timedelta(hours=2))
    assert even["opened"] == list(RUN_TARGETS) and even["created"] == 8
    slots = query(
        database,
        "SELECT target_id,count(*) FROM test_batches WHERE kind='browser' GROUP BY 1 ORDER BY 1",
    )
    assert slots == sorted((t, 2 if t in SLOW else 3) for t in RUN_TARGETS)
    assert query(database, "SELECT count(*) FROM alerts") == [(0,)]
    assert query(database, "SELECT count(*) FROM test_events") == [(0,)]


async def test_a_late_tick_opens_what_still_fits(database, db, tmp_path):
    prepare(database)
    browser_nodes(database)
    await configure(db, settings_file(tmp_path))
    odd = HOUR + timedelta(hours=1)

    # 30 minutes late: the hourly slot has 20 minutes left, the 120-minute one 20 too.
    late = await browser_plan(db, odd + timedelta(minutes=30))
    assert late["opened"] == list(RUN_TARGETS)
    for _, target, _, _, slot, start, _ in browser_batches(database):
        assert slot == (HOUR if target in SLOW else odd)
        assert odd + timedelta(minutes=30) < start <= odd + timedelta(minutes=50)
    # The next slot is normal again.
    after = browser_batches(database)[-1][0]
    assert (await browser_plan(db, odd + timedelta(hours=1)))["created"] == 8
    assert [r[5] for r in browser_batches(database, after)] == [
        odd + timedelta(hours=1) + LEAD + timedelta(minutes=120 if t in SLOW else 60) * i / 8
        for i, t in enumerate(RUN_TARGETS)
    ]


async def test_a_tick_55_minutes_late_waits_for_the_next_slot(database, db, tmp_path):
    prepare(database)
    browser_nodes(database)
    await configure(db, settings_file(tmp_path))
    odd = HOUR + timedelta(hours=1)
    assert await browser_plan(db, odd + timedelta(minutes=55)) == {
        "opened": [],
        "created": 0,
        "networks": 2,
    }
    assert browser_batches(database) == []
    normal = await browser_plan(db, odd + timedelta(hours=1))
    assert normal["opened"] == list(RUN_TARGETS)
    assert browser_batches(database)[0][5] == odd + timedelta(hours=1) + LEAD


async def test_the_planner_is_off_unless_both_switches_are_on(database, db, tmp_path):
    prepare(database)
    browser_nodes(database)
    assert await browser_plan(db, HOUR) == {"skipped": "browser.enabled is off"}
    await configure(db, None)
    assert await browser_plan(db, HOUR) == {"skipped": "browser.settings_file is not set"}
    await configure(db, settings_file(tmp_path, settings_text(enabled=False)))
    assert await browser_plan(db, HOUR) == {
        "skipped": "the browser settings file has enabled = false"
    }
    await configure(db, settings_file(tmp_path), enabled=False)
    assert await browser_plan(db, HOUR) == {"skipped": "browser.enabled is off"}
    assert browser_batches(database) == []

    # The job records the skip.
    scheduler = Scheduler(db)
    register_jobs(scheduler, db, str(database))
    task = scheduler.tasks["browser_plan"]
    assert task.every_s == 60 and task.aligned
    await scheduler.run_now("browser_plan")
    assert query(database, "SELECT status,error FROM job_runs WHERE job='browser_plan'") == [
        ("skipped", "browser.enabled is off")
    ]


async def test_an_invalid_settings_file_alerts_once_and_plans_nothing(
    database, db, tmp_path, monkeypatch
):
    prepare(database)
    browser_nodes(database)
    path = settings_file(tmp_path, settings_text().replace("schema = 2", "schema = 3"))
    await configure(db, path)
    loads = []
    real = browser_settings.load_settings
    monkeypatch.setattr(browser, "load_settings", lambda p: loads.append(p) or real(p))

    for minute in range(2):
        skipped = await browser_plan(db, HOUR + timedelta(minutes=minute))
        assert skipped == {"skipped": "the browser settings file is invalid"}
    # Parsed once: the file's bytes did not change between the ticks.
    assert len(loads) == 1
    alerts = query(database, "SELECT kind,severity,occurrences,data,acknowledged_at FROM alerts")
    assert alerts == [(ALERT, "major", 2, {"error": "schema: must be 2"}, None)]
    assert browser_batches(database) == []

    # Fixed: parsed again, planned, and the alert acknowledged.
    path.write_text(settings_text())
    assert (await browser_plan(db, HOUR + timedelta(minutes=2)))["created"] == 8
    assert len(loads) == 2
    assert query(database, "SELECT acknowledged_at IS NOT NULL FROM alerts") == [(True,)]

    # A file that cannot be read alerts the same way.
    await configure(db, tmp_path / "missing.toml")
    skipped = await browser_plan(db, HOUR + timedelta(minutes=3))
    assert skipped == {"skipped": "the browser settings file is invalid"}
    assert query(database, "SELECT occurrences,data->>'error' FROM alerts") == [
        (3, "settings: the file cannot be read")
    ]


async def test_batches_without_a_browser_node_close_at_once(database, db, tmp_path):
    prepare(database)
    issue_token(database, provider="gamma", product="res", region="fra1")
    await configure(db, settings_file(tmp_path))
    # No browser token yet: the planner creates the browser region's row itself.
    assert query(database, "SELECT 1 FROM runner_regions WHERE region_id=%s", (20,)) == []
    result = await browser_plan(db, HOUR)
    assert result == {"opened": list(RUN_TARGETS), "created": 8, "networks": 0}
    assert {r[6] for r in browser_batches(database)} == {"failed"}
    assert query(database, "SELECT count(*) FROM batch_runs") == [(0,)]
    assert query(
        database, "SELECT slug,cloud,pattern FROM runner_regions WHERE region_id=%s", (20,)
    ) == [(BROWSER_REGION, BROWSER_REGION_CLOUD, BROWSER_REGION_PATTERN)]
    await db.drain()


# --- census and pulse are unchanged -------------------------------------------------------------


async def test_browser_nodes_change_no_census_or_pulse_run(databases):
    plain, with_browser = databases(), databases()
    for config in (plain, with_browser):
        initialize(config)
    expected = await planned_runs(plain, with_browser=False)
    assert expected[0]["created"] == 1 and expected[1]["created"] >= 1 and expected[2]
    assert await planned_runs(with_browser, with_browser=True) == expected
    # No no_node alert for beta's missing cloud node, and no pulse batch of a browser target.
    assert query(with_browser, "SELECT count(*) FROM alerts") == [(0,)]
    assert query(
        with_browser,
        "SELECT count(*) FROM test_batches b JOIN targets t USING(target_id) "
        "WHERE t.engine='browser' OR b.region_id=%s",
        (BROWSER_REGION_ID,),
    ) == [(0,)]
    beta = product_id(with_browser, "beta")
    assert query(with_browser, "SELECT count(*) FROM batch_runs WHERE product_id=%s", (beta,)) == [
        (0,)
    ]


# --- tokens -----------------------------------------------------------------------------------


def test_browser_tokens_are_bound_to_a_logical_host(database, capsys):
    prepare(database)
    for host in (None, "mini-one", "Browser-1", "browser-"):
        with pytest.raises(ValueError, match="browser-<n>"):
            issue_token(database, provider="alpha", product="res", region="browser", host=host)
    command = [
        "node-token",
        "--database-file",
        str(database),
        "--provider",
        "alpha",
        "--product",
        "res",
        "--region",
        "browser",
    ]
    assert main(command) == 2
    capsys.readouterr()
    assert main([*command, "--host", "browser-1"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["region"] == BROWSER_REGION and not first["rotated"]
    again = issue_token(
        database, provider="alpha", product="res", region="browser", host="browser-1"
    )
    assert again["rotated"] and again["node_id"] == first["node_id"]
    assert again["token"] != first["token"]
    with connect(database) as conn:
        nodes = conn.execute("SELECT region_id,host FROM nodes").fetchall()
    assert nodes == [(BROWSER_REGION_ID, "browser-1")]


async def test_a_browser_node_registers_by_its_host_name(database, client):
    prepare(database)
    node = issue_token(
        database, provider="alpha", product="res", region="browser", host="browser-1"
    )
    response = await client.post(
        "/v1/register",
        json={"provider": "alpha", "product": "res", "host": "browser-1"},
        headers=bearer(node["token"]),
    )
    assert response.status == 200
    assert (await response.json())["region"] == BROWSER_REGION


# --- the rest of the run's life is unchanged ----------------------------------------------------


async def test_browser_runs_are_assigned_reaped_and_reported(database, db, tmp_path):
    prepare(database)
    nodes = browser_nodes(database)
    await configure(db, settings_file(tmp_path))
    await browser_plan(db, HOUR)
    first = browser_batches(database)[0]
    node = {"node_id": nodes["alpha"]["node_id"]}
    work = await nodework.assignments(db, node, HOUR)
    assert [a["batch_id"] for a in work["assignments"]] == [first[0]]
    handed = work["assignments"][0]
    assert (handed["kind"], handed["region"], handed["country_code"]) == (
        "browser",
        BROWSER_REGION,
        "US",
    )
    assert handed["target_id"] == RUN_TARGETS[0]

    # A run that never starts is failed not_started like any run.
    grace = timedelta(seconds=180)
    reaped = await nodework.reap(db, first[5] + grace + timedelta(seconds=1))
    assert reaped["failed"]["not_started"] == 2 and reaped["closed"] >= 1
    assert query(
        database, "SELECT status,error FROM batch_runs WHERE batch_id=%s", (first[0],)
    ) == [("failed", "not_started"), ("failed", "not_started")]
    assert query(database, "SELECT status FROM test_batches WHERE batch_id=%s", (first[0],)) == [
        ("failed",)
    ]
    await db.drain()

    written = report_html.write(database, tmp_path / "reports", [first[0]])
    assert written["batches"] == [f"batch-{first[0]}.html"]
    page = (tmp_path / "reports" / written["batches"][0]).read_text()
    assert f"Batch {first[0]} · browser" in page
