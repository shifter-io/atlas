"""REPLAY-STATS-1: pg_stat_statements' detection on the test database, whichever state it is in,
and the windows' delta and merge on recorded snapshots (synthetic documentation addresses only)."""

import contextlib
import importlib
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from atlas.storage import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scale"))
measure = importlib.import_module("measure")
statement_source = importlib.import_module("statement_source")

pytest_plugins = ["test_storage"]

SELECT = "SELECT run_id FROM test_runs WHERE run_key = $1"
BEFORE = {
    "11": (SELECT, 10, 100.0, 10, 9.0),
    "12": ("COPY incoming_ips FROM STDIN", 1, 50.0, 5, 50.0),
    "other:16384": ("other role", 3, 30.0, 3, 20.0),
}
AFTER = BEFORE | {
    "11": (SELECT, 14, 180.5, 18, 40.0),
    "other:16384": ("other role", 5, 60.0, 4, 20.0),
    "13": ("SET application_name = 'from 192.0.2.1'  " + "x" * 300, 1, 0.25, 0, 0.25),
}
COVERS = {"calculate": [0], "ingest": [1]}


def test_a_window_is_the_change_of_the_counters_per_queryid():
    found = statement_source.window(BEFORE, AFTER, COVERS)
    assert found["covers"] == COVERS
    assert [row["queryid"] for row in found["shapes"]] == ["11", "other:16384", "13"]
    first, other, setting = found["shapes"]
    assert first == {"queryid": "11", "statement": SELECT, "calls": 4, "ms": 80.5, "rows": 8} | {
        "max_ms_since_reset": 40.0  # cumulative: not this window's maximum
    }
    keys = ("statement", "calls", "ms", "rows")
    assert [other[key] for key in keys] == ["other role", 2, 30.0, 1]
    assert setting["statement"].startswith("SET application_name = ?")  # no value kept
    assert len(setting["statement"]) == measure.SHAPE and "192.0.2.1" not in str(found)
    assert (found["calls"], found["ms"], found["rows"]) == (7, 110.75, 9)
    assert found["mean_ms"] == round(110.75 / 7, 3)
    # An unchanged statement is not in the window; no change at all has no mean.
    assert statement_source.window(AFTER, AFTER, COVERS)["mean_ms"] is None


def test_windows_merge_by_statement_shape():
    first = statement_source.window(BEFORE, AFTER, COVERS)
    later = {key: (v[0], v[1] * 2, v[2] * 2, v[3] * 2, v[4]) for key, v in AFTER.items()}
    later["14"] = (SELECT, 2, 9.5, 2, 90.0)  # the same shape under another queryid
    second = statement_source.window(AFTER, later, {"calculate": [1], "ingest": []})
    merged = statement_source.merge([first, second])
    assert [row["statement"] for row in merged][:2] == [SELECT, "other role"]
    select = merged[0]
    assert (select["calls"], select["rows"]) == (4 + 14 + 2, 8 + 18 + 2)
    assert select["max_ms_since_reset"] == 90.0  # the largest of the windows'
    assert select["ms"] == round(80.5 + 180.5 + 9.5, 3)
    assert select["mean_ms"] == round(select["ms"] / select["calls"], 3)
    assert statement_source.windows({}) == [] and statement_source.merge([]) == []
    assert statement_source.totals([first, second])["calls"] == first["calls"] + second["calls"]


def test_a_snapshot_keys_other_roles_by_role():
    class Fake:
        def execute(self, query, params=None):
            assert "GROUP BY 1" in query and params == (measure.TEXT,)
            return self

        def fetchall(self):
            return [("7", "SELECT 7", 1, 3.0, 1, 3.0), ("other:10", "other role", 2, 4.0, 2, 3.5)]

    assert measure.snapshot(Fake()) == {
        "7": ("SELECT 7", 1, 3.0, 1, 3.0),
        "other:10": ("other role", 2, 4.0, 2, 3.5),
    }


def test_detection_reads_the_view_inside_a_savepoint():
    class Loaded:
        """A connection where the extension exists and its view is readable."""

        def __init__(self):
            self.ran = []

        def execute(self, query, params=None):
            self.ran.append(query)
            return self

        def fetchone(self):
            return (1,)

        @contextlib.contextmanager
        def transaction(self):
            self.ran.append("SAVEPOINT")
            yield

    conn = Loaded()
    assert measure.has_statements(conn) is True
    assert conn.ran == [measure.EXTENSION, "SAVEPOINT", measure.LOADED]
    assert "shared_preload_libraries" not in measure.EXTENSION + measure.LOADED


def installed(conn) -> bool:
    """Install the extension where the server has it (it can be installed but not loaded)."""
    try:
        with conn.transaction():
            conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
    except psycopg.Error:
        return False
    return True


def test_detection_without_pg_read_all_settings_is_true_or_false(database):
    role = sql.Identifier(f"plain_{uuid.uuid4().hex[:12]}")
    with connect(database) as conn:
        conn.autocommit = True
        loaded = "pg_stat_statements" in conn.execute("SHOW shared_preload_libraries").fetchone()[0]
        expected = installed(conn) and loaded
        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(role))
        try:
            conn.execute(sql.SQL("SET ROLE {}").format(role))
            # The setting the earlier detection read: this role may not see it.
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT current_setting('shared_preload_libraries')")
            assert measure.has_statements(conn) is expected
            with conn.transaction():  # in an open transaction: a savepoint, then still usable
                assert measure.has_statements(conn) is expected
                assert conn.execute("SELECT 1").fetchone() == (1,)
        finally:
            conn.execute("RESET ROLE")
            conn.execute(sql.SQL("DROP ROLE {}").format(role))
        assert measure.has_statements(conn) is expected
        if expected:
            found = measure.snapshot(conn)
            assert found and all(len(values) == 5 for values in found.values())
