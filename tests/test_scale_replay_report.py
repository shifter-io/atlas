"""REPLAY-REPORT-1: the tracer's statement shapes on the test database, and the replay's report
from a fixture state (synthetic documentation addresses only)."""

import asyncio
import gzip
import importlib
import json
import re
import runpy
import stat
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from atlas.providers.credentials import read_credentials
from atlas.storage import connect

SCALE = Path(__file__).resolve().parents[1] / "scripts" / "scale"
sys.path.insert(0, str(SCALE))
measure = importlib.import_module("measure")
replay_report = importlib.import_module("replay_report")
statement_source = importlib.import_module("statement_source")

pytest_plugins = ["test_storage"]

GB = 2**30
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
IPV6 = re.compile(r"\b(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}\b|[0-9a-f]{0,4}::[0-9a-f]{0,4}", re.I)
PATHS = re.compile(r"(?<![\w.])(?:~|\.{1,2})?/(?:[\w.-]+/)+[\w.-]*|\b[A-Z]:\\")


def test_a_statement_shape_keeps_no_value():
    text = "SELECT  'it''s', 1.5e3, -7, x1, $2\n FROM t_2026 WHERE ip='192.0.2.1'"
    assert measure.shape(text) == "SELECT ?, ?, -?, x1, $2 FROM t_2026 WHERE ip=?"
    assert measure.shape("SELECT '" + "198.51.100.7 " * 500) == "SELECT ?"  # cut off, still ?
    assert len(measure.shape("SELECT " + "x, " * 300)) == measure.SHAPE


def test_the_tracer_times_statements_and_copies_by_shape(database):
    async def copied():
        credentials = read_credentials(database, "postgres")
        async with await psycopg.AsyncConnection.connect(**credentials) as conn:
            await conn.execute("CREATE TEMP TABLE later(n int)")
            async with conn.cursor().copy("COPY later FROM STDIN") as copy:
                await copy.write_row((1,))
                await copy.write_row((2,))

    real = psycopg.Cursor.copy
    with measure.Tracer() as tracer, connect(database) as conn:
        conn.autocommit = True
        conn.execute("SELECT pg_sleep(0.3)")
        conn.execute("SELECT pg_sleep(%s)", (0.01,))
        conn.execute("CREATE TEMP TABLE seen(ip text, n int)")
        conn.execute("INSERT INTO seen VALUES ('192.0.2.1', 1), ('192.0.2.2', 2)")
        with conn.cursor().copy("COPY seen FROM STDIN") as copy:
            for i in range(3):
                copy.write_row((f"198.51.100.{i}", i))
        where = sql.SQL("SELECT {} FROM seen WHERE ip={}")
        conn.execute(where.format(sql.Identifier("n"), sql.Literal("192.0.2.1")))
        conn.execute(b"SELECT 42")
        conn.execute("CREATE TEMP TABLE kept(k int PRIMARY KEY, n int)")
        upsert = "INSERT INTO kept VALUES (1, 1) /* " + "x" * 250 + " */ ON CONFLICT (k) DO "
        for _ in range(2):
            conn.execute(upsert + "UPDATE SET n = kept.n + 1")
        with pytest.raises(psycopg.Error), conn.cursor().copy("COPY nowhere FROM STDIN"):
            pass
        asyncio.run(copied())
    assert psycopg.Cursor.copy is real
    summary = tracer.summary()
    shapes = {row["statement"]: row for row in summary["shapes"]}
    sleep = shapes["SELECT pg_sleep(?)"]
    assert sleep["calls"] == 1 and sleep["rows"] == 1 and sleep["max_seconds"] >= 0.3
    assert shapes["SELECT pg_sleep(%s)"]["calls"] == 1
    assert shapes["INSERT INTO seen VALUES (?, ?), (?, ?)"]["rows"] == 2
    assert shapes["COPY seen FROM STDIN"]["rows"] == 3 and shapes["COPY later FROM STDIN"]["rows"]
    kept = shapes[measure.shape(upsert)]  # DO UPDATE is past the shape's 200 characters
    assert kept["upsert"] and kept["calls"] == 2 and kept["rows"] == 2
    assert not shapes["INSERT INTO seen VALUES (?, ?), (?, ?)"]["upsert"]
    failed = shapes["COPY nowhere FROM STDIN"]
    assert failed["calls"] == 1 and failed["rows"] == 0
    assert 'SELECT "n" FROM seen WHERE ip=?' in shapes and "SELECT ?" in shapes
    assert sum(sleep["histogram"].values()) == 1
    assert summary["count"] == sum(row["calls"] for row in summary["shapes"])
    assert summary["seconds"] >= 0.3 and len(summary["slowest"]) == measure.SLOWEST
    assert summary["slowest"][0]["statement"] == "SELECT pg_sleep(?)"
    assert summary["slowest"][0]["rows"] == 1 and summary["slowest"][0]["seconds"] >= 0.3
    assert not IPV4.search(json.dumps(summary))
    assert measure.top(summary)[0]["statement"] == "SELECT pg_sleep(?)"


def shape(text, times, rows):
    histogram = {}
    for seconds in times:
        histogram[measure.bucket(seconds)] = histogram.get(measure.bucket(seconds), 0) + 1
    found = {"statement": text, "calls": len(times), "seconds": sum(times), "rows": rows}
    return found | {"max_seconds": max(times), "histogram": histogram}


def test_shapes_of_several_hours_add_up_with_their_p95():
    first = {"shapes": [shape("SELECT ?", [0.01] * 50, 50), shape("COPY x FROM STDIN", [2.0], 9)]}
    second = {"shapes": [shape("SELECT ?", [0.01] * 45 + [1.0] * 5, 50)]}
    found = replay_report.combine([first, second])
    assert [row["statement"] for row in found] == ["SELECT ?", "COPY x FROM STDIN"]
    select = found[0]
    assert select["calls"] == 100 and select["rows"] == 100 and select["max_seconds"] == 1.0
    assert select["p95_seconds"] == pytest.approx(0.01, rel=0.05)
    assert found[1]["p95_seconds"] == pytest.approx(2.0, rel=0.05)
    assert found[1]["p95_seconds"] <= found[1]["max_seconds"]  # never above the maximum
    assert replay_report.quantile({}, 95) is None


START = "2026-11-0{}T{:02d}:00:00+00:00"
COUNTS = {"public.ip_observations": 10, "public.test_runs": 2, "public.job_runs": 3}
COUNTS |= {"public.product_ips": 5, "public.pool_ips": 5}


def ingest(rows, slow):
    shapes = [
        shape("INSERT INTO ip_observations SELECT * FROM incoming_ips", [slow], rows),
        shape('INSERT INTO "public"."test_runs" (run_id) VALUES ($1)', [0.01, 0.01], 2),
        shape("COPY incoming_ips FROM STDIN", [0.2], rows),  # staging: not a table of its own
        shape("SELECT count(*) FROM test_runs", [0.05], 1),
        # Its rowcount includes updated rows: not counted per hour.
        shape("INSERT INTO product_ips SELECT ? FROM incoming_ips", [0.1], 999) | {"upsert": True},
        # pool_ips' insert inside WITH: its rowcount is the count(*) row.
        shape("WITH added AS (INSERT INTO pool_ips SELECT ?) SELECT count(*) FROM added", [0.1], 1),
    ]
    slowest = [{"statement": shapes[0]["statement"], "seconds": slow, "rows": rows}]
    return {"count": 5, "seconds": slow + 0.27, "slowest": slowest, "shapes": shapes}


def hour(index, rows, calculate, size, done=True):
    day, at = divmod(index, 2)
    record = {"index": index, "day": day, "start": START.format(day + 1, at), "retries": 0}
    daily = {"parts": [{"tests": 2}], "test_seconds": [0.5, 1.5]}
    record["ingest"] = {"done": True, "steps": {"daily": daily}, "statements": ingest(rows, 0.4)}
    record["ingest"]["started"] = START.format(day + 1, at)
    record["ingest"]["finished"] = record["ingest"]["started"].replace(":00:00+", ":01:30+")
    calc = {"count": 1, "seconds": calculate, "shapes": [shape("SELECT build()", [calculate], 1)]}
    calc["slowest"] = [{"statement": "SELECT build()", "seconds": calculate, "rows": 1}]
    attempts = [{"exit": 3, "seconds": 1.0}, {"exit": 0, "seconds": calculate, "peak_rss_kb": 2048}]
    record["calculate"] = {"done": done, "attempts": attempts if done else []}
    if done:
        record["calculate"]["statements"] = calc
        record["sizes"] = {"database_bytes": size, "all_databases_bytes": size + GB}
    return record


def fixture_state(tmp_path):
    slices = tmp_path / "slices"
    slices.mkdir()
    for at in range(2):
        (slices / f"slice-{at:02d}.json").write_text(json.dumps({"tests": [{"ip": "192.0.2.9"}]}))
        (slices / f"slice-{at:02d}.jsonl.gz").write_bytes(gzip.compress(b"2001:db8::1\n" * 100))
    hours = [hour(0, 100, 30.0, 5 * GB), hour(1, 400, 60.0, 5 * GB + 2**20)]
    hours += [hour(2, 50, 20.0, 6 * GB), hour(3, 70, 0, None, done=False)]
    tables = {"ip_observations": 3 * GB, "test_runs": GB // 2}
    other = {"public.job_runs": 5, "public.test_runs": 0}
    state = {"replay_id": "ab" * 16, "database": "proxy_reviews", "first_day": "2026-11-01"}
    state |= {"days": 2, "hours_per_day": 2, "slices": str(slices), "hours": hours}
    state |= {"baseline": {"counts": COUNTS, "sizes": {"database_bytes": 4 * GB}}}
    replay = {"public.test_runs": 4, "public.pool_ips": 12, "public.product_ips": 30}
    state["days_counted"] = {"0": {"other": other, "table_bytes": tables, "replay": replay}}
    out = tmp_path / "out"
    out.mkdir()
    (out / "state.json").write_text(json.dumps(state))
    event = {"time": "x", "hour": hours[1]["start"], "step": "daily", "pause": 0.5}
    event["reason"] = f"OSError: 203.0.113.9 and 2001:db8::7 in {slices}/slice-01.json"
    (out / "events.jsonl").write_text(json.dumps(event) + "\n")
    return out, slices


def test_the_report_per_hour_and_day_with_the_extremes_and_the_last_day(tmp_path):
    out, slices = fixture_state(tmp_path)
    path = replay_report.write(out)
    report = json.loads((out / "report.json").read_text())
    first, second, third, fourth = report["hours"]
    assert first["rows"] == {"public.ip_observations": 100, "public.test_runs": 2}
    assert first["rows_total"] == 102 and first["tests"] == 2 and first["hour"] == 0
    sizes = sum(p.stat().st_size for p in slices.iterdir() if p.name.startswith("slice-00"))
    assert first["slice_bytes"] == sizes and first["growth_bytes"] == GB
    assert second["growth_bytes"] == 2**20 and third["growth_bytes"] == GB - 2**20
    assert first["ingest_seconds"] == 90.0 and second["ingest_seconds"] == 89.5  # no pause
    assert first["per_test"] == {"count": 2, "p50": 0.5, "p95": 1.5, "max": 1.5}
    assert first["calculate_seconds"] == 30.0 and first["peak_rss_kb"] == 2048
    assert first["slowest"]["statement"] == "SELECT build()" and first["slowest"]["seconds"] == 30
    assert (second["retries"], second["delay_seconds"]) == (1, 0.5) and first["retries"] == 0
    assert not fourth["calculated"] and fourth["calculate_seconds"] is None
    assert fourth["growth_bytes"] is None

    day1, day2 = report["days_done"]
    assert day1["rows_total"] == 102 + 402 and day1["tests"] == 4
    assert day1["calculate"] == {"count": 2, "p50": 30.0, "p95": 60.0, "max": 60.0}
    assert day1["database_gb"] == 5.001 and day1["server_gb"] == 6.001
    assert day1["rows"]["public.ip_observations"] == 500 and day2["hours"] == 2
    assert day2["database_gb"] == 6.0  # the last hour with its sizes
    assert day1["replay_rows"] == {"public.pool_ips": 12, "public.product_ips": 30} | {
        "public.test_runs": 4
    }
    assert day2["replay_rows"] is None  # not counted yet
    assert (report["hardest"]["day"], report["hardest"]["hour"]) == (1, 1)
    assert (report["lightest"]["day"], report["lightest"]["hour"]) == (2, 0)
    assert report["final"] == {"day": 1, "of": 2, "database_gb": 5.001, "server_gb": 6.001} | {
        "tables_gb": {"ip_observations": 3.0, "test_runs": 0.5},
        "production_growth": {"public.job_runs": 5},
    }
    statements = {row["statement"]: row for row in report["statements"]}
    assert statements["SELECT build()"]["calls"] == 3 and statements["SELECT build()"]["rows"] == 3
    assert statements["COPY incoming_ips FROM STDIN"]["rows"] == 620
    # Without pg_stat_statements: the client side only, with a note.
    assert report["server_statements"] is None and first["server"] is None
    assert first["client"] == {"calls": 5 + 1, "seconds": 30.67}
    assert fourth["client"] == {"calls": 5, "seconds": 0.67}  # its calculation is not done

    text = path.read_text()
    assert text.count("pg_stat_statements is absent: statements are timed by the tool") == 2
    assert "## Statement time per hour" in text and "Server ms" not in text
    assert "| 1 | 0 | 6 | 30.67 |" in text
    assert "slowest statement shapes of the run: server" not in text
    for heading in ("## Per day", "## Per hour", "## Day 1 of 2: sizes", "## Modelled, not real"):
        assert heading in text
    assert "Hardest: day 1 01h, 402 rows inserted" in text
    assert "Lightest: day 2 00h, 52 rows inserted" in text
    assert "| 1 | 12 | 30 | 4 |" in text  # the exact replay rows of day 1, by table
    assert "| 1 | 2 | 4 | 46 | 504 |" in text  # day 1: hours, tests, replay rows, rows inserted
    assert "| public.job_runs | 5 |" in text and "| ip_observations | 3.0 |" in text
    for label in ("Byteful rows", "HTTP pulses", "Browser data", "Bucket counters and latency"):
        assert label in text
    assert "lower bound" in text and "pool_ips" in report["lower_bound"]["tables"]
    for name in ("report.md", "report.json"):
        found = (out / name).read_text()
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o600
        assert not IPV4.search(found) and not IPV6.search(found), name
        assert not PATHS.search(found) and str(tmp_path) not in found, name


def test_the_clean_output_patterns_catch_what_they_guard():
    assert IPV4.search("from 192.0.2.1") and IPV6.search("2001:db8::1")
    assert IPV6.search("fe80:0:0:0:200:f8ff:fe21:67cf") and not IPV6.search("T00:01:30+00:00")
    assert PATHS.search("in /srv/replay/x") and PATHS.search("../replay/x")
    assert PATHS.search("C:\\x")
    assert not PATHS.search("p50/p95/max s") and not PATHS.search("done/planned")


def test_a_report_before_any_day_ends_and_from_the_command_line(tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    out.mkdir()
    state = {"replay_id": "cd" * 16, "database": "proxy_reviews", "first_day": "2026-11-01"}
    state |= {"days": 7, "slices": str(tmp_path / "gone"), "hours": [], "days_counted": {}}
    (out / "state.json").write_text(json.dumps(state | {"baseline": {"counts": {}}}))
    monkeypatch.setattr(sys, "argv", ["replay_report.py", str(out)])
    runpy.run_path(str(SCALE / "replay_report.py"), run_name="__main__")
    assert capsys.readouterr().out.strip().endswith("report.md")
    report = json.loads((out / "report.json").read_text())
    assert report["hours"] == [] and report["final"] is None and report["hardest"] is None
    text = (out / "report.md").read_text()
    assert "No simulated day has ended yet." in text and "no hour calculated yet" in text
    monkeypatch.setattr(sys, "argv", ["replay_report.py"])
    with pytest.raises(SystemExit, match="usage"):
        runpy.run_path(str(SCALE / "replay_report.py"), run_name="__main__")


BUILD = "SELECT build_id FROM current_calculation WHERE x = $1"
COPY = "COPY t FROM STDIN WITH (FORMAT 'csv', NULL '198.51.100.4')"
SERVER_BEFORE = {"5": (BUILD, 1, 1.0, 1, 2.0)}


def counters(n):
    """The counters after `n` windows: each window runs BUILD twice, COPY and another role once."""
    found = {"5": (BUILD, 1 + 2 * n, 1.0 + 8 * n, 1 + 2 * n, 6.5)}
    found["other:16384"] = ("other role", n, 3.0 * n, n, 4.0)
    return found | {"9": (COPY, n, 50.0 * n, 0, 70.0)}


def test_the_report_with_pg_stat_statements_as_its_source(tmp_path):
    out, _ = fixture_state(tmp_path)
    state = json.loads((out / "state.json").read_text())
    previous = SERVER_BEFORE
    for n, record in enumerate(state["hours"][:3], start=1):
        covers = {"calculate": [record["index"]], "ingest": [record["index"] + 1]}
        record["server"] = [statement_source.window(previous, counters(n), covers)]
        previous = counters(n)
    (out / "state.json").write_text(json.dumps(state))
    path = replay_report.write(out)
    report = json.loads((out / "report.json").read_text())
    first, *_, fourth = report["hours"]
    assert first["server"] == {"calls": 4, "ms": 61.0, "rows": 3, "mean_ms": 15.25}
    assert fourth["server"] is None and first["client"]["calls"] == 6
    shapes = {row["statement"]: row for row in report["server_statements"]}
    shaped = "COPY t FROM STDIN WITH (FORMAT ?, NULL ?)"  # no value kept
    copy = shapes[shaped]
    assert (copy["calls"], copy["ms"], copy["mean_ms"]) == (3, 150.0, 50.0)
    assert copy["max_ms_since_reset"] == 70.0
    assert shapes["other role"]["calls"] == 3  # another role's statements count in the totals
    assert report["server_statements"][0]["statement"] == shaped

    text = path.read_text()
    assert "Statement numbers come from the server first (pg_stat_statements" in text
    assert "pg_stat_statements is absent" not in text
    assert "| Day | Hour | Server calls | Server ms | Server mean ms | Server rows |" in text
    assert "| 1 | 0 | 4 | 61.0 | 15.25 | 3 | 6 | 30.67 |" in text
    assert "| 2 | 1 |  |  |  |  | 5 | 0.67 |" in text  # no window closed on that hour yet
    assert "slowest statement shapes of the run: server time" in text
    assert f"| 150.0 | 3 | 50.0 | 0 | 70.0 | `{shaped}` |" in text
    assert "Max ms since reset" in text and "`other role`" in text
    assert "slowest statement shapes of the run: client wall time" in text
    for name in ("report.md", "report.json"):
        found = (out / name).read_text()
        assert not IPV4.search(found) and not IPV6.search(found), name
