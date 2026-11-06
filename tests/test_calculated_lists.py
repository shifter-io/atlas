"""Shared breakdown lists (contract K2, calculated v9): what persist writes, the v8 → v9
migration of real calculator output, garbage collection after discard and expire, and the
privileges the run-time and API roles get on the new tables and views."""

import hashlib
import secrets
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from calculated_cases import benchmark_scenario, lenient_policy, payload_mismatches
from migration_cases import catalog
from psycopg import sql
from test_dbserver import apply, toml
from test_retention import clone

from proxy_index import detect, traffic
from proxy_index.calculated import browser, calculator, incremental, lists, netlists, store
from proxy_index.calculated.aggregate import BREAKDOWNS
from proxy_index.calculated.calculator import calculate
from proxy_index.dbserver import Roles
from proxy_index.dbserver.check import check
from proxy_index.providers.credentials import read_credentials
from proxy_index.storage import connect, initialize, migrate

pytest_plugins = ["test_storage", "calculated_template"]

CALCULATED_V8 = Path(__file__).parent / "fixtures/calculated_v8.sql"
# Lists retention keeps no longer: group lists no window references, network lists (K10) whose
# build has no window left.
KEPT = "w.list_id=l.list_id OR l.cohort='network' AND w.build_id=l.created_build"
ORPHANS = (
    "SELECT (SELECT count(*) FROM calculated_lists l WHERE NOT EXISTS (SELECT FROM "
    f"calculated_windows w WHERE {KEPT})),(SELECT count(*) FROM calculated_list_rows r JOIN "
    f"calculated_lists l USING(list_id) WHERE NOT EXISTS (SELECT FROM calculated_windows w WHERE "
    f"{KEPT}))"
)
GROUP_ROWS = (
    "SELECT count(*) FROM calculated_list_rows JOIN calculated_lists USING(list_id) "
    "WHERE cohort<>'network'"
)
VIEWS = ("calculated_list_details", "calculated_breakdown_details", "current_breakdowns")


def views(conn):
    """Both reader views of the breakdowns, row for row."""
    return {
        name: conn.execute(f"SELECT * FROM {name} ORDER BY result_id,dimension,position").fetchall()
        for name in ("calculated_breakdown_details", "current_breakdowns")
    }


@pytest.mark.asyncio
async def test_each_target_free_window_writes_one_list_by_copy(database, tmp_path, monkeypatch):
    statements = []
    execute = psycopg.Cursor.execute

    def spy(self, query, *args, **kwargs):
        statements.append(str(query))
        return execute(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", spy)
    _, _, first = await benchmark_scenario(database, tmp_path)
    monkeypatch.undo()
    build = int(first["snapshot_id"])
    windows = [(p, w) for p in first["products"] for w in p["windows"].values()]
    # One INSERT per group list, and the rows go by COPY: never an INSERT per row. Each network
    # window's list and rows come from one INSERT … SELECT (netlists.py).
    networks = {(p["provider"], p["kind"]) for p in first["products"]}
    assert statements.count(netlists.MERGE) == len(networks) * len(first["periods"])
    groups = [s for s in statements if s != netlists.MERGE]
    assert sum("INSERT INTO calculated_lists" in s for s in groups) == len(windows)
    assert not any("INSERT INTO calculated_list_rows" in s for s in groups)
    assert payload_mismatches(database, build) == []
    with connect(database) as conn:
        assert conn.execute(
            "SELECT count(*) FILTER (WHERE (target='')<>(list_id IS NOT NULL)),"
            "count(DISTINCT list_id) FROM calculated_windows WHERE build_id=%s",
            (build,),
        ).fetchone() == (0, len(windows))
        stored = {
            tuple(r[:3]): r[3:]
            for r in conn.execute(
                "SELECT l.product_id,l.cohort,l.window_hours,l.signature,l.created_build,"
                "l.entries,l.figures,l.content_hash,(SELECT count(*) FROM calculated_list_rows r "
                "WHERE r.list_id=l.list_id) FROM calculated_lists l WHERE l.cohort<>'network'"
            )
        }
        assert conn.execute(ORPHANS).fetchone() == (0, 0)
    assert len(stored) == len(windows)
    for p, w in windows:
        signature, created, entries, figures, content_hash, rows = stored[
            (p["product_id"], p["cohort"], w["period"]["hours"])
        ]
        assert (signature, created) == (None, build)
        # The slimmed window keeps the uncapped counts and totals the list was written from.
        assert entries == w["breakdown_entries"] and set(entries) == set(BREAKDOWNS)
        assert rows == sum(entries.values()) > 0
        assert figures == {k: w["pool"][k] for k in lists.FIGURES} | {
            "list_entries": w["pool"]["list_entries"]
        }
        assert "new_ips" not in figures and len(content_hash) == 64


def downgrade_to_v8(database, empty):
    """Store a calculated v9 database's lists as a v8 release did (each build's breakdowns in
    its own partition of calculated_breakdowns, no lists); the window empty loses its rows."""
    assert hashlib.sha256(CALCULATED_V8.read_bytes()).hexdigest() == store.V8_CHECKSUM
    with connect(database) as conn:
        conn.execute(
            "CREATE TEMP TABLE v9_rows ON COMMIT DROP AS SELECT w.build_id,w.result_id,r.* "
            "FROM calculated_windows w JOIN calculated_list_rows r USING(list_id) "
            "WHERE w.result_id<>%s",
            (empty,),
        )
        conn.execute(
            "DROP VIEW current_breakdowns,calculated_breakdown_details,calculated_list_details,"
            "current_windows"  # current_windows selects w.*, list_id included
        )
        conn.execute("ALTER TABLE calculated_windows DROP COLUMN list_id")
        conn.execute("DROP TABLE calculated_list_rows,calculated_lists")
        conn.execute(  # the calculated v10 memo tables (CALC-INC-1a)
            "DROP TABLE calculated_run_ranges,calculated_slot_figures,calculated_distinct,"
            "calculated_bucket_ips,calculated_ip_last_seen"
        )
        conn.execute(
            "DROP TABLE calculated_geo_hours,calculated_geo_rollups,calculated_pulse_buckets,"
            "calculated_pulse_rollups"
        )  # calculated v11 and v12
        conn.execute(CALCULATED_V8.read_text())
        builds = conn.execute("SELECT DISTINCT build_id FROM v9_rows").fetchall()
        assert builds
        for (build,) in builds:
            store.partition(conn, build, "calculated_breakdowns")
        columns = (
            "build_id,result_id,dimension,position,place_id,asn_label_id,ips,observations,"
            "completed,successes,challenges,blocks,p50,p90,p99"
        )
        conn.execute(f"INSERT INTO calculated_breakdowns({columns}) SELECT {columns} FROM v9_rows")
        conn.execute("UPDATE calculation_schema SET version=8,checksum=%s", (store.V8_CHECKSUM,))


@pytest.mark.asyncio
async def test_v8_breakdowns_migrate_into_lists_unchanged(
    databases, database, calculated_template, tmp_path, monkeypatch
):
    # A v8 release stored payload v2: each part holds its rows, not a list id (SNAP-LISTREF-1).
    monkeypatch.setattr(calculator, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(store, "refer", lambda product, lists: product)
    _, _, first = await benchmark_scenario(database, tmp_path)
    cutoff = datetime.fromisoformat(first["as_of"]) + timedelta(hours=1)
    # A draft: publishing it would expire the first build (one build per UTC day is kept).
    second = calculate(database, lenient_policy(tmp_path), as_of=cutoff)
    monkeypatch.undo()
    builds = sorted(int(b["snapshot_id"]) for b in (first, second))
    with connect(database) as conn:
        empty = conn.execute(
            "SELECT min(result_id) FROM calculated_windows WHERE target='' AND build_id=%s",
            (builds[0],),
        ).fetchone()[0]
    downgrade_to_v8(database, empty)
    with connect(database) as conn:
        query = "SELECT count(*) FROM pg_inherits WHERE inhparent='calculated_breakdowns'::regclass"
        assert conn.execute(query).fetchone() == (2,)
        before = views(conn)
        windows = conn.execute(
            "SELECT result_id,product_id,cohort,window_hours,build_id,unique_ips FROM "
            "calculated_windows WHERE target='' ORDER BY 1"
        ).fetchall()
    assert all(before.values())
    result = migrate(database)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        8,
        12,
    )
    verified = [*store.V8_CHECKS, *store.V9_CHECKS, *store.V10_CHECKS, *store.V11_CHECKS]
    assert result["verified"] == verified
    with connect(database) as conn:
        assert views(conn) == before
        # One list per target-free window, numbered with its result_id; the rest have none.
        migrated = conn.execute(
            "SELECT list_id,product_id,cohort,window_hours,created_build,"
            "(figures->>'unique_ips')::bigint FROM calculated_lists ORDER BY 1"
        ).fetchall()
        assert migrated == windows
        assert conn.execute(
            "SELECT count(*) FROM calculated_windows WHERE list_id IS DISTINCT FROM "
            "CASE WHEN target='' THEN result_id END"
        ).fetchone() == (0,)
        entries = conn.execute(
            "SELECT entries,signature FROM calculated_lists WHERE list_id=%s", (empty,)
        ).fetchone()
        assert entries == (dict.fromkeys(BREAKDOWNS, 0), None)
        assert conn.execute("SELECT to_regclass('calculated_breakdowns')").fetchone() == (None,)
    assert migrate(database)["migrated"] is False
    reference = databases(template=calculated_template)
    assert catalog(database) == catalog(reference)
    # New lists follow the migrated ids, and every kept build still reads as it did.
    third = calculate(database, lenient_policy(tmp_path), as_of=cutoff + timedelta(hours=1))
    with connect(database) as conn:
        assert conn.execute(
            "SELECT min(list_id)>%s FROM calculated_lists WHERE created_build=%s",
            (windows[-1][0], int(third["snapshot_id"])),
        ).fetchone() == (True,)
    for build in builds[1:]:
        assert payload_mismatches(database, build) == []


def own_lists(conn, build):
    """Give the build's windows their own copies of the lists they reference."""
    for result, old in conn.execute(
        "SELECT result_id,list_id FROM calculated_windows WHERE build_id=%s AND list_id>0",
        (build,),
    ).fetchall():
        new = conn.execute(
            "INSERT INTO calculated_lists(product_id,cohort,window_hours,content_hash,entries,"
            "figures,created_build) SELECT product_id,cohort,window_hours,content_hash,entries,"
            "figures,%s FROM calculated_lists WHERE list_id=%s RETURNING list_id",
            (build, old),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO calculated_list_rows SELECT %s,dimension,position,place_id,asn_label_id,"
            "ips,observations,completed,successes,challenges,blocks,p50,p90,p99 "
            "FROM calculated_list_rows WHERE list_id=%s",
            (new, old),
        )
        conn.execute("UPDATE calculated_windows SET list_id=%s WHERE result_id=%s", (new, result))


@pytest.mark.asyncio
async def test_discard_and_expire_delete_only_lists_no_window_references(database, tmp_path):
    _, _, first = await benchmark_scenario(database, tmp_path)
    base = int(first["snapshot_id"])
    at = datetime.fromisoformat(first["as_of"])
    with connect(database) as conn:
        written = conn.execute(GROUP_ROWS).fetchone()[0]
        daily = clone(conn, base, at - timedelta(days=1))  # a kept daily build, sharing lists
        older = clone(conn, base, at - timedelta(days=40))  # expires, sharing lists
        own = clone(conn, base, at - timedelta(days=41))  # expires, with lists of its own
        own_lists(conn, own)
        mine = conn.execute(
            "SELECT count(DISTINCT list_id),count(*) FROM calculated_list_rows JOIN "
            "calculated_windows USING(list_id) WHERE build_id=%s",
            (own,),
        ).fetchone()
        conn.execute("UPDATE current_calculation SET build_id=%s", (daily,))
        # The discarded build wrote the lists, but another build's windows still use them.
        store.discard(conn, base)
        assert conn.execute(GROUP_ROWS).fetchone()[0] == written + mine[1]
    estimate = store.retention(database, dry_run=True)
    assert (estimate["kept_builds"], estimate["expired_builds"]) == ([daily], [older, own])
    assert (estimate["rows"]["calculated_lists"], estimate["rows"]["calculated_list_rows"]) == mine
    assert store.retention(database)["expired_builds"] == [older, own]
    with connect(database) as conn:
        assert conn.execute(ORPHANS).fetchone() == (0, 0)
        assert conn.execute(
            "SELECT count(*),bool_and(created_build=%s) FROM calculated_lists", (base,)
        ).fetchone() == (mine[0], True)
        assert conn.execute(GROUP_ROWS).fetchone()[0] == written
        assert store.collect(conn) == 0
        # The kept build reads every row it had.
        assert conn.execute(
            "SELECT count(*) FROM calculated_breakdown_details JOIN calculated_windows "
            "USING(result_id) WHERE build_id=%s",
            (daily,),
        ).fetchone() == (written,)


@pytest.fixture
def owned(databases, tmp_path):
    """A calculated v8 database whose schema belongs to a non-superuser admin role, with the
    privileges `db-server check` asks for (default privileges included) applied."""
    superuser = read_credentials(databases(), "postgres") | {"sslmode": "disable"}
    tag = uuid.uuid4().hex[:8]
    roles = Roles(runtime=f"rt_{tag}", api=f"api_{tag}", admin=f"adm_{tag}")
    files = {"superuser": toml(tmp_path / "superuser.toml", superuser)}
    with psycopg.connect(**superuser, autocommit=True) as conn:
        for role in roles:
            password = secrets.token_hex(16)
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOCREATEDB PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
            files[role] = toml(
                tmp_path / f"{role}.toml", superuser | {"user": role, "password": password}
            )
        conn.execute(sql.SQL("ALTER SCHEMA public OWNER TO {}").format(sql.Identifier(roles.admin)))
    initialize(files[roles.admin])
    with connect(files[roles.admin]) as conn:
        conn.execute(CALCULATED_V8.read_text())
        conn.execute("INSERT INTO calculation_schema VALUES (8,%s)", (store.V8_CHECKSUM,))
    server = SimpleNamespace(roles=roles, files=files)
    apply(server, check(files["superuser"], roles)["gaps"])
    try:
        yield server
    finally:
        names = [sql.Identifier(role) for role in roles]
        with psycopg.connect(**superuser, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {} CASCADE").format(sql.SQL(", ").join(names)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.SQL(", ").join(names)))


def test_migrated_lists_are_written_by_runtime_and_read_by_api_roles(owned):
    roles, files = owned.roles, owned.files
    # The admin role migrates; the new tables and views get the default privileges only.
    assert migrate(files[roles.admin])["calculated_schema_version"] == 12
    counters = {"ips": 2, "observations": 3, "completed": 4, "successes": 4, "success_rate": 1.0}
    counters |= {"challenges": 0, "blocks": 0, "p50": 1, "p90": 2, "p99": 3}
    country = {"country_code": "XA", "country": "Example"} | counters
    rows = {name: [] for name in BREAKDOWNS} | {"country": [country]}
    window = {"breakdowns": rows, "pool": {"unique_ips": 2}, "period": {"hours": 24}}
    with connect(files[roles.runtime]) as conn:
        conn.execute("INSERT INTO providers VALUES ('example')")
        product = conn.execute(
            "INSERT INTO products(provider,name) VALUES ('example','residential') "
            "RETURNING product_id"
        ).fetchone()[0]
        written = []
        group = {"product_id": product, "cohort": "c"}
        list_id = lists.write_list(conn, 1, group, window, written)
        lists.copy_rows(conn, written)
    with connect(files[roles.api]) as conn:
        assert conn.execute(
            "SELECT dimension,position,label->>'country',ips,success_rate "
            "FROM calculated_list_details WHERE list_id=%s",
            (list_id,),
        ).fetchall() == [("country", 0, "Example", 2, 1.0)]
        for name in (*VIEWS, "calculated_lists", "calculated_list_rows"):
            conn.execute(f"SELECT count(*) FROM {name}")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), connect(files[roles.api]) as conn:
        conn.execute("DELETE FROM calculated_list_rows")
    with connect(files[roles.runtime]) as conn:
        # No window references the list: collection deletes it, its rows first.
        assert store.collect(conn) == 1
        assert conn.execute("SELECT count(*) FROM calculated_list_rows").fetchone() == (0,)


def test_implementation_id_hashes_the_list_writer():
    """persist and the list figures live in lists.py: a change there must change the build
    fingerprint (and the list signature's implementation id), as it did inside calculator.py."""
    hashed = ("calculator.py", "aggregate.py", "policy.py", "slim.py", "inputs.py", "lists.py")
    # The incremental calculation's modules (CALC-INC-1a) and the SQL counts they use.
    hashed += ("incremental.py", "hourly.py", "fleetsql.py", "rollups.py", "sqlwindow.py")
    hashed += ("poolfig.py", "network.py", "network_scores.py")  # networks (NET-SUM-1, -2)
    hashed += ("scheduled.py",)  # the census/pulse inputs, split out of calculator.py (LIMITS-1)
    hashed += ("scores.py",)  # moved out of aggregate.py (LIMITS-6)
    hashed += ("window_scoring.py",)  # the scoring helpers, split out of calculator.py
    hashed += ("hourly_windows.py",)  # moved out of hourly.py (FILE-3)
    hashed += ("product_windows.py",)  # build()'s window steps, split out of calculator.py
    here = Path(calculator.__file__).parent
    expected = hashlib.sha256(
        b"".join((here / name).read_bytes() for name in hashed)
        + b"".join(Path(m.__file__).read_bytes() for m in (detect, traffic, browser))
    ).hexdigest()
    assert calculator.IMPLEMENTATION_ID == expected
    # calculate() writes through lists.reusing(), which wraps lists.persist with the signatures.
    for writer in (lists.reusing, lists.persist, lists.signatures, incremental.read):
        assert Path(sys.modules[writer.__module__].__file__).name in hashed
