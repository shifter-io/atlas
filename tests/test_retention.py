"""Build retention (store.expire), snapshot part partitions per build, shared lists and the light
browse views."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from calculated_cases import POLICY, benchmark_scenario
from psycopg import sql
from test_storage import measurement

from atlas.calculated import store
from atlas.calculated.calculator import calculate
from atlas.cli import main
from atlas.storage import connect, ingest

pytest_plugins = ["test_storage", "calculated_template"]


@pytest.fixture
def database(calculated_database):
    """Every test here works on the current calculated schema, copied from its template."""
    return calculated_database


DERIVED = (
    "calculated_series",
    "calculated_scores",
    "calculated_score_components",
    "calculated_score_reasons",
)


def columns(conn, table):
    return [
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
            "AND table_name=%s ORDER BY ordinal_position",
            (table,),
        )
    ]


def copy(conn, table, column, old, **values):
    """Copy the rows whose column is old, with values replacing columns (None: the default)."""
    names = [c for c in columns(conn, table) if c not in values]
    given = [k for k, v in values.items() if v is not None]
    query = sql.SQL("INSERT INTO {}({}) SELECT {} FROM {} WHERE {}=%s RETURNING *").format(
        sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, [*given, *names])),
        sql.SQL(",").join([*[sql.Placeholder()] * len(given), *map(sql.Identifier, names)]),
        sql.Identifier(table),
        sql.Identifier(column),
    )
    return conn.execute(query, [*[values[k] for k in given], old]).fetchall()


def clone(conn, source, as_of, *, published=True, payload=True):
    """A new build with every row of source, at another cutoff; its windows share source's lists."""
    build = copy(
        conn,
        "calculation_builds",
        "build_id",
        source,
        build_id=None,
        as_of=as_of,
        input_hash=uuid.uuid4().hex,
        published=published,
    )[0][0]
    windows = conn.execute(
        "SELECT result_id FROM calculated_windows WHERE build_id=%s ORDER BY 1", (source,)
    ).fetchall()
    for (old,) in windows:
        new = copy(conn, "calculated_windows", "result_id", old, result_id=None, build_id=build)
        for table in DERIVED:
            copy(conn, table, "result_id", old, result_id=new[0][0])
    copy(conn, "calculated_overlaps", "build_id", source, build_id=build)
    if payload:
        copy(conn, "calculated_snapshots", "build_id", source, build_id=build)
    return build


def rows_by_build(conn):
    """(table, build_id) -> rows; rows without a window would count under build None."""
    counts = {}
    for table in DERIVED:
        query = sql.SQL(
            "SELECT w.build_id,count(*) FROM {} d LEFT JOIN calculated_windows w USING(result_id) "
            "GROUP BY 1"
        ).format(sql.Identifier(table))
        counts |= {(table, b): n for b, n in conn.execute(query)}
    for table in (
        "calculated_windows",
        "calculated_overlaps",
        "calculated_snapshots",
        "calculated_snapshot_parts",
    ):
        query = sql.SQL("SELECT build_id,count(*) FROM {} GROUP BY 1").format(sql.Identifier(table))
        counts |= {(table, b): n for b, n in conn.execute(query)}
    # Shared list rows count under every build whose windows reference them.
    query = (
        "SELECT w.build_id,count(*) FROM calculated_list_rows "
        "JOIN calculated_windows w USING(list_id) GROUP BY 1"
    )
    return counts | {("calculated_list_rows", b): n for b, n in conn.execute(query)}


def partitions(conn):
    return {
        r[0]
        for r in conn.execute(
            "SELECT inhrelid::regclass::text FROM pg_inherits "
            "WHERE inhparent='calculated_snapshot_parts'::regclass"
        )
    }


def output(capsys, argv):
    assert main(argv) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.asyncio
async def test_expire_keeps_current_and_newest_published_per_utc_day(database, tmp_path, capsys):
    _, _, first = await benchmark_scenario(database, tmp_path)
    base = int(first["snapshot_id"])
    day = datetime(2026, 10, 31, tzinfo=UTC)
    with connect(database) as conn:
        # The scenario scores no extra reason; add one so every typed table has rows to expire.
        conn.execute("INSERT INTO score_reasons(reason) VALUES ('fixture reason')")
        conn.execute(
            "INSERT INTO calculated_score_reasons SELECT s.result_id,s.profile,1,r.reason_id "
            "FROM calculated_scores s JOIN calculated_windows USING(result_id),score_reasons r "
            "WHERE build_id=%s AND r.reason='fixture reason' LIMIT 1",
            (base,),
        )
        # The calculator created the build's partition.
        assert partitions(conn) == {f"calculated_snapshot_parts_b{base}"}
        assert {t for t, b in rows_by_build(conn) if b == base} == {
            *DERIVED,
            "calculated_list_rows",
            "calculated_windows",
            "calculated_overlaps",
            "calculated_snapshots",
            "calculated_snapshot_parts",
        }
        times = {
            "current": day + timedelta(hours=18),
            "same day, older": day + timedelta(hours=12),
            # 23:30 at UTC-2 is 01:30 UTC on the current build's day.
            "local previous day": datetime.fromisoformat("2026-10-30T23:30-02:00"),
            "newest of day": day - timedelta(seconds=1),
            "older of day": day - timedelta(hours=16),
            "tie, first": day - timedelta(hours=36),
            "tie, second": day - timedelta(hours=36),
            "first day of window": day - timedelta(days=29),
            "before window": day - timedelta(days=29, minutes=1),
        }
        b = {name: clone(conn, base, at) for name, at in times.items()}
        b["draft"] = clone(conn, base, day + timedelta(hours=20), published=False)
        b["pruned draft"] = clone(
            conn, base, day - timedelta(days=10), published=False, payload=False
        )
        conn.execute("UPDATE current_calculation SET build_id=%s", (b["current"],))
        store.discard(conn, base)
        assert f"calculated_snapshot_parts_b{base}" not in partitions(conn)
    kept = sorted(
        b[k] for k in ("current", "newest of day", "tie, second", "first day of window", "draft")
    )
    expired = sorted(set(b.values()) - set(kept))
    with connect(database) as conn:
        assert store.retained(conn) == (kept, expired)
        # Both numbers are configurable: one day with two builds, or no day at all.
        assert store.retained(conn, days=1, per_day=2)[0] == sorted(
            b[k] for k in ("current", "same day, older", "draft")
        )
        assert store.retained(conn, days=0)[0] == sorted([b["current"], b["draft"]])
        for days, per_day in ((-1, 1), (1.5, 1), (True, 1), (30, -1)):
            with pytest.raises(ValueError, match="non-negative"):
                store.retained(conn, days, per_day)
        before = rows_by_build(conn)
    argv = ["calculate-expire", "--database-file", str(database)]
    estimate = output(capsys, [*argv, "--dry-run"])
    assert (estimate["schema"], estimate["kept_builds"], estimate["expired_builds"]) == (
        "v20",
        kept,
        expired,
    )
    # Every clone shares the base build's lists, which kept builds still reference.
    shared = {"calculated_lists": 0, "calculated_list_rows": 0}
    assert estimate["rows"] == shared | {
        t: sum(n for (table, build), n in before.items() if table == t and build in expired)
        for t in estimate["rows"]
        if t not in shared
    }
    assert estimate["total_bytes"] >= estimate["bytes"]["calculated_snapshots"] > 0
    with connect(database) as conn:
        assert rows_by_build(conn) == before  # the dry run changed nothing
    assert output(capsys, argv) == {
        "schema": "v20",
        "kept_builds": kept,
        "expired_builds": expired,
    }
    with connect(database) as conn:
        # Kept builds keep every row; nothing of the others is left, and no orphans.
        assert rows_by_build(conn) == {k: n for k, n in before.items() if k[1] in kept}
        assert [
            r[0] for r in conn.execute("SELECT build_id FROM calculation_builds ORDER BY 1")
        ] == kept
        # Expired builds' snapshot parts go with their partitions too.
        assert partitions(conn) <= {f"calculated_snapshot_parts_b{k}" for k in kept}
        # The kept builds still share every list (the discarded base build wrote them).
        query = "SELECT count(*),bool_and(created_build=%s) FROM calculated_lists"
        listed = conn.execute(query, (base,)).fetchone()
        referenced = conn.execute("SELECT count(DISTINCT list_id) FROM calculated_windows")
        assert listed == (referenced.fetchone()[0], True) and listed[0]
    assert output(capsys, argv) == {"schema": "v20", "kept_builds": kept, "expired_builds": []}


@pytest.mark.asyncio
async def test_publication_expires_older_builds_of_the_day(database, tmp_path):
    directory = await measurement(tmp_path, requests=4)
    ingest(directory, database)
    raw = json.loads((directory / "summary.json").read_text())
    end = datetime.fromisoformat(raw["finished_at"]) + timedelta(minutes=5)
    first = int(calculate(database, POLICY, as_of=end, publish=True)["snapshot_id"])
    # New data revises the same cutoff: the revision replaces the first publication.
    ingest(await measurement(tmp_path, requests=4, provider="second"), database)
    second = int(calculate(database, POLICY, as_of=end, publish=True)["snapshot_id"])
    assert second != first
    assert store.read(database, str(first)) is None
    ingest(await measurement(tmp_path, requests=4, provider="third"), database)
    draft = calculate(database, POLICY, as_of=end)["snapshot_id"]
    with connect(database) as conn:
        builds = [r[0] for r in conn.execute("SELECT build_id FROM calculation_builds ORDER BY 1")]
        assert builds == [second, int(draft)]
        assert partitions(conn) == {f"calculated_snapshot_parts_b{b}" for b in builds}
    assert store.publish(database, draft) == {
        "published_snapshot": draft,
        "expired_builds": [second],
    }
    assert store.read(database)["snapshot_id"] == draft


def test_retention_without_current_build_or_known_schema(database, capsys):
    with connect(database) as conn:
        conn.execute("INSERT INTO methodologies VALUES ('m','{}')")
        conn.execute(
            "INSERT INTO calculation_builds(methodology_id,as_of,input_hash) "
            "VALUES ('m','2026-01-01Z','h')"
        )
    argv = ["calculate-expire", "--database-file", str(database)]
    # Nothing is current, so nothing can be dated: nothing expires.
    assert output(capsys, argv) == {"schema": "v20", "kept_builds": [], "expired_builds": []}
    with connect(database) as conn:
        conn.execute("UPDATE calculation_schema SET checksum='edited'")
    # The dry run checks the layer exactly like the real run: unknown fails both.
    for dry in ([], ["--dry-run"]):
        assert main([*argv, *dry]) == 2
        assert "ValueError" in capsys.readouterr().err
        with pytest.raises(ValueError, match="db-migrate"):
            store.retention(database, dry_run=bool(dry))
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO calculation_schema VALUES (%s,'second row')", (store.SCHEMA_VERSION + 1,)
        )
    with pytest.raises(ValueError, match="db-migrate"):
        store.retention(database, dry_run=True)
    with connect(database) as conn:
        # A calculated v5 database may expire before its migration.
        conn.execute("DELETE FROM calculation_schema")
        conn.execute("INSERT INTO calculation_schema VALUES (5,%s)", (store.V5_CHECKSUM,))
        assert conn.execute("SELECT count(*) FROM calculation_builds").fetchone() == (1,)
    assert output(capsys, [*argv, "--dry-run"])["schema"] == "v5"
    assert store.retention(database) == {"schema": "v5", "kept_builds": [], "expired_builds": []}


@pytest.mark.asyncio
async def test_browse_views_never_read_payloads(database, tmp_path):
    _, _, first = await benchmark_scenario(database, tmp_path)
    build = int(first["snapshot_id"])
    with connect(database) as conn:
        # An incompressible filler keeps the payload out of line, in the TOAST table.
        conn.execute(
            "UPDATE calculated_snapshots SET payload=payload||jsonb_build_object('filler',"
            "(SELECT string_agg(md5(random()::text),'') FROM generate_series(1,4000)))"
        )
    with connect(database) as conn:
        toast = conn.execute(
            "SELECT reltoastrelid FROM pg_class WHERE oid='calculated_snapshots'::regclass"
        ).fetchone()[0]

        def fetched():
            query = "SELECT pg_stat_get_xact_blocks_fetched(%s)"
            return conn.execute(query, (toast,)).fetchone()[0]

        listed = conn.execute(
            "SELECT build_id,published,is_current,payload_bytes>50000 "
            "FROM calculated_snapshots_list"
        ).fetchall()
        windows = conn.execute(
            "SELECT count(*),count(DISTINCT build_id),bool_and(product IS NOT NULL) "
            "FROM current_windows"
        ).fetchone()
        breakdowns = conn.execute(
            "SELECT dimension,position,provider,country,city,asn_name,ips,success_rate "
            "FROM current_breakdowns ORDER BY result_id,dimension,position"
        ).fetchall()
        assert fetched() == 0
        conn.execute("SELECT payload::text FROM calculated_snapshots").fetchall()
        assert fetched() > 0  # the counter does see a payload read
        details = conn.execute(
            "SELECT d.dimension,d.position,p.provider,d.label->>'country',d.label->>'city',"
            "d.label->>'name',d.ips,d.success_rate FROM calculated_breakdown_details d "
            "JOIN calculated_windows w USING(result_id) JOIN products p USING(product_id) "
            "WHERE w.build_id=%s ORDER BY d.result_id,d.dimension,d.position",
            (build,),
        ).fetchall()
        expected = conn.execute(
            "SELECT count(*) FROM calculated_windows WHERE build_id=%s", (build,)
        ).fetchone()[0]
    assert listed == [(build, True, True, True)]
    assert windows == (expected, 1, True)
    assert breakdowns == details and any(r[3] for r in breakdowns) and any(r[5] for r in breakdowns)
