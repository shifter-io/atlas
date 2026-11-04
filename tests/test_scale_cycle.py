"""SCALE-GEN-1b: the hourly cycle driver on the SCALE-GEN-1a miniature."""

import asyncio
import importlib
import json
import os
import sys
import threading
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
import pytest

from proxy_index.providers.credentials import read_credentials
from proxy_index.storage import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scale"))
generate = importlib.import_module("generate")
synthetic = importlib.import_module("synthetic")
cycle = importlib.import_module("cycle")
measure = importlib.import_module("measure")

pytest_plugins = ["test_storage"]

END = "2026-10-03"  # generated day 1 is 2026-10-01; the cycle drives day 2, 2026-10-02
MINIATURE = {"networks": "iproyal,decodo", "countries": "AQ,DE,US", "total_days": 2, "seed": 7}
MINIATURE |= {"end_date": END, "scale": 0.004, "jobs": 1, "formulas": str(generate.FORMULAS)}
DAILY = (
    "SELECT a.ip::text,o.bucket_start,o.count,o.first_seen,o.last_seen FROM ip_observations o "
    "JOIN ip_addresses a USING(ip_id) JOIN test_runs r USING(run_key) "
    "WHERE r.summary->>'test_kind'='daily' AND r.started_at>=%s AND r.started_at<%s ORDER BY 1,2"
)
ENRICHED = (
    "SELECT a.ip::text,e.location_id,e.network_id,e.matched FROM ip_enrichment e "
    "JOIN ip_addresses a USING(ip_id) JOIN dbip_releases d USING(release_id) WHERE d.sha256=%s "
    "AND e.ip_id IN (SELECT o.ip_id FROM ip_observations o JOIN test_runs r USING(run_key) "
    "WHERE r.summary->>'test_kind'='daily' AND r.started_at>=%s AND r.started_at<%s) ORDER BY 1"
)
ESTIMATED = (
    "SELECT h.product_id,h.country_code,h.day,h.hour,h.measured_ips,h.measured_requests,"
    "h.unique_ips,h.requests FROM estimated_hours h JOIN test_runs r USING(run_key) "
    "WHERE r.started_at>=%s AND r.started_at<%s ORDER BY 1,2,3,4"
)
PULSES = (
    "SELECT s.product_id,trim(b.country_code),g.location_id,g.network_id,g.completed,"
    "g.successes,g.timeouts,g.bytes_up,g.bytes_down,g.histogram_steps,g.histogram_counts,"
    "u.completed,u.successes,u.timeouts,u.histogram_steps,u.histogram_counts "
    "FROM run_geo_buckets g JOIN test_runs r USING(run_key) JOIN run_buckets u USING(run_key) "
    "JOIN scope_stats s USING(scope_id) JOIN test_batches b USING(batch_id) "
    "WHERE b.kind='pulse' AND b.scheduled_for=%s ORDER BY 1,2,3,4,5,6"
)
# The calculator adds its breakdown labels to place_names and asn_names; these lists only ingest.
LISTS = ("pool_stats", "scope_stats", "enrichment_locations", "enrichment_networks")


def name(config):
    return read_credentials(config, "postgres")["dbname"]


def dataset(config, days="1", **changes):
    values = MINIATURE | {"database": config, "days": generate.parse_days(days)}
    return generate.generate(Namespace(**(values | changes)), name(config))


def model(config):
    with connect(config) as conn:
        settings, _ = generate.stored(conn)
    return synthetic.Model(settings, *generate.load(str(generate.FORMULAS)))


def first_test_hour(config):
    return min(lane.hour for lane in model(config).lanes)


def options(config, tmp_path, **changes):
    values = {"database": config, "hours": 1, "out": str(tmp_path / "report"), "max_minutes": 25.0}
    values |= {"first_hour": 0, "pulse_rate": 0.0, "pulse_concurrency": 4, "node_api": None}
    values |= {"siteapi": None, "siteapi_pid": None, "siteapi_timeout": 30.0}
    values |= {"methodology": str(cycle.METHODOLOGY), "formulas": str(generate.FORMULAS)}
    return Namespace(**(values | changes))


def drive(config, tmp_path, **changes):
    return cycle.run(options(config, tmp_path, **changes), name(config))


def rows(config, query, *params):
    with connect(config) as conn:
        return conn.execute(query, params).fetchall()


class Siteapi:
    """A stand-in for siteapi's /healthz: 503 while starting, then the database's current build."""

    def __init__(self, config):
        site = self
        self.config, self.calls = config, 0

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                site.calls += 1
                body, status = {"status": "starting"}, 503
                if site.calls > 1:
                    (build,) = rows(site.config, "SELECT build_id FROM current_calculation")[0]
                    body = {"status": "ok", "fingerprint": json.dumps({"build_id": build})}
                    body, status = body | {"buildMs": 7, "loadMs": 3, "unserved": ["/v1/x"]}, 200
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def test_two_hours_go_through_the_real_paths_and_store_what_the_generator_does(databases, tmp_path):
    config, reference = databases(), databases()
    dataset(config, "1")
    dataset(reference, "1..2")  # the generator's own day 2, written directly
    first = first_test_hour(config)
    lanes = [lane for lane in model(config).lanes if lane.hour == first]
    with Siteapi(config) as site:
        result = drive(
            config, tmp_path, hours=2, first_hour=first, siteapi=site.url, siteapi_pid=os.getpid()
        )
    assert result["complete"] and result["hours_done"] == 2 and len(result["finished"]) == 2
    report = json.loads(Path(result["report"]).read_text())
    assert report["first_day"] == 2 and report["current"] is None
    one, two = (record["steps"] for record in report["hours"])
    assert one["ingest"]["tests"] == len(lanes) > 0
    assert one["ingest"]["statements"] and one["ingest"]["per_test"]["count"] == len(lanes)
    for steps in (one, two):
        assert steps["pulses"]["planned"] == steps["pulses"]["uploads"] == 6
        assert steps["pulses"]["failed"] == 0 and steps["pulses"]["latency"]["p95"] > 0
        assert steps["pulses"]["statuses"] == {"done": 6}
        assert steps["calculate"]["exit"] == 0 and steps["calculate"]["peak_rss_kb"] > 0
        assert steps["calculate"]["statements"]  # traced in the child process
        assert steps["siteapi"]["reached"] and steps["siteapi"]["status"] == "ok"
        assert steps["siteapi"]["build_id"] == steps["calculate"]["build_id"]
        assert steps["siteapi"]["buildMs"] == 7 and steps["siteapi"]["unserved"] == 1
        assert steps["siteapi"]["peak_rss_kb"] > 0
    assert one["release"]["released"] > 0
    assert two["calculate"]["build_id"] > one["calculate"]["build_id"]
    assert report["hours"][1]["sizes"]["tables"]["ip_observations"] > 0
    markdown = (tmp_path / "report" / "scale-report.md").read_text()
    assert markdown.count("| 2026-10-02T") == 2 and "## Largest tables" in markdown

    # The real ingest, enrichment, estimate and pulse paths stored the generator's rows.
    start = datetime(2026, 10, 2, first, tzinfo=UTC)
    window = (start, start + timedelta(hours=2))
    assert rows(config, DAILY, *window) == rows(reference, DAILY, *window)
    assert len(rows(config, DAILY, *window)) == sum(
        model(config).test(lane, 2).rows for lane in model(config).lanes if lane.hour - first < 2
    )
    release = generate.Writer(None, model(config), {}).release(2)
    assert rows(config, ENRICHED, release, *window) == rows(reference, ENRICHED, release, *window)
    assert rows(config, ESTIMATED, *window) == rows(reference, ESTIMATED, *window)
    for slot in (start, start + timedelta(hours=1)):
        assert rows(config, PULSES, slot) == rows(reference, PULSES, slot) != []
    for table in LISTS:  # no new pool, scope, location or network: ingest found the generator's
        count = f"SELECT count(*) FROM {table}"
        assert rows(config, count) == rows(reference, count)
    published = rows(config, "SELECT build_id FROM current_calculation")
    assert published == [(two["calculate"]["build_id"],)]


def test_an_interrupted_hour_continues_where_it_stopped(databases, tmp_path, monkeypatch):
    config = databases()
    dataset(config, "1")
    first = first_test_hour(config)
    real_daily, real_upload = cycle.Cycle.daily, cycle.Cycle.upload

    def out_of_time_after_one(self, test):
        found = real_daily(self, test)
        self.deadline = 0  # the budget ran out during the first test
        return found

    monkeypatch.setattr(cycle.Cycle, "daily", out_of_time_after_one)
    stopped = drive(config, tmp_path, first_hour=first)
    assert not stopped["complete"] and stopped["hours_done"] == 0 and stopped["finished"] == []
    report = json.loads(Path(stopped["report"]).read_text())
    assert report["current"]["index"] == 0 and report["hours"] == []
    assert "Hour 0 is in progress." in (tmp_path / "report" / "scale-report.md").read_text()

    # Two uploads are lost: a rejected answer and a client timeout (a slow ingest at full size).
    lost = [cycle.Failed("connection lost"), TimeoutError("upload timed out")]

    async def lost_twice(self, *args):
        if lost:
            raise lost.pop(0)
        return await real_upload(self, *args)

    monkeypatch.setattr(cycle.Cycle, "daily", real_daily)
    monkeypatch.setattr(cycle.Cycle, "upload", lost_twice)
    stopped = drive(config, tmp_path, first_hour=first, pulse_rate=50.0)
    assert not stopped["complete"]  # two pulses are left to upload
    assert not drive(config, tmp_path, max_minutes=0)["complete"]  # no time: nothing runs
    with pytest.raises(cycle.Failed, match="calculate exited with 2"):
        drive(config, tmp_path, methodology=str(tmp_path / "missing.toml"))
    done = drive(config, tmp_path)
    assert done["complete"] and done["hours_done"] == 1
    (record,) = json.loads(Path(done["report"]).read_text())["hours"]
    steps = record["steps"]
    tests = sum(lane.hour == first for lane in model(config).lanes)
    assert steps["ingest"]["parts"] == (2 if tests > 1 else 1) and steps["ingest"]["tests"] == tests
    assert steps["pulses"]["parts"] == 2 and steps["pulses"]["failed"] == 2  # attempts
    assert steps["pulses"]["uploads"] == steps["pulses"]["planned"] == 6
    assert steps["pulses"]["statuses"] == {"done": 6}  # every run ended done on the server
    first_failure, second_failure = steps["pulses"]["failures"]
    assert first_failure.startswith("batch ") and first_failure.endswith(": connection lost")
    assert second_failure.endswith(": upload timed out")
    assert steps["siteapi"]["skipped"] == "no --siteapi" and steps["calculate"]["parts"] == 1
    daily = rows(
        config,
        "SELECT count(*) FROM test_runs WHERE summary ? 'scale_cycle' "
        "AND summary->>'test_kind'='daily'",
    )
    assert daily == [(tests,)]
    assert drive(config, tmp_path)["finished"] == []  # already done: nothing more to run


def test_guards_refuse_other_databases_runs_hours_and_data_sets(databases, tmp_path, capsys):
    fresh = databases()
    with pytest.raises(cycle.Refused, match="uses only 'proxy_scale'"):
        cycle.run(options(fresh, tmp_path))
    assert cycle.main(["--database", str(fresh), "--hours", "1"]) == 2
    assert "refused: the database is" in capsys.readouterr().err
    with pytest.raises(cycle.Refused, match="no synthetic data set"):
        drive(fresh, tmp_path)

    whole = databases()
    dataset(whole, "1..2")
    with pytest.raises(cycle.Refused, match="every day of the data set is generated"):
        drive(whole, tmp_path)
    with connect(whole) as conn:
        conn.execute(
            'UPDATE settings SET value=value||\'{"days_done":1,"prepared":2}\' '
            "WHERE key='scale.generator'"
        )
    with pytest.raises(cycle.Refused, match="day 2 is half generated"):
        drive(whole, tmp_path)
    with connect(whole) as conn:
        conn.execute("UPDATE test_runs SET summary=summary-'scale_generator' WHERE run_key=1")
    with pytest.raises(cycle.Refused, match="1 test runs neither generator wrote"):
        drive(whole, tmp_path)

    config = databases()
    dataset(config, "1")
    with pytest.raises(cycle.Refused, match="--first-hour is 0..23"):
        drive(config, tmp_path, first_hour=24)
    with connect(config) as conn:
        conn.autocommit = True
        found = cycle.Cycle(conn, options(config, tmp_path, first_hour=23), name(config))
        assert found.bounds(0)[0] == 2
        with pytest.raises(cycle.Refused, match="ends before day 3"):
            found.bounds(1)
    assert rows(config, "SELECT country_code FROM countries WHERE enabled ORDER BY 1") == [
        ("AQ",),
        ("DE",),
        ("US",),
    ]

    later = databases()
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    dataset(later, "1", end_date=tomorrow)  # its day 2 is today, not over yet
    with pytest.raises(cycle.Refused, match="has not ended yet"):
        drive(later, tmp_path, first_hour=23)


def test_siteapi_waits_reads_health_and_the_node_api_can_be_external(monkeypatch):
    found = object.__new__(cycle.Cycle)
    found.state = {"current": {"steps": {"calculate": {"parts": [{"build_id": 99}]}}}}
    found.args = Namespace(siteapi="http://127.0.0.1:9/", siteapi_timeout=1.5, siteapi_pid=None)
    part, done = found.siteapi(2, None, {})
    assert done and part == {"build_id": 99, "reached": False, "status": None} | {
        key: None for key in ("buildMs", "loadMs", "assembleMs", "renderMs")
    } | {"unserved": 0}
    assert cycle.healthz("http://127.0.0.1:9") == {}

    async def base():
        async with found.node_api(None) as url:
            return url

    found.args = Namespace(node_api="http://node.invalid:8443/")
    assert asyncio.run(base()) == "http://node.invalid:8443"

    # With pg_stat_statements, a step's statements are the change of its counters.
    snapshots = iter([{}, {"q": ("SELECT 1", 2, 3000.0)}])
    monkeypatch.setattr(measure, "snapshot", lambda conn: next(snapshots))
    found.pg_stat, found.conn, found.idle = True, None, lambda day, start, step: ({}, True)
    part, done = found.measured("idle", 2, None, {})
    assert done and part["statements"] == [{"statement": "SELECT 1", "calls": 2, "seconds": 3.0}]


def test_main_reports_a_failed_step_and_prints_the_result(monkeypatch, capsys):
    def failing(args):
        raise cycle.Failed("calculate exited with 2")

    monkeypatch.setattr(cycle, "run", failing)
    assert cycle.main(["--database", "x.toml", "--hours", "1"]) == 1
    assert "failed: calculate exited with 2" in capsys.readouterr().err
    monkeypatch.setattr(cycle, "run", lambda args: {"hours_done": args.hours})
    assert cycle.main(["--database", "x.toml", "--hours", "3"]) == 0
    assert json.loads(capsys.readouterr().out) == {"hours_done": 3}


def test_statement_timings_from_snapshots_and_the_tracer(database, tmp_path, monkeypatch):
    before = {"a": ("SELECT 1", 2, 10.0), "b": ("SELECT 2", 1, 5.0)}
    after = before | {"a": ("SELECT 1", 5, 4010.0), "c": ("SELECT 3", 1, 2.0)}
    assert measure.delta(before, after) == [
        {"statement": "SELECT 1", "calls": 3, "seconds": 4.0},
        {"statement": "SELECT 3", "calls": 1, "seconds": 0.002},
    ]

    class Fake:
        def execute(self, query, params=None):
            return self

        def fetchall(self):
            return [("7", "SELECT 7", 1, 3.0)]

    assert measure.snapshot(Fake()) == {"7": ("SELECT 7", 1, 3.0)}
    merged = measure.merge(
        [
            [{"statement": "x", "calls": 1, "seconds": 1.0, "max_seconds": 1.0}],
            [{"statement": "x", "calls": 2, "seconds": 0.5, "max_seconds": 0.4}],
            [{"statement": "y", "calls": 1, "seconds": 2.0}],
        ]
    )
    assert merged == [
        {"statement": "y", "calls": 1, "seconds": 2.0},
        {"statement": "x", "calls": 3, "seconds": 1.5, "max_seconds": 1.0},
    ]

    async def asynchronous():
        async with await psycopg.AsyncConnection.connect(
            **read_credentials(database, "postgres")
        ) as conn:
            await conn.execute("SELECT  2")

    real = psycopg.Cursor.execute
    with measure.Tracer() as tracer, connect(database) as conn:
        conn.autocommit = True
        assert isinstance(measure.has_statements(conn), bool)
        conn.execute("SELECT 1\n")
        conn.cursor().executemany("SELECT %s", [(1,), (2,)])
        asyncio.run(asynchronous())
        with pytest.raises(psycopg.Error):
            conn.execute("SELECT nothing")
    assert psycopg.Cursor.execute is real
    found = {row["statement"]: row["calls"] for row in tracer.report()}
    assert found["SELECT 1"] == 1 and found["SELECT 2"] == 1 and found["SELECT %s"] == 1
    assert found["SELECT nothing"] == 1

    module = tmp_path / "scale_child.py"
    module.write_text("import sys\nif sys.argv[1:] == ['fail']:\n    raise SystemExit('no')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "argv", list(sys.argv))  # trace() sets the child's argv
    out = tmp_path / "statements.json"
    assert measure.trace([str(out), "scale_child"]) == 0 and json.loads(out.read_text()) == []
    assert measure.trace([str(out), "scale_child", "fail"]) == 1


def test_peak_rss_percentiles_and_the_report(tmp_path):
    big = "b = bytes(64 << 20)\nb = bytearray(b) + b'x'\n"
    child = measure.run_child([sys.executable, "-c", big], poll=0.05)
    assert child["exit"] == 0 and child["peak_rss_kb"] > 64 << 10
    assert measure.run_child([sys.executable, "-c", "raise SystemExit(3)"])["exit"] == 3
    assert measure.vm_hwm(os.getpid()) > 0 and measure.vm_hwm(2**22 + 7) is None
    assert measure.percentiles([]) == {"count": 0, "p50": None, "p95": None, "max": None}
    values = [i / 10 for i in range(1, 21)]
    assert measure.percentiles(values) == {"count": 20, "p50": 1.0, "p95": 1.9, "max": 2.0}
