"""SCORE-HISTORY-BACKFILL-1: the Atlas Score history's source (calculated v21), the trend's
methodology rule (score_history.py) and `calculate-backfill-scores` (score_backfill.py)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from calculated_cases import lenient_policy
from test_calculated_hourly_reads import floor_hour
from test_calculated_incremental import history

from atlas.calculated import rollups, score_backfill, score_history
from atlas.calculated.calculator import calculate
from atlas.calculated.score_backfill import Busy, backfill, point, wanted
from atlas.calculated.score_history import spark
from atlas.cli import main
from atlas.storage import connect

pytest_plugins = ["test_storage", "calculated_template"]

MINUTE, HOUR, DAY = timedelta(minutes=1), timedelta(hours=1), timedelta(days=1)
AS_OF = datetime(2026, 11, 20, 15, 10, tzinfo=UTC)
HERE = Path(score_history.__file__).parent
INSERT = "INSERT INTO calculated_score_history VALUES (%s,%s,%s,%s,%s,%s,%s)"
ROWS = "SELECT period,as_of,score,source FROM calculated_score_history ORDER BY as_of,period,source"


@pytest.fixture
def database(calculated_database):
    return calculated_database


def test_the_missing_points_are_the_trends_slots_without_a_record():
    stored = [
        ("24h", AS_OF - HOUR),  # the hour before as_of
        ("24h", AS_OF - 2 * HOUR - 30 * MINUTE),  # an off-grid build fills its hour
        ("7d", datetime(2026, 11, 19, 23, 10, tzinfo=UTC)),  # the day trends read 30d records
        ("30d", datetime(2026, 11, 18, 5, tzinfo=UTC)),  # any build of a day fills it
    ]
    first = datetime(2026, 11, 17, 20, tzinfo=UTC)  # the first stored run's end
    found = wanted(AS_OF, stored, first, 23, 29)
    hours = [AS_OF - k * HOUR for k in range(23, 2, -1)]
    # 19 Nov 23:10 is one of the 23 hours too; 16 Nov 23:10 is before the first run.
    assert found == [datetime(2026, 11, 17, 23, 10, tzinfo=UTC), *hours]
    assert wanted(AS_OF, stored, first, 0, 1) == [datetime(2026, 11, 19, 23, 10, tzinfo=UTC)]
    assert wanted(AS_OF, [], None, 23, 29) == []


def head(provider, score):
    """A head network with its 24h window's balanced score."""
    window = {"scores": {"balanced": {"score": score}}}
    return {"id": provider, "provider": provider, "kind": "residential", "windows": {"24h": window}}


def test_a_trend_reads_its_methodology_and_prefers_the_published_record(database):
    rows = [
        (AS_OF - HOUR, 80.0, "m1", "build"),
        (AS_OF - HOUR, 70.0, "m1", "backfill"),  # the published record of the hour wins
        (AS_OF - 2 * HOUR, 60.0, "m1", "backfill"),  # a recomputed hour fills its slot
        (AS_OF - 3 * HOUR, 50.0, "m0", "build"),  # another methodology's: not mixed in
        (AS_OF - 4 * HOUR, 40.0, "m0", "backfill"),
    ]
    with connect(database) as conn:
        for at, score, method, source in rows:
            conn.execute(INSERT, ("alpha", "residential", "24h", at, score, method, source))
        built = [head("alpha", 90.0)]
        spark(conn, built, AS_OF, "m1")
    found = built[0]["windows"]["24h"]["atlas_score_spark"][-5:]
    assert [(p["score"], p["stored"]) for p in found] == [
        (None, False),
        (None, False),
        (60.0, True),
        (80.0, True),
        (90.0, True),
    ]


def test_the_v21_step_marks_every_record_published_and_keys_the_source(database):
    with connect(database) as conn:
        conn.execute("DROP TABLE calculated_score_history")
        conn.execute((HERE / "migrate_v18_v19.sql").read_text())  # the v19 table
        conn.execute(
            "INSERT INTO calculated_score_history VALUES ('alpha','residential','24h',%s,75.5,'m')",
            (AS_OF,),
        )
        conn.execute((HERE / "migrate_v20_v21.sql").read_text())
        assert conn.execute(ROWS).fetchall() == [("24h", AS_OF, 75.5, "build")]
        conn.execute(INSERT, ("alpha", "residential", "24h", AS_OF, 70.0, "m", "backfill"))
        assert [r[3] for r in conn.execute(ROWS)] == ["backfill", "build"]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(INSERT, ("alpha", "residential", "24h", AS_OF, 1.0, "m", "guess"))


def test_a_point_never_starts_beside_a_build(database):
    with connect(database) as conn:
        conn.execute(f"SELECT pg_advisory_lock({rollups.LOCK})")  # a build is running
        with pytest.raises(Busy):
            point(database, "unused.toml", AS_OF)
        conn.execute(f"SELECT pg_advisory_unlock({rollups.LOCK})")


WEIGHTS = "reliability = 22\nspeed = 14\nuptime = 10\ncoverage = 12\ndiversity = 8\n"
WEIGHTS += "dcAsns = 10\nauthenticity = 10\nvalue = 14\n"
ONLY = "reliability = 1\nspeed = 0\nuptime = 0\ncoverage = 0\ndiversity = 0\n"
ONLY += "dcAsns = 0\nauthenticity = 0\nvalue = 0\n"


def scored_policy(tmp_path):
    """lenient_policy() with a balanced score of the HTTP reliability alone, which the 3-day
    fixture measures (it has no price, DB-IP usage type or uptime batches for the rest)."""
    policy = lenient_policy(tmp_path)
    text = policy.read_text()
    assert text.count(WEIGHTS) == 2  # [weights] and [profiles.balanced]
    both = 'required_targets = ["generic_http", "generic_https"]'
    text = text.replace(WEIGHTS, ONLY).replace(both, 'required_targets = ["generic_http"]')
    pair = "[benchmark_weights]\ngeneric_http = 1\ngeneric_https = 1\n"
    assert pair in text
    text = text.replace(pair, "[benchmark_weights]\ngeneric_http = 1\n")
    policy.write_text(text)
    return policy


def scores_at(conn, at, source):
    query = (
        "SELECT provider,kind,period,score,methodology_id FROM calculated_score_history "
        "WHERE as_of=%s AND source=%s ORDER BY 1,2,3"
    )
    return conn.execute(query, (at, source)).fetchall()


def builds(conn):
    return conn.execute("SELECT build_id,published FROM calculation_builds ORDER BY 1").fetchall()


@pytest.mark.asyncio
async def test_the_backfill_equals_the_build_and_is_resumable_and_idempotent(
    database, tmp_path, capsys
):
    """On the 3-day fixture: a build published at end - 2h and the current one at end. With the
    earlier build's records gone (as before the history began), the backfill recomputes end - 2h
    equal to what that build published, then end - 1h equal to a draft build there, one point a
    run (--limit 1), and then nothing; no build of its own stays. The points run in forked
    processes of their own (FORK), the dry run's in this one."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    policy = scored_policy(tmp_path)
    calculate(database, policy, as_of=end - 2 * HOUR, mode="incremental", publish=True)
    calculate(database, policy, as_of=end, mode="incremental", publish=True)
    with connect(database) as conn:
        published = scores_at(conn, end - 2 * HOUR, "build")
        conn.execute("DELETE FROM calculated_score_history WHERE as_of<%s", (end,))
        before = builds(conn)
    assert published and any(row[3] is not None for row in published)
    argv = ["calculate-backfill-scores", "--database-file", str(database)]
    argv += ["--methodology", str(policy), "--hours", "2", "--days", "0"]
    assert main([*argv, "--list"]) == 0
    listed = [(end - 2 * HOUR).isoformat(), (end - HOUR).isoformat()]
    assert json.loads(capsys.readouterr().out)["left"] == listed
    with connect(database) as conn:  # a build is running: the run stops, resumable
        conn.execute(f"SELECT pg_advisory_lock({rollups.LOCK})")
        assert main(argv) == 3
        conn.execute(f"SELECT pg_advisory_unlock({rollups.LOCK})")
    capsys.readouterr()
    assert main([*argv, "--limit", "1"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert [p["as_of"] for p in first["points"]] == listed[:1] and first["left"] == listed[1:]
    with pytest.MonkeyPatch.context() as here:  # this point in the test's own process
        here.setattr(score_backfill, "FORK", False)
        dry = backfill(database, policy, hours=2, days=0, dry_run=True)
    assert [p["as_of"] for p in dry["points"]] == listed[1:]
    with connect(database) as conn:
        assert scores_at(conn, end - 2 * HOUR, "backfill") == published
        assert scores_at(conn, end - HOUR, "backfill") == []  # the dry run recorded nothing
    second = backfill(database, policy, hours=2, days=0)
    assert [p["as_of"] for p in second["points"]] == listed[1:] and second["left"] == []
    assert backfill(database, policy, hours=2, days=0)["points"] == []
    with connect(database) as conn:
        assert builds(conn) == before  # every point's build was rolled back
        recomputed = scores_at(conn, end - HOUR, "backfill")
    draft = calculate(database, policy, as_of=end - HOUR, mode="incremental")
    expected = [
        (n["provider"], n["kind"], label, w["scores"]["balanced"]["score"], first["methodology_id"])
        for n in draft["networks"]
        for label, w in n["windows"].items()
    ]
    assert recomputed == sorted(expected) and dry["points"][0]["scores"]


def test_the_command_checks_its_ranges(database):
    argv = ["calculate-backfill-scores", "--database-file", str(database), "--hours", "24"]
    with pytest.raises(SystemExit):
        main(argv)
    with connect(database) as conn:  # no current build: nothing to fill
        assert score_backfill.missing(conn, "m") == (None, [])
