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
HOUR = timedelta(hours=1)
NOW = H + timedelta(minutes=105)  # settled, and the newest result under 90 minutes old
SCHEDULES = {"alpha": {"DE": 13, "CA": 2}, "beta": {"DE": 13}}
TARGETS = ("generic_http", "generic_https")


def _formulas(folder, schedules):
    folder.mkdir()
    for network, schedule in schedules.items():
        formula = {"network": network, "product": "residential", "schedule": schedule}
        (folder / f"{network}.json").write_text(json.dumps(formula))
    return folder


@pytest.fixture
def formulas(tmp_path):
    return _formulas(tmp_path / "formulas", SCHEDULES)


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


def _browser(cur, product, *, uploaded, at=H - timedelta(hours=3)):
    cur.execute(BROWSER_REGION_SQL, BROWSER_REGION_ROW)
    batch = cur.execute(
        "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,start_at,"
        "status) VALUES ('browser','US',%s,'x_web',%s,%s,'done') RETURNING batch_id",
        (BROWSER_REGION_ROW[0], at, at),
    ).fetchone()[0]
    key = _run(cur, product, "US", at, None, daily=False) if uploaded else None
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
    return main([*args, "--hour", H.isoformat(), "--no-cloud", *extra])


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
    enriched = "release built 2026-12-22, 10 days old; 0 of 8 runs not enriched, 0 of 6 pulse runs"
    assert enriched in lines["DB-IP"]
    assert "ok   0 countries with low availability (no pulses)" in lines["expected"]
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
        "verify-hour 2027-01-01 13:00 UTC: FAIL in 6 of 11 sections",
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


def test_a_browser_batch_whose_runs_were_all_excluded_is_not_due(calculated_database, formulas):
    """VERIFY-EXCLUDED-1: an exclusion deletes a done batch's runs; the batch is then not due. A
    skipped batch (no run either) still shows as -, and alpha's uploaded run counts as before."""
    stock(calculated_database)
    with connect(calculated_database) as conn, conn.cursor() as cur:
        beta = cur.execute("SELECT product_id FROM products WHERE provider='beta'").fetchone()[0]
        _browser(cur, beta, uploaded=False, at=H - timedelta(hours=2))
        cur.execute(
            "DELETE FROM batch_runs WHERE product_id=%s AND batch_id IN (SELECT batch_id "
            "FROM test_batches WHERE kind='browser')",
            (beta,),
        )
        cur.execute(
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at,status) VALUES ('browser','US',%s,'x_web',%s,%s,'skipped')",
            (BROWSER_REGION_ROW[0], H - timedelta(hours=1), H - timedelta(hours=1)),
        )
    found = verify.report(calculated_database, formulas, Hour(H, NOW))
    browser = {c.subject: c for c in found.checks if c.section == "browser 24h"}
    assert {s: (c.ok, c.detail) for s, c in browser.items() if s != "total"} == {
        "- x_web": (False, "0 of 1 uploaded"),
        "alpha x_web": (True, "1 of 1 uploaded"),
    }
    assert browser["total"].detail == "2 runs due in the 24 hours to the hour's end"


def test_a_country_with_low_availability_is_not_pulsed_and_is_listed_as_expected(
    calculated_database, formulas, monkeypatch, capsys
):
    # LOWAVAIL-1: alpha's CA found 2 IPs in its daily test before the hour; its pulses are not
    # due (the pulse hour skipped them), so the hour is healthy, and CA is on the expected line.
    stock(calculated_database)
    with connect(calculated_database) as conn:
        alpha = conn.execute("SELECT product_id FROM products WHERE provider='alpha'").fetchone()
        conn.execute(
            "INSERT INTO country_verdicts(product_id,country_code,decided_at,run_key,unique_ips,"
            "counts) VALUES (%s,'CA',%s,-1,2,false)",
            (alpha[0], H - timedelta(hours=11)),
        )
        # CA's next daily test (before the hour) found 40 IPs, but it was stored only after the
        # hour began: the pulse hour, planned at HH:57, did not see it, so neither does the report.
        late = _run(conn.cursor(), alpha[0], "CA", H - timedelta(minutes=50), None, daily=True)
        conn.execute("UPDATE test_runs SET imported_at=%s WHERE run_key=%s", (H + HOUR, late))
        conn.execute(
            "INSERT INTO country_verdicts(product_id,country_code,decided_at,run_key,unique_ips,"
            "counts) VALUES (%s,'CA',%s,%s,40,true)",
            (alpha[0], H - timedelta(minutes=50), late),
        )
    assert verify_hour(monkeypatch, calculated_database, formulas) == 0
    lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()[2:]}
    assert "ok   2 cells x 2 targets" in lines["pulses"]  # alpha's tor1 cell: none left
    assert "ok   1 country with low availability (no pulses): alpha: CA" in lines["expected"]


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


@pytest.mark.parametrize(
    "daily, ok, detail",
    [
        (False, None, "only 1 browser runs (never enriched)"),
        (True, False, "1 of 1 runs not enriched (1 browser runs not counted)"),
    ],
)
def test_db_ip_counts_only_runs_that_can_be_enriched(
    calculated_database, formulas, daily, ok, detail
):
    seed(calculated_database)
    with connect(calculated_database) as conn, conn.cursor() as cur:
        partition(conn, "ip_observations", H)
        cur.execute("INSERT INTO providers VALUES ('alpha') ON CONFLICT DO NOTHING")
        product = cur.execute(
            "INSERT INTO products(provider,name) VALUES ('alpha','residential') "
            "RETURNING product_id"
        ).fetchone()[0]
        _browser(cur, product, uploaded=True, at=H + timedelta(minutes=5))  # no exit IP
        if daily:
            _run(cur, product, "DE", H + timedelta(minutes=5), None, daily=True)
    found = verify.report(calculated_database, formulas, Hour(H, NOW))
    assert [c for c in found.checks if c.section == "DB-IP"] == [
        Check("DB-IP", "total", ok, detail)
    ]


def test_pulse_runs_without_db_ip_fail_the_hour_and_are_named(calculated_database, formulas):
    stock(calculated_database)  # PULSE-ENRICH-1: as the pulse runs of 14 Nov 2026 were stored
    with connect(calculated_database) as conn:
        conn.execute("DELETE FROM run_enrichments e USING batch_runs r WHERE e.run_key=r.run_key")
    found = verify.report(calculated_database, formulas, Hour(H, NOW))
    assert [c for c in found.checks if c.section == "DB-IP"] == [
        Check(
            "DB-IP",
            "total",
            False,
            "release built 2026-12-22, 10 days old; 6 of 8 runs not enriched, 6 of 6 pulse runs",
        )
    ]
    assert not found.ok


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


def test_the_pulses_report_the_cells_planned_against_those_stored_and_list_the_lost(
    calculated_database, formulas
):
    """PULSE-REPLACE-1: a cell (network and region, one droplet) whose droplet never booted has
    no pulse stored: it is listed as lost once the hour has settled."""
    stock(calculated_database)
    with connect(calculated_database) as conn:
        conn.execute(
            "DELETE FROM batch_runs WHERE product_id=(SELECT product_id FROM products "
            "WHERE provider='beta')"
        )
    found = verify.report(calculated_database, formulas, Hour(H, NOW))
    [cells] = [c for c in found.checks if (c.section, c.subject) == ("pulses", "cells")]
    assert cells.ok is False
    assert cells.detail == "2 of 3 cells pulsed, 1 lost (beta/fra1)"
    young = verify.report(calculated_database, formulas, Hour(H, H + timedelta(minutes=40)))
    [pending] = [c for c in young.checks if (c.section, c.subject) == ("pulses", "cells")]
    assert pending.ok is None  # not settled yet
    stock_ok = [c for c in found.checks if c.section == "pulses" and c.subject == "alpha"]
    assert stock_ok[0].ok is True


def _forget(conn, network):
    """Remove a network's IP observations: pulse runs store none (PULSE-COUNTERS-1)."""
    conn.execute(
        "DELETE FROM ip_observations io USING test_runs t JOIN scope_stats s USING(scope_id) "
        "JOIN products p USING(product_id) WHERE io.run_key=t.run_key AND p.provider=%s",
        (network,),
    )


def _observed(found):
    return [(c.subject, c.ok, c.detail) for c in found.checks if c.section == "observations"]


@pytest.mark.parametrize(
    "schedules, networks",
    [
        ({"alpha": SCHEDULES["alpha"], "beta": {"DE": 2}}, None),  # beta: no daily cell at 13
        ({"alpha": SCHEDULES["alpha"]}, None),  # beta paused: no formula
        (SCHEDULES, ("alpha",)),  # beta paused: no fleet entry
    ],
)
def test_observations_are_due_only_from_networks_with_a_daily_cell(
    calculated_database, tmp_path, schedules, networks
):
    """VERIFY-OBS-DUE-1: a network that only pulsed in the hour (16 Nov 01:00: byteful and
    iproyal, 0 countries due) is not listed, and the total counts only alpha, whose daily cell
    was due; beta's fixture pulses stored observations, then none, as pulses do now."""
    stock(calculated_database, beta_daily=False)
    folder = _formulas(tmp_path / "due", schedules)
    for forgotten in (False, True):
        if forgotten:
            with connect(calculated_database) as conn:
                _forget(conn, "beta")
        found = verify.report(calculated_database, folder, Hour(H, NOW), networks=networks)
        assert _observed(found) == [
            ("alpha", True, "15 observations, 1 IPs"),
            ("total", True, "15 in the hour, 1 networks due"),
        ]
        assert found.ok


def test_a_network_with_a_daily_cell_and_no_observations_still_fails(calculated_database, formulas):
    stock(calculated_database)
    with connect(calculated_database) as conn:
        _forget(conn, "alpha")
    found = verify.report(calculated_database, formulas, Hour(H, NOW))
    assert _observed(found) == [
        ("alpha", False, "0 observations, 0 IPs"),
        ("beta", True, "9 observations, 1 IPs"),
        ("total", False, "9 in the hour, 2 networks due"),
    ]
    young = verify.report(calculated_database, formulas, Hour(H, H + timedelta(minutes=40)))
    assert _observed(young)[0] == ("alpha", None, "0 observations, 0 IPs")  # not settled yet
