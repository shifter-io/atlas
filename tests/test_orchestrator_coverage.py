"""COVERAGE-ALERT-1 (#1216): the coverage job raises one Slack alert per network and day when a
browser target, the pulses or the daily tests stop running, never for a paused network, and
clears it once the network has run (orchestrator/coverage.py), against an isolated PostgreSQL."""

from __future__ import annotations

import json
import tomllib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from orchestrator_cases import query, set_setting
from psycopg.types.json import Jsonb
from test_browser_settings import write

from atlas.daily import DAILY, MARKER
from atlas.detect import METHODOLOGY_FILE
from atlas.orchestrator.coverage import KIND, Gap, coverage, gaps, plan, short
from atlas.orchestrator.db import Database
from atlas.orchestrator.nodes import BROWSER_REGION_ROW, BROWSER_REGION_SQL
from atlas.regions import CONFIGS
from atlas.storage import connect, seed

pytest_plugins = ["test_storage"]

NOW = datetime(2027, 1, 1, 12, tzinfo=UTC)
HOUR = timedelta(hours=1)
ALERTS = (
    "SELECT alert_id,message,occurrences,acknowledged_at IS NULL AS open,severity FROM alerts "
    "WHERE kind=%s ORDER BY alert_id"
)
FLEET = {"provider_config": "p.json", "credentials": "c.toml", "product": "residential"}


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()


def manager_root(tmp_path):
    """alpha runs everything; beta is paused everywhere (netpause's three places)."""
    root = tmp_path / "manager"
    for folder, network in (("formulas", "alpha"), ("formulas-paused", "beta")):
        (root / "daily" / folder).mkdir(parents=True)
        (root / "daily" / folder / f"{network}.json").write_text("{}")
    (root / "pulse").mkdir()
    (root / "pulse" / "fleet.json").write_text(json.dumps({"networks": {"alpha": FLEET}}))
    (root / "pulse" / "paused-networks.json").write_text(json.dumps({"beta": FLEET}))
    return root


def browser_file(tmp_path):
    doc = tomllib.loads((CONFIGS / "browser-pulse.example.toml").read_text())
    doc["enabled"] = True
    doc["host"] = [{"name": "browser-1", "networks": ["alpha/residential", "beta/paused"]}]
    del doc["networks"]
    return write(tmp_path, doc), [target["id"] for target in doc["target"]]


def stored(cur, product, at, *, kind=None, target="generic_http"):
    """A completed run finished at `at`: daily (kind None) or in a pulse or browser batch."""
    pool = cur.execute(
        "INSERT INTO pool_stats(product_id,pool_hash,target_id,requested_country) "
        "VALUES (%s,%s,%s,'de') RETURNING pool_id",
        (product, uuid.uuid4().hex, target),
    ).fetchone()[0]
    scope = cur.execute(
        "INSERT INTO scope_stats(pool_id,product_id,target,scope_hash,concurrency,"
        "timeout_seconds) VALUES (%s,%s,%s,%s,25,15) RETURNING scope_id",
        (pool, product, target, uuid.uuid4().hex),
    ).fetchone()[0]
    key = cur.execute(
        "INSERT INTO test_runs(run_id,scope_id,started_at,finished_at,status,summary_sha256,"
        "summary) VALUES (%s,%s,%s,%s,'completed','x',%s) RETURNING run_key",
        (uuid.uuid4(), scope, at - HOUR / 4, at, Jsonb({} if kind else {MARKER: DAILY})),
    ).fetchone()[0]
    if kind is None:
        return
    country, region = ("US", BROWSER_REGION_ROW[0]) if kind == "browser" else ("DE", None)
    if region is None:
        region = cur.execute("SELECT region_id FROM countries WHERE country_code='DE'").fetchone()
        region = region[0]
    batch = cur.execute(
        "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,start_at,"
        "status) VALUES (%s,%s,%s,%s,%s,%s,'done') RETURNING batch_id",
        (kind, country, region, target, at - HOUR / 4 + timedelta(seconds=key), at - HOUR / 4),
    ).fetchone()[0]
    cur.execute(
        "INSERT INTO batch_runs(batch_id,product_id,status,run_key) VALUES (%s,%s,'done',%s)",
        (batch, product, key),
    )


def verdict(cur, product, counts, at=NOW - 10 * 24 * HOUR):
    """The network's US daily test verdict from `at`: whether its browser runs count (50 IPs,
    else none: under any minimum)."""
    cur.execute(
        "INSERT INTO country_verdicts(product_id,country_code,decided_at,run_key,unique_ips) "
        "VALUES (%s,'US',%s,%s,%s)",
        (product, at, int(at.timestamp()), 50 if counts else 0),
    )


async def stock(database, db, tmp_path, browser_ages):
    """alpha's daily test 2 h ago, its pulse 1 h ago and each browser target `browser_ages` h
    ago (in settings order); beta, paused, has run nothing. Returns the root and target ids."""
    seed(database)
    path, targets = browser_file(tmp_path)
    with connect(database) as conn, conn.cursor() as cur:
        cur.execute(BROWSER_REGION_SQL, BROWSER_REGION_ROW)
        cur.execute("INSERT INTO providers VALUES ('alpha'),('beta') ON CONFLICT DO NOTHING")
        alpha = cur.execute(
            "INSERT INTO products(provider,name) VALUES ('alpha','residential'),"
            "('beta','residential') RETURNING product_id"
        ).fetchone()[0]
        verdict(cur, alpha, True)
        stored(cur, alpha, NOW - 2 * HOUR)
        stored(cur, alpha, NOW - HOUR, kind="pulse")
        for target, age in zip(targets, browser_ages(targets), strict=True):
            stored(cur, alpha, NOW - age * HOUR, kind="browser", target=target)
    for key, value in (
        ("pulse.on_demand", True),
        ("browser.enabled", True),
        ("browser.settings_file", str(path)),
    ):
        await set_setting(db, key, value)
    return manager_root(tmp_path), targets, alpha


async def test_a_missing_browser_target_alerts_once_a_day_and_clears_after_a_run(
    database, db, tmp_path
):
    root, targets, alpha = await stock(database, db, tmp_path, lambda t: [30] + [2] * (len(t) - 1))
    first = await coverage(db, root, NOW)
    words = f"browser target {targets[0]} has no counted run for 30 hours"
    assert first["missing"] == {"alpha": [words]}
    assert first["paused"] == {"browser": ["beta"], "daily": ["beta"], "pulses": ["beta"]}
    [alert] = query(database, ALERTS, (KIND,))
    assert alert[1] == f"alpha: {words}. Its score may become unranked until it runs again."
    assert (alert[2], alert[3], alert[4]) == (1, True, "major")
    # Two hours later: the same row (no second Slack post), its message and count updated.
    await coverage(db, root, NOW + 2 * HOUR)
    [again] = query(database, ALERTS, (KIND,))
    assert (again[0], again[2], again[3]) == (alert[0], 2, True)
    assert "has no counted run for 32 hours" in again[1]
    # The pair runs (and the pulses go on): the alert clears.
    with connect(database) as conn, conn.cursor() as cur:
        stored(cur, alpha, NOW + 2 * HOUR, kind="browser", target=targets[0])
        stored(cur, alpha, NOW + 2 * HOUR, kind="pulse")
    cleared = await coverage(db, root, NOW + 3 * HOUR)
    assert (cleared["missing"], cleared["cleared"]) == ({}, 1)
    assert [row[3] for row in query(database, ALERTS, (KIND,))] == [False]


async def test_a_network_with_every_target_run_raises_nothing(database, db, tmp_path):
    root, _, _ = await stock(database, db, tmp_path, lambda t: [2] * len(t))
    assert (await coverage(db, root, NOW))["missing"] == {}
    assert query(database, ALERTS, (KIND,)) == []


async def test_a_gap_on_the_next_day_posts_again_and_the_old_row_closes(database, db, tmp_path):
    root, targets, _ = await stock(database, db, tmp_path, lambda t: [30] + [2] * (len(t) - 1))
    await coverage(db, root, NOW)
    # Next day: pulses and daily tests stopped too, all named in the day's one new row.
    nextday = await coverage(db, root, NOW + 25 * HOUR)
    words = [f"browser target {t} has no counted run for 27 hours" for t in targets]
    words[0] = words[0].replace("27", "55")
    words += ["no pulse stored for 26 hours", "no daily test stored for 27 hours"]
    assert nextday["missing"]["alpha"] == words
    rows = query(database, ALERTS, (KIND,))
    assert [(row[2], row[3]) for row in rows] == [(1, False), (1, True)]


async def test_off_parts_and_a_pair_never_run(database, db, tmp_path):
    """Browser off and pulses off: only the daily tests are due; with none stored for 8 days
    the alert says so."""
    seed(database)
    await set_setting(db, "browser.enabled", False)
    result = await coverage(db, manager_root(tmp_path), NOW)
    assert result["missing"] == {"alpha": ["no daily test stored in the last 8 days"]}
    assert result["paused"] == {"daily": ["beta"]}
    [alert] = query(database, ALERTS, (KIND,))
    data = query(database, "SELECT data FROM alerts WHERE alert_id=%s", (alert[0],))[0][0]
    assert short(data) == "alpha (daily 8+ d)"


def test_a_paused_network_is_never_missing_and_short_names_the_gaps(tmp_path):
    settings = SimpleNamespace(
        targets=[SimpleNamespace(id="x_web")],
        hosts=[SimpleNamespace(networks=("alpha/residential", "beta/paused"))],
    )
    found = plan(manager_root(tmp_path), settings, pulses=True)
    assert found.due["browser"] == {"alpha": ("x_web",)}
    # beta is paused for the browser and listed nowhere as due or missing.
    limits = dict.fromkeys(found.due, timedelta(hours=26))
    newest = {("daily", "alpha", None): NOW, ("pulses", "alpha", None): NOW}
    assert gaps(found, newest, NOW, limits) == [Gap("alpha", "browser", "x_web", None)]
    data = {"network": "alpha", "gaps": [{"part": "browser", "target": "x_web", "hours": 30}]}
    assert short(data) == "alpha (browser x_web 30 h)"
    # A missing manager root plans nothing.
    assert plan(tmp_path / "none", None, pulses=True).due == {
        "browser": {},
        "pulses": {},
        "daily": {},
    }


async def test_a_browser_run_in_a_country_that_does_not_count_is_missing(database, db, tmp_path):
    """The calculation counts a browser run only in a country that counted for its network at
    the run's start (country_verdicts): from a verdict under 10 IPs on, its runs are missing."""
    root, targets, alpha = await stock(database, db, tmp_path, lambda t: [2] * len(t))
    with connect(database) as conn, conn.cursor() as cur:
        verdict(cur, alpha, False, at=NOW - 30 * HOUR)
    minimum = tomllib.loads(METHODOLOGY_FILE.read_text())["country_min_ips"]
    found = await coverage(db, root, NOW, verdicts=minimum)
    assert found["missing"]["alpha"] == [
        f"browser target {t} has no counted run in the last 8 days" for t in targets
    ]
    # Without the methodology's country_verdicts every run counts.
    assert (await coverage(db, root, NOW, verdicts=None))["missing"] == {}


class Jobs:
    """A scheduler that keeps each registered job, to run it as the orchestrator does."""

    def __init__(self):
        self.jobs = {}

    def add(self, name, job, **schedule):
        self.jobs[name] = job


@pytest.mark.country_verdicts  # the real policy.country_verdicts, not conftest's stand-in
@pytest.mark.parametrize("name", ["coverage", "login_pause"])
async def test_the_registered_job_runs_with_the_real_methodology_and_database(
    database, db, tmp_path, monkeypatch, name
):
    """COVERAGE-JOB-FIX-1 (#1238): the jobs added with #1218 and #1209, as job_table registers
    them: the real methodology file (policy.load() returns (settings, id), which the coverage
    job passed whole to country_verdicts(): AttributeError 'tuple' object has no attribute
    'get' in production) and the test database's own rows, not hand-made ones."""
    from atlas.campaign import collector
    from atlas.orchestrator import job_table

    seed(database)
    monkeypatch.setattr(collector, "MANAGER_ROOT", manager_root(tmp_path))
    scheduler = Jobs()
    job_table.register_jobs(scheduler, db, "db.toml")
    result = await scheduler.jobs[name]()
    assert isinstance(result, dict)
    if name == "coverage":  # alpha's daily tests: none stored, so missing; beta paused
        assert result["missing"] == {"alpha": ["no daily test stored in the last 8 days"]}
        assert result["paused"] == {"daily": ["beta"]}
