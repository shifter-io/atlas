"""Scheduler switches, the census interval mode, staging init and the HTML reports."""

import json
import os
import shlex
import stat
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import psycopg
import pytest
from helpers import scratch_database_name
from orchestrator_cases import prepare, query
from psycopg import sql
from test_census_pulse import scenario

from proxy_index import report_html
from proxy_index.cli import main
from proxy_index.orchestrator import batches, nodework
from proxy_index.orchestrator.db import DEFAULTS, Database, put_setting
from proxy_index.orchestrator.jobs import install_reports, register_jobs
from proxy_index.orchestrator.nodes import issue_token, token_hash
from proxy_index.orchestrator.scheduler import Scheduler
from proxy_index.orchestrator.staging import init, load_profile
from proxy_index.providers.credentials import read_credentials
from proxy_index.regions import CONFIGS
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

NOW = datetime(2026, 10, 1, 11, 59, 30, tzinfo=UTC)


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()


async def setting(db, key, value):
    async with db.connection() as conn:
        await put_setting(conn, key, value)


def jobs(database, name):
    return query(database, "SELECT status,error FROM job_runs WHERE job=%s ORDER BY id", (name,))


# --- scheduler switches ---------------------------------------------------------------------


async def test_scheduler_switches_skip_planning(database, db, capsys):
    prepare(database, ("DE",), DE=datetime.now(UTC).time())
    issue_token(database, provider="alpha", product="res", region="fra1")
    scheduler = Scheduler(db)
    register_jobs(scheduler, db, str(database))

    assert main(["scheduler", "off", "--database-file", str(database)]) == 0
    assert json.loads(capsys.readouterr().out) | {"reports_dir": None} == {
        "census": False,
        "pulse": False,
        "census_every_minutes": None,
        "reports_dir": None,
    }
    await scheduler.run_now("census_plan")
    await scheduler.run_now("pulse_plan")
    assert jobs(database, "census_plan") == [("skipped", "census scheduler is off")]
    assert jobs(database, "pulse_plan") == [("skipped", "pulse scheduler is off")]
    assert query(database, "SELECT count(*) FROM test_batches") == [(0,)]

    # Only pulse back on: pulse plans, census still records a skip.
    assert main(["scheduler", "on", "--pulse", "--database-file", str(database)]) == 0
    assert json.loads(capsys.readouterr().out)["pulse"] is True
    await scheduler.run_now("census_plan")
    await scheduler.run_now("pulse_plan")
    assert jobs(database, "census_plan")[-1] == ("skipped", "census scheduler is off")
    assert jobs(database, "pulse_plan")[-1] == ("ok", None)
    # Pulses, and the load wave the census target's pulse slot adds; no census.
    assert query(database, "SELECT DISTINCT kind FROM test_batches ORDER BY 1") == [
        ("load",),
        ("pulse",),
    ]

    assert main(["scheduler", "on", "--census", "--database-file", str(database)]) == 0
    capsys.readouterr()
    assert main(["scheduler", "status", "--database-file", str(database)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert (status["census"], status["pulse"]) == (True, True)
    await scheduler.run_now("census_plan")
    assert jobs(database, "census_plan")[-1] == ("ok", None)
    assert query(database, "SELECT count(*) FROM test_batches WHERE kind='census'") == [(1,)]


async def test_overlap_skip_count_still_records_ok(database, db):
    """census_plan reports skipped batches as a count; only a reason string skips the job."""
    prepare(database, ("DE",), DE=time(12))
    issue_token(database, provider="alpha", product="res", region="fra1")
    await batches.census_plan(db, NOW)
    # A slot whose start_at (slot + 30 s) has passed, with the first census still open.
    with connect(database) as conn:
        conn.execute(
            "UPDATE countries SET census_time=%s WHERE country_code='DE'",
            ((datetime.now(UTC) - timedelta(seconds=40)).time(),),
        )
    scheduler = Scheduler(db)
    register_jobs(scheduler, db, str(database))
    assert (await scheduler.run_now("census_plan"))["skipped"] == 1
    assert jobs(database, "census_plan") == [("ok", None)]


# --- census interval mode -------------------------------------------------------------------


def test_interval_slots_are_epoch_aligned():
    every = timedelta(minutes=7)
    lo, hi = NOW - timedelta(seconds=60), NOW + timedelta(seconds=60)
    slots = batches.interval_slots(every, lo, hi)
    assert all(s.timestamp() % 420 == 0 and lo <= s < hi for s in slots)
    assert batches.interval_slots(timedelta(minutes=1), lo, hi) == [
        datetime(2026, 10, 1, 11, 59, tzinfo=UTC),
        datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    ]
    assert batches.next_slot(timedelta(minutes=4), NOW) == datetime(2026, 10, 1, 12, tzinfo=UTC)
    assert batches.census_every(None) is None
    for bad in (0, -4, "4", True):
        with pytest.raises(ValueError):
            batches.census_every(bad)


async def test_census_interval_slots_idempotent_and_overlap(database, db):
    # Daily times far from now: in interval mode they no longer matter.
    prepare(database, ("DE", "AT"), DE=time(3), AT=time(4))
    issue_token(database, provider="alpha", product="res", region="fra1")
    await setting(db, "census.every_minutes", 4)
    lead = timedelta(seconds=30)

    def census():
        return query(
            database,
            "SELECT country_code,scheduled_for,start_at,status FROM test_batches "
            "WHERE kind='census' ORDER BY scheduled_for,country_code",
        )

    slot = datetime(2026, 10, 1, 12, 4, tzinfo=UTC)
    first = await batches.census_plan(db, slot + timedelta(milliseconds=200))
    assert first == {"created": 2, "skipped": 0, "deferred": 0, "no_node": 0}
    assert census() == [
        ("AT", slot, slot + lead, "scheduled"),
        ("DE", slot, slot + lead, "scheduled"),
    ]
    # The next minute's tick sees the same slot again: nothing new.
    assert (await batches.census_plan(db, slot + timedelta(minutes=1)))["created"] == 0
    assert (await batches.census_plan(db, slot + timedelta(minutes=2)))["created"] == 0
    assert len(census()) == 2

    # The next slot comes into the window a minute early; the previous censuses are still
    # open, so it waits for its start_at, and is skipped only if they are open then.
    later = slot + timedelta(minutes=4)
    for at in (later - timedelta(seconds=59.8), later + timedelta(milliseconds=200)):
        assert await batches.census_plan(db, at) == {
            "created": 0,
            "skipped": 0,
            "deferred": 2,
            "no_node": 0,
        }
    assert len(census()) == 2
    assert await batches.census_plan(db, later + lead) == {
        "created": 0,
        "skipped": 2,
        "deferred": 0,
        "no_node": 0,
    }
    assert [row[1:] for row in census()[2:]] == [(later, later + lead, "skipped")] * 2
    events = query(
        database, "SELECT country_code,data->>'reason' FROM test_events ORDER BY country_code"
    )
    assert events == [
        ("AT", "previous census still scheduled"),
        ("DE", "previous census still scheduled"),
    ]

    # Once they finished, the following slot runs.
    with connect(database) as conn:
        conn.execute("UPDATE test_batches SET status='done' WHERE scheduled_for=%s", (slot,))
    third = slot + timedelta(minutes=8)
    assert (await batches.census_plan(db, third))["created"] == 2
    assert [row[3] for row in census() if row[1] == third] == ["scheduled", "scheduled"]

    # Back to the daily slot when the setting is cleared.
    await setting(db, "census.every_minutes", None)
    assert (await batches.census_plan(db, slot + timedelta(minutes=12)))["created"] == 0


async def test_census_overlap_is_decided_at_start_time(database, db):
    """A slot whose previous census is still open waits until its start_at: planned when the
    previous one finished in time, skipped when it is still open then, and decided late (with
    start_at moved to now + 5 s) after a restart that missed the ticks around start_at."""
    prepare(database, ("DE",), DE=time(3))
    issue_token(database, provider="alpha", product="res", region="fra1")
    await setting(db, "census.every_minutes", 4)
    tick = timedelta(milliseconds=200)  # aligned ticks fire just after the minute
    lead = timedelta(seconds=30)

    def at(minute, second=0):
        return datetime(2026, 10, 1, 12, minute, second, tzinfo=UTC) + tick

    def batch(slot_minute):
        rows = query(
            database,
            "SELECT batch_id,start_at,status FROM test_batches WHERE kind='census' "
            "AND scheduled_for=%s",
            (datetime(2026, 10, 1, 12, slot_minute, tzinfo=UTC),),
        )
        return rows[0] if rows else None

    def finish(slot_minute, status="done"):
        with connect(database) as conn:
            conn.execute(
                "UPDATE test_batches SET status=%s WHERE kind='census' AND scheduled_for=%s",
                (status, datetime(2026, 10, 1, 12, slot_minute, tzinfo=UTC)),
            )

    async def plan(moment):
        return await batches.census_plan(db, moment)

    assert (await plan(at(4)))["created"] == 1
    finish(4, "running")

    # 1. The previous census finishes before the new start_at: planned normally.
    assert (await plan(at(7)))["deferred"] == 1 and batch(8) is None
    finish(4)
    assert (await plan(at(8)))["created"] == 1
    slot8 = datetime(2026, 10, 1, 12, 8, tzinfo=UTC)
    assert batch(8)[1:] == (slot8 + lead, "scheduled")
    assert (await plan(at(9)))["created"] == 0  # idempotent
    finish(8, "running")

    # 2. Still running at start_at: deferred until then, then skipped with an event.
    assert (await plan(at(11)))["deferred"] == 1
    assert (await plan(at(12)))["deferred"] == 1
    assert batch(12) is None
    assert await plan(at(13)) == {"created": 0, "skipped": 1, "deferred": 0, "no_node": 0}
    slot12 = datetime(2026, 10, 1, 12, 12, tzinfo=UTC)
    assert batch(12)[1:] == (slot12 + lead, "skipped")
    assert query(
        database,
        "SELECT kind,(data->>'previous_batch_id')::bigint FROM test_events WHERE batch_id=%s",
        (batch(12)[0],),
    ) == [("census_skipped", batch(8)[0])]
    assert (await plan(at(14)))["skipped"] == 0  # idempotent

    # 3. A restart misses the ticks around start_at; the previous census finished meanwhile.
    assert (await plan(at(15)))["deferred"] == 1
    finish(8)
    late = at(17, 40)
    assert (await plan(late))["created"] == 1
    assert batch(16)[1:] == (late + timedelta(seconds=5), "scheduled")
    finish(16, "running")
    # ...and a restart while the previous census is still running: skipped when seen again.
    assert (await plan(at(22, 10)))["skipped"] == 1
    assert batch(20)[2] == "skipped"


# --- staging init ---------------------------------------------------------------------------


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return path


def toml(values):
    return "[postgres]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in values.items())


def profile(path, name, countries=("RO", "BG")):
    text = (CONFIGS / "staging.toml").read_text()
    text = text.replace('"proxy_reviews_staging"', json.dumps(name))
    text = text.replace('["RO", "BG"]', json.dumps(list(countries)))
    text = text.replace('"reports/staging"', json.dumps(str(path.parent / "reports")))
    path.write_text(text)
    return path


def test_staging_profile_is_packaged_and_valid():
    loaded = load_profile(CONFIGS / "staging.toml")
    assert loaded["name"] == "proxy_reviews_staging"
    assert loaded["countries"] == ["BG", "RO"]
    assert (loaded["census_every_minutes"], loaded["pulse_interval_minutes"]) == (4, 2)
    assert loaded["nodes"] == [{"provider": "shifter", "product": "residential", "region": "fra1"}]
    assert loaded["reference_defaults"] == {"fra1": "RO"}
    assert loaded["methodology"] == "configs/staging-methodology.toml"


def test_staging_load_country_must_be_in_scope(tmp_path):
    text = (CONFIGS / "staging.toml").read_text()
    for bad, message in (
        (text.replace('fra1 = "RO"', 'fra1 = "US"'), "reference_defaults"),
        (text.replace('"configs/staging-methodology.toml"', '""'), "methodology"),
    ):
        path = tmp_path / "staging.toml"
        path.write_text(bad)
        with pytest.raises(ValueError, match=message):
            load_profile(path)


@pytest.mark.parametrize("name", ["proxy_reviews", "proxy_live_main", "postgres"])
def test_staging_init_refuses_the_real_database(tmp_path, name):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    fake = {"host": "127.0.0.1", "port": "1", "user": "app", "password": "x"}
    private(secrets / "database.toml", toml(fake | {"dbname": "proxy_live_main"}))
    private(secrets / "postgres-admin.toml", toml(fake | {"dbname": "postgres"}))
    with pytest.raises(ValueError, match="Refusing"):
        init(profile(tmp_path / "staging.toml", name), secrets_dir=secrets)
    assert sorted(os.listdir(secrets)) == ["database.toml", "postgres-admin.toml"]


def test_staging_init_needs_an_app_user(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    fake = {"host": "127.0.0.1", "port": "1", "password": "x"}
    private(secrets / "database.toml", toml(fake | {"dbname": "proxy_live_main"}))
    private(secrets / "postgres-admin.toml", toml(fake | {"user": "admin", "dbname": "postgres"}))
    with pytest.raises(ValueError, match="needs a user to own the staging database"):
        init(profile(tmp_path / "staging.toml", "proxy_test_staging"), secrets_dir=secrets)
    assert sorted(os.listdir(secrets)) == ["database.toml", "postgres-admin.toml"]


def test_staging_init_rejects_an_unknown_scope_country(tmp_path):
    admin_file = os.environ.get("PG_TEST_ADMIN_FILE")
    if not admin_file:
        pytest.skip("Set PG_TEST_ADMIN_FILE for isolated real PostgreSQL integration tests")
    admin = read_credentials(admin_file, "postgres")
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    name = scratch_database_name() + "_staging"
    private(secrets / "database.toml", toml(admin | {"dbname": "proxy_test_never_created"}))
    private(secrets / "postgres-admin.toml", toml(admin))
    config = profile(tmp_path / "staging.toml", name, countries=("RO", "ZZ"))
    try:
        with pytest.raises(ValueError, match="^Unknown scope countries: ZZ$"):
            init(config, secrets_dir=secrets)
        assert not (secrets / "staging-tokens").exists()
    finally:
        with psycopg.connect(**admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_staging_init_end_to_end(tmp_path, capsys):
    admin_file = os.environ.get("PG_TEST_ADMIN_FILE")
    if not admin_file:
        pytest.skip("Set PG_TEST_ADMIN_FILE for isolated real PostgreSQL integration tests")
    admin = read_credentials(admin_file, "postgres")
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    name = scratch_database_name()
    # The "production" dbname is another scratch name; the app user is the test admin.
    private(secrets / "database.toml", toml(admin | {"dbname": "proxy_test_never_created"}))
    private(secrets / "postgres-admin.toml", toml(admin))
    config = profile(tmp_path / "staging.toml", name)
    try:
        argv = ["staging", "init", "--config", str(config), "--secrets-dir", str(secrets)]
        assert main(argv) == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["database"] == name and summary["created"] and not summary["dropped"]
        staging_file = secrets / "staging-database.toml"
        assert summary["database_file"] == str(staging_file.resolve())
        assert mode(staging_file) == 0o600
        assert read_credentials(staging_file, "postgres")["dbname"] == name
        assert summary["countries"] == ["BG", "RO"]
        assert summary["reports_dir"] == str((tmp_path / "reports").resolve())

        (node,) = summary["nodes"]
        token_file = Path(node["token_file"])
        assert token_file == (secrets / "staging-tokens/shifter-residential-fra1.token").resolve()
        assert mode(token_file) == 0o600
        assert f"--token-file {token_file}" in node["command"]
        assert "--region fra1" in node["command"]
        methodology = shlex.quote(str(Path("configs/staging-methodology.toml").resolve()))
        assert summary["commands"]["orchestrator"].endswith(
            f"--database-file {staging_file} --methodology {methodology}"
        )
        assert summary["commands"]["calculate"] == (
            f"proxy-index calculate --database-file {staging_file} --methodology {methodology}"
        )
        assert summary["load"] == {
            "reference_defaults": {"fra1": "RO"},
            "concurrency": 100,
            "targets": ["generic_http"],
        }

        with connect(staging_file) as conn:
            enabled = conn.execute(
                "SELECT country_code FROM countries WHERE enabled ORDER BY 1"
            ).fetchall()
            assert enabled == [("BG",), ("RO",)]
            settings = dict(conn.execute("SELECT key,value FROM settings").fetchall())
            assert settings == {
                "census.every_minutes": 4,
                "census.min_sample": 1000,
                "scheduler.census_enabled": True,
                "scheduler.pulse_enabled": True,
                "load.reference_defaults": DEFAULTS["load.reference_defaults"] | {"fra1": "RO"},
                "reports.dir": summary["reports_dir"],
            }
            targets = conn.execute(
                "SELECT target_id,census,census_concurrency,pulse_interval_minutes,"
                "pulse_requests FROM targets WHERE engine='http' ORDER BY sort_order"
            ).fetchall()
            assert targets == [
                ("generic_http", True, 100, 2, 100),
                ("generic_https", False, 1000, 2, 100),
            ]
            hashed = conn.execute(
                "SELECT token_hash FROM nodes WHERE provider='shifter' AND product='residential'"
            ).fetchone()[0]
            assert hashed == token_hash(token_file.read_text().strip())
            # Both layers exist, as db-init creates them.
            assert conn.execute("SELECT to_regclass('calculation_schema')").fetchone()[0]
            conn.execute("INSERT INTO providers VALUES ('leftover')")

        # Again without --clean: the database stays; with --clean it is recreated empty.
        assert main(argv) == 0
        again = json.loads(capsys.readouterr().out)
        assert not again["created"] and again["nodes"][0]["rotated"]
        assert main([*argv, "--clean"]) == 0
        clean = json.loads(capsys.readouterr().out)
        assert clean["created"] and clean["dropped"]
        with connect(staging_file) as conn:
            assert conn.execute("SELECT id FROM providers ORDER BY 1").fetchall() == [("shifter",)]
    finally:
        with psycopg.connect(**admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


# --- HTML reports ---------------------------------------------------------------------------


def text(path):
    return Path(path).read_text()


async def test_batch_report_and_index_numbers(database, tmp_path, capsys):
    _, ids = await scenario(database, tmp_path)
    census_batch = ids["census_batch"]
    with connect(database) as conn:
        conn.execute(
            "UPDATE batch_runs SET requests=1234567,bytes_down=12345678 "
            "WHERE batch_id=%s AND product_id=%s",
            (census_batch, ids["alpha"]),
        )
    out = tmp_path / "reports"
    result = report_html.write(database, out, [census_batch])
    assert result["batches"] == [f"batch-{census_batch}.html"]
    page = text(out / f"batch-{census_batch}.html")
    assert "Test #1 · Census" in page and "Germany (DE) from fra1" in page
    assert "alpha/residential" in page and "beta/residential" in page
    assert "1,234,567" in page and "12.35 MB" in page
    # Unique share: 1 unique IP over 3 successes; one address new to alpha.
    assert "33.3%" in page
    assert "Stopped on the unique-IP rule" in page
    assert "11 ms" in page  # p50 of the FakeTransport latency
    assert "Berlin" in page and "AS3320" in page and "Deutsche Telekom AG" in page
    assert "<script" not in page and "http-equiv" not in page
    assert "prefers-color-scheme:dark" in page

    index = text(out / "index.html")
    assert '<meta http-equiv="refresh" content="30">' in index
    assert f'href="batch-{census_batch}.html"' in index
    assert "Census scheduler" in index and "Next pulse slots" in index
    assert "Unique IPs per country and product" in index
    assert "<script" not in index

    # The CLI writes every listed batch and links them all.
    every = tmp_path / "every"
    assert main(["report", "html", "--database-file", str(database), "--out", str(every)]) == 0
    written = json.loads(capsys.readouterr().out)["batches"]
    assert len(written) == len(query(database, "SELECT 1 FROM test_batches"))
    for name in written:
        assert f'href="{name}"' in text(every / "index.html")


async def test_barrier_hook_writes_reports_and_never_fails(database, db, tmp_path):
    now = datetime.now(UTC)
    prepare(database, ("DE",), DE=now.time())
    node = issue_token(database, provider="alpha", product="res", region="fra1")
    install_reports(db)
    out = tmp_path / "reports"

    # Without reports.dir nothing is written.
    await batches.census_plan(db, now)
    batch_id = query(database, "SELECT batch_id FROM test_batches")[0][0]
    await setting(db, "reports.dir", str(out))
    await db.drain()
    assert not out.exists()

    assert await nodework.fail_run(db, node, batch_id, "boom", now) == "failed"
    await db.drain()
    assert query(database, "SELECT status FROM test_batches") == [("failed",)]
    assert f'href="batch-{batch_id}.html"' in text(out / "index.html")
    page = text(out / f"batch-{batch_id}.html")
    assert "failed" in page and "boom" in page

    # A census skipped for overlap is closed at once and reported too.
    with connect(database) as conn:
        conn.execute("UPDATE test_batches SET status='running' WHERE batch_id=%s", (batch_id,))
        conn.execute(
            "UPDATE countries SET census_time=%s WHERE country_code='DE'",
            ((now + timedelta(minutes=10)).time(),),
        )
    await batches.census_plan(db, now + timedelta(minutes=10, seconds=30))
    await db.drain()
    skipped = query(database, "SELECT batch_id FROM test_batches WHERE status='skipped'")[0][0]
    assert "census_skipped" in text(out / f"batch-{skipped}.html")

    # An unwritable directory is logged; the barrier still closes the batch.
    blocker = tmp_path / "file"
    blocker.write_text("")
    await setting(db, "reports.dir", str(blocker / "sub"))
    with connect(database) as conn:
        conn.execute("UPDATE test_batches SET status='running' WHERE batch_id=%s", (skipped,))
        conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,node_id,status) "
            "SELECT %s,product_id,%s,'running' FROM products",
            (skipped, node["node_id"]),
        )
    assert await nodework.fail_run(db, node, skipped, "again") == "failed"
    await db.drain()
    assert query(database, "SELECT status FROM test_batches WHERE batch_id=%s", (skipped,)) == [
        ("failed",)
    ]


async def test_hook_runs_only_after_commit(database, db):
    prepare(database, ("DE",), DE=datetime.now(UTC).time())
    issue_token(database, provider="alpha", product="res", region="fra1")
    await batches.census_plan(db)
    batch_id = query(database, "SELECT batch_id FROM test_batches")[0][0]
    seen = []

    async def hook(ids):
        seen.append(ids)

    db.on_close["probe"] = hook
    with pytest.raises(RuntimeError):
        async with db.transaction() as conn:
            await conn.execute("UPDATE batch_runs SET status='failed'")
            await batches.close_batch(conn, batch_id, datetime.now(UTC))
            raise RuntimeError("roll back")
    await db.drain()
    assert seen == []
    async with db.transaction() as conn:
        await conn.execute("UPDATE batch_runs SET status='failed'")
        await batches.close_batch(conn, batch_id, datetime.now(UTC))
    await db.drain()
    assert seen == [[batch_id]]


# --- live reports ---------------------------------------------------------------------------


def test_rule_progress_and_text():
    assert report_html.rule_progress(500, 400, 1000) == {"sample": 0.5, "share": 0.8, "met": False}
    assert report_html.rule_progress(1200, 500, 1000)["met"] is True
    # Exactly 50% is not below the line (the runner needs successes > 2 x unique IPs).
    assert report_html.rule_progress(1200, 600, 1000)["met"] is False
    assert report_html.rule_progress(0, 0, 1000) == {"sample": 0.0, "share": None, "met": False}
    text_ = report_html.rule_text(1200, 900, 1000)
    assert "minimum sample reached" in text_ and "unique share 75.0%" in text_
    assert "25.0 points above the 50% stop line" in text_
    assert "500 of 1,000 completed requests for the minimum sample (50.0%)" in (
        report_html.rule_text(500, 400, 1000)
    )
    assert report_html.rule_text(1200, 500, 1000).startswith("Stop rule met")


def test_rate_limited_stop_reason_has_its_own_text():
    text_ = report_html.stop_reason("rate_limited", "done")
    assert "rate limiting" in text_ and "RATE_LIMIT_STOP_SECONDS" in text_ and "120 s" in text_
    assert text_ != "Stopped: rate_limited."


async def test_reports_job_writes_live_pages(database, db, tmp_path):
    from proxy_index.orchestrator.admin import writers

    now = datetime.now(UTC)
    prepare(database, ("DE",), DE=now.time())
    alpha = issue_token(database, provider="alpha", product="res", region="fra1")
    issue_token(database, provider="beta", product="res", region="fra1")
    await batches.census_plan(db, now)
    batch_id = query(database, "SELECT batch_id FROM test_batches")[0][0]
    await nodework.assignments(db, alpha, now + timedelta(minutes=1))
    await nodework.progress(
        db,
        alpha,
        batch_id,
        {
            "requests": 1500,
            "successes": 1200,
            "unique_ips": 900,
            "challenges": 3,
            "blocks": 2,
            "timeouts": 40,
            "proxy_errors": 5,
            "bytes_up": 2_000_000,
            "bytes_down": 34_567_890,
        },
    )
    scheduler = Scheduler(db)
    register_jobs(scheduler, db, str(database))
    assert "reports" in scheduler.tasks and scheduler.tasks["reports"].every_s == 30

    await scheduler.run_now("reports")
    assert jobs(database, "reports") == [("skipped", "reports.dir is not set")]

    out = tmp_path / "reports"
    await setting(db, "reports.dir", str(out))
    assert (await scheduler.run_now("reports"))["running"] == 1
    assert jobs(database, "reports")[-1] == ("ok", None)
    page = text(out / f"batch-{batch_id}.html")
    assert "running — live counters, updated" in page
    assert '<meta http-equiv="refresh" content="30">' in page
    for value in ("1,500", "1,200", "900", "34.57 MB", "Elapsed", "Last progress"):
        assert value in page, value
    # Unique share 900 / 1,200 and the distance to the stop rule.
    assert "75.0%" in page and "25.0 points above the 50% stop line" in page
    assert "minimum sample reached" in page

    index = text(out / "index.html")
    assert "1 batch · 1,500 requests" in index
    assert f'href="batch-{batch_id}.html"' in index
    assert "Running now (live)" in index and ">900<" in index

    # Once the batch closes, its page is rewritten from the final data, without the banner.
    await writers.stop_batch(db, batch_id, "killed")
    await db.drain()
    final = text(out / f"batch-{batch_id}.html")
    assert "live counters" not in final and "http-equiv" not in final
    assert "Stopped by an operator" in final
    await scheduler.run_now("reports")
    assert "0 batches · 0 requests" in text(out / "index.html")
