"""VERIFY-HOUR-1: the read-only health report of one hour, against an isolated PostgreSQL.

The hour's data is fixture rows (no cloud, documentation addresses only): two networks, alpha
(DE due at 13:00, CA at 02:00) and beta (DE at 13:00), each with its daily run, the hour's
pulses on both HTTP targets, observations, a DB-IP release, the tape's daily_test_finished
rows, an x_web browser run and the calculation that covers the hour.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from atlas.cli import main
from atlas.daily import DAILY, MARKER, cli, verify
from atlas.daily.verify import Check, Hour, Report
from atlas.orchestrator.control import write_settings
from atlas.orchestrator.nodes import BROWSER_REGION_ROW, BROWSER_REGION_SQL
from atlas.storage import connect, partition, seed

pytest_plugins = ["test_storage", "calculated_template"]

H = datetime(2027, 1, 1, 13, tzinfo=UTC)
NOW = H + timedelta(minutes=105)  # settled, and the newest result under 90 minutes old
SCHEDULES = {"alpha": {"DE": 13, "CA": 2}, "beta": {"DE": 13}}
TARGETS = ("generic_http", "generic_https")


@pytest.fixture
def formulas(tmp_path):
    folder = tmp_path / "formulas"
    folder.mkdir()
    for network, schedule in SCHEDULES.items():
        formula = {"network": network, "product": "residential", "schedule": schedule}
        (folder / f"{network}.json").write_text(json.dumps(formula))
    return folder


def _run(cur, product, country, at, release, *, daily):
    """A stored run with one observation, enriched with `release`; returns its run_key."""
    pool = cur.execute(
        "INSERT INTO pool_stats(product_id,pool_hash,target_id,requested_country) "
        "VALUES (%s,%s,'generic_http',%s) RETURNING pool_id",
        (product, uuid.uuid4().hex, country.lower()),
    ).fetchone()[0]
    scope = cur.execute(
        "INSERT INTO scope_stats(pool_id,product_id,target,scope_hash,concurrency,"
        "timeout_seconds) VALUES (%s,%s,'generic_http',%s,25,15) RETURNING scope_id",
        (pool, product, uuid.uuid4().hex),
    ).fetchone()[0]
    summary = Jsonb({MARKER: DAILY} if daily else {})
    key = cur.execute(
        "INSERT INTO test_runs(run_id,scope_id,started_at,finished_at,status,summary_sha256,"
        "summary) VALUES (%s,%s,%s,%s,'completed','x',%s) RETURNING run_key",
        (uuid.uuid4(), scope, at, at + timedelta(minutes=15), summary),
    ).fetchone()[0]
    ip = cur.execute(
        "INSERT INTO ip_addresses(ip) VALUES ('192.0.2.1') ON CONFLICT (ip) "
        "DO UPDATE SET ip=excluded.ip RETURNING ip_id"
    ).fetchone()[0]
    cur.execute("INSERT INTO ip_observations VALUES (%s,%s,%s,3,%s,%s)", (at, key, ip, at, at))
    if release is not None:
        cur.execute("INSERT INTO run_enrichments VALUES (%s,%s,'x','{}')", (key, release))
    return key


def _pulse(cur, product, country, target, *, status="done", run_key=None):
    region = cur.execute(
        "SELECT region_id FROM countries WHERE country_code=%s", (country,)
    ).fetchone()[0]
    batch = cur.execute(
        "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,start_at,"
        "status) VALUES ('pulse',%s,%s,%s,%s,%s,'done') ON CONFLICT (kind,country_code,"
        "target_id,scheduled_for) DO UPDATE SET status='done' RETURNING batch_id",
        (country, region, target, H, H + timedelta(minutes=10)),
    ).fetchone()[0]
    cur.execute(
        "INSERT INTO batch_runs(batch_id,product_id,status,requests,run_key) "
        "VALUES (%s,%s,%s,100,%s)",
        (batch, product, status, run_key),
    )


def _pulses(cur, product, country, target, release, problems):
    """One pulse run of the hour; with problems, alpha's CA generic_http failed unstored."""
    if problems and (country, target) == ("CA", "generic_http"):
        _pulse(cur, product, country, target, status="failed")
        return
    key = _run(cur, product, country, H + timedelta(minutes=10), release, daily=False)
    _pulse(cur, product, country, target, run_key=key)


def _browser(cur, product, *, uploaded):
    cur.execute(BROWSER_REGION_SQL, BROWSER_REGION_ROW)
    batch = cur.execute(
        "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,start_at,"
        "status) VALUES ('browser','US',%s,'x_web',%s,%s,'done') RETURNING batch_id",
        (BROWSER_REGION_ROW[0], H - timedelta(hours=3), H - timedelta(hours=3)),
    ).fetchone()[0]
    key = _run(cur, product, "US", H - timedelta(hours=3), None, daily=False) if uploaded else None
    cur.execute(
        "INSERT INTO batch_runs(batch_id,product_id,status,run_key) VALUES (%s,%s,'done',%s)",
        (batch, product, key),
    )


def _calculation(cur):
    cur.execute("INSERT INTO methodologies VALUES ('m','{}')")
    build = cur.execute(
        "INSERT INTO calculation_builds(methodology_id,as_of,input_hash,created_at,published) "
        "VALUES ('m',%s,'h',%s,true) RETURNING build_id",
        (H + timedelta(hours=1), H + timedelta(minutes=66)),
    ).fetchone()[0]
    cur.execute("INSERT INTO calculated_snapshots VALUES (%s,'c','{}')", (build,))
    cur.execute("INSERT INTO current_calculation(build_id) VALUES (%s)", (build,))
    cur.execute(
        "INSERT INTO job_runs(job,started_at,status) VALUES ('calculate',%s,'ok')",
        (H + timedelta(minutes=66),),
    )


def stock(database, *, beta_daily=True, problems=False):
    """The hour's rows; with problems also a failed pulse, an open alert, a failed job and a
    browser run never uploaded."""
    seed(database)
    with connect(database) as conn, conn.cursor() as cur:
        write_settings(conn, {"pulse.on_demand": True})
        for table, moment in (("ip_observations", H - timedelta(hours=3)), ("test_events", H)):
            partition(conn, table, moment)
        partition(conn, "ip_observations", H)
        cur.execute("INSERT INTO providers VALUES ('alpha'),('beta') ON CONFLICT DO NOTHING")
        products = dict(
            cur.execute(
                "INSERT INTO products(provider,name) VALUES ('alpha','residential'),"
                "('beta','residential') ON CONFLICT (provider,name) DO UPDATE "
                "SET name=excluded.name RETURNING provider,product_id"
            ).fetchall()
        )
        release = cur.execute(
            "INSERT INTO dbip_releases(sha256,metadata) VALUES ('r',%s) RETURNING release_id",
            (Jsonb({"built_at": (H - timedelta(days=10)).isoformat()}),),
        ).fetchone()[0]
        for network, product in products.items():
            if network == "alpha" or beta_daily:
                _run(cur, product, "DE", H + timedelta(minutes=5), release, daily=True)
                cur.execute(
                    "INSERT INTO test_events(kind,at,product_id) "
                    "VALUES ('daily_test_finished',%s,%s)",
                    (H + timedelta(minutes=20), product),
                )
            for country in ("DE", "CA") if network == "alpha" else ("DE",):
                for target in TARGETS:
                    _pulses(cur, product, country, target, release, problems)
        _browser(cur, products["alpha"], uploaded=not problems)
        _calculation(cur)
        if problems:
            cur.execute("INSERT INTO alerts(kind,severity,message) VALUES ('disk_low','major','m')")
            cur.execute(
                "INSERT INTO job_runs(job,started_at,status) VALUES ('archive',%s,'failed')", (H,)
            )


def verify_hour(monkeypatch, database, formulas, *extra):
    monkeypatch.setattr(cli, "utc_now", lambda: NOW)
    args = ["daily", "verify-hour", "--formulas", str(formulas), "--database-file", str(database)]
    return main([*args, "--hour", H.isoformat(), *extra])


def test_a_healthy_hour_passes_and_writes_nothing(
    calculated_database, formulas, monkeypatch, capsys
):
    stock(calculated_database)
    opened = []

    def spy(path):
        opened.append(connect(path))
        return opened[-1]

    monkeypatch.setattr(verify, "connect", spy)
    with connect(calculated_database) as conn:
        before = conn.execute("SELECT count(*),max(event_id) FROM test_events").fetchone()
    assert verify_hour(monkeypatch, calculated_database, formulas) == 0
    out = capsys.readouterr().out
    assert out.startswith("verify-hour 2027-01-01 13:00 UTC: OK\n\n"), out
    lines = {line.split()[0]: line for line in out.splitlines()[2:]}
    assert "ok   2 cells, 2 countries due" in lines["daily"]
    assert "ok   3 cells x 2 targets, 600 requests" in lines["pulses"]
    assert "ok   1 runs due" in lines["browser"]
    assert "snapshot" in lines["calculation"] and "last calculate job ok" in lines["calculation"]
    assert "release built 2026-12-22, 10 days old; 0 of 8 runs not enriched" in lines["DB-IP"]
    assert "not ok:" not in out
    assert opened[0].read_only  # every statement ran read-only
    with connect(calculated_database) as conn:
        assert conn.execute("SELECT count(*),max(event_id) FROM test_events").fetchone() == before


def test_an_hour_with_problems_names_the_first_and_exits_3(
    calculated_database, formulas, monkeypatch, capsys
):
    stock(calculated_database, beta_daily=False, problems=True)
    assert verify_hour(monkeypatch, calculated_database, formulas) == 3
    out = capsys.readouterr().out
    assert out.splitlines()[:2] == [
        "verify-hour 2027-01-01 13:00 UTC: FAIL in 6 of 10 sections",
        "first problem: daily tests, beta: 0 of 1 stored, 1 missing (DE)",
    ]
    assert "  FAIL pulses, alpha: 3 of 4 stored, 1 failed (CA generic_http), 400 requests" in out
    assert "  FAIL browser 24h, alpha x_web: 0 of 1 uploaded" in out
    assert "  FAIL events, beta: 0 of 1 daily_test_finished" in out
    assert "1 open: disk_low (major, 1x)" in out and "archive (1x)" in out
    assert verify_hour(monkeypatch, calculated_database, formulas, "--json") == 3
    found = json.loads(capsys.readouterr().out)
    assert found["ok"] is False and found["hour"] == H.isoformat()
    assert found["first_failure"]["subject"] == "beta"
    observed = [c for c in found["checks"] if c["section"] == "observations"]
    assert [(c["subject"], c["ok"]) for c in observed] == [
        ("alpha", True),
        ("beta", True),  # its pulses stored observations
        ("total", True),
    ]


def test_a_young_hour_is_pending_and_flags_runs_without_db_ip(calculated_database, tmp_path):
    seed(calculated_database)
    with connect(calculated_database) as conn, conn.cursor() as cur:
        partition(conn, "ip_observations", H)
        cur.execute("INSERT INTO providers VALUES ('alpha') ON CONFLICT DO NOTHING")
        product = cur.execute(
            "INSERT INTO products(provider,name) VALUES ('alpha','residential') "
            "RETURNING product_id"
        ).fetchone()[0]
        _run(cur, product, "DE", H + timedelta(minutes=5), None, daily=True)
    folder = tmp_path / "formulas"
    folder.mkdir()
    (folder / "alpha.json").write_text(json.dumps({"network": "alpha", "schedule": {"DE": 13}}))
    found = verify.report(calculated_database, folder, Hour(H, H + timedelta(minutes=40)))
    states = {(c.section, c.subject): c.ok for c in found.checks}
    assert states[("daily tests", "alpha")] is True and states[("ingest lag", "total")] is True
    assert states[("events", "alpha")] is None  # its daily_test_finished is not due yet
    assert states[("pulses", "total")] is None and states[("browser 24h", "total")] is None
    assert states[("calculation", "total")] is None
    assert states[("DB-IP", "total")] is False  # stored without its enrichment
    text = verify.render(found)
    assert "pulses        -    off (pulse.on_demand is off)" in text
    assert "no published calculation; no calculate job yet" in text
    assert "DB-IP         FAIL 1 of 1 runs not enriched" in text
    before = verify.report(calculated_database, folder, Hour(H - timedelta(hours=1), H))
    assert [c.detail for c in before.checks if c.section == "DB-IP"] == [
        "no runs stored in the hour"
    ]


def test_the_screen_keeps_to_twenty_network_lines():
    checks = [Check("daily tests", f"n{i}", False, "0 of 1 stored") for i in range(23)]
    text = verify.render(Report(H, (*checks, Check("daily tests", "total", False, "x"))))
    assert text.splitlines()[-1] == "  ... and 3 more (--json)"
    assert verify.tally("pulses", "n", list("abcdefg"), {}, True).detail == (
        "0 of 7 stored, 7 missing (a, b, c, d, e, ...)"
    )


def test_last_hour_is_the_hour_that_just_ended():
    assert verify.hour_of("last-hour", H + timedelta(minutes=7)) == H - timedelta(hours=1)
    assert verify.hour_of("now-hour", H + timedelta(minutes=7)) == H
    assert verify.hour_of("2027-01-01T15:00:00+02:00", H) == H
