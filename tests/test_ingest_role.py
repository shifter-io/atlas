"""SEC-INGEST-ROLE-1 (#1022): the manager's upload path ingests with a login that may only add runs.

The reviewed file scripts/db/ingest-role.sql makes the role on a database in production's state
(EXECUTE and TEMPORARY revoked from PUBLIC); its dry run changes nothing, and `db-server check`
asks for exactly its grants and finds nothing missing or beyond them after it. The real upload
path (/v1/results) and storage.ingest's other shapes (a daily test with its verdict, a pulse's
counters and geo rows, a stored run joined to a batch, a replay) then run under that role, the
day's partitions made by the run-time login, and what the role may not do is refused. The
rollback file removes the role. Each test renames the role (roles are the server's, shared by
parallel tests).
"""

import json
import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from ingest_cases import daily_test, pulse
from orchestrator_cases import bearer, prepare, query
from psycopg import sql
from test_storage import enrichment, measurement

from atlas import dbserver, storage
from atlas.daily import DAILY, MARKER
from atlas.dbserver import Roles
from atlas.dbserver.check import check
from atlas.orchestrator import api, batches
from atlas.orchestrator.app import build_app, use_ingest_login
from atlas.orchestrator.archive import pack
from atlas.orchestrator.db import Database
from atlas.orchestrator.nodes import issue_token
from atlas.providers.credentials import read_credentials

pytest_plugins = ["test_storage"]

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "db"
NAME = "atlas_ingest"
INGEST_GAPS = {"ingest_login", "ingest_excess", "ingest_role", "ingest_grant"}
# What the ingest role may not do, and PostgreSQL's words for it.
REFUSED = [
    ("DELETE FROM test_runs", "permission denied for table test_runs"),
    ("UPDATE test_runs SET status='failed'", "permission denied for table test_runs"),
    ("UPDATE scope_stats SET scope_hash='x'", "permission denied for table scope_stats"),
    ("TRUNCATE ip_addresses", "permission denied for table ip_addresses"),
    ("SELECT count(*) FROM ip_observations", "permission denied for table ip_observations"),
    ("SELECT count(*) FROM nodes", "permission denied for table nodes"),
    ("SELECT value FROM settings", "permission denied for table settings"),
    ("UPDATE batch_runs SET status='done'", "permission denied for table batch_runs"),
    ("INSERT INTO settings(key,value) VALUES ('x','1')", "permission denied for table settings"),
    ("CREATE TABLE scratch (x integer)", "permission denied for schema public"),
    ("ALTER TABLE test_runs ADD COLUMN x integer", "must be owner of table test_runs"),
    ("DROP TABLE run_buckets_2026_11_02", "must be owner of table run_buckets_2026_11_02"),
]


def script(name, role):
    return (SCRIPTS / name).read_text().replace(NAME, role)


def psql(credentials, text, *, apply=False):
    """Run a file's text with psql as the operator would (its dry run unless `apply`)."""
    binary = Path(os.environ["PG_BIN"], "psql") if os.environ.get("PG_BIN") else None
    binary = binary or shutil.which("psql")
    assert binary, "psql is needed: set PG_BIN (CI does) or put psql on PATH"
    names = {"host": "PGHOST", "port": "PGPORT", "user": "PGUSER", "dbname": "PGDATABASE"}
    env = os.environ | {variable: str(credentials[key]) for key, variable in names.items()}
    env |= {"PGPASSWORD": credentials["password"], "PGSSLMODE": "disable"}
    flags = ["-v", "apply=1"] if apply else []
    command = [str(binary), "-X", "-v", "ON_ERROR_STOP=1", *flags, "-f", "-"]
    done = subprocess.run(command, input=text, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def toml(path, credentials):
    path.write_text(
        "[postgres]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in credentials.items())
    )
    path.chmod(0o600)
    return str(path)


def ingest_gaps(server, role=None):
    report = check(server.runtime, server.roles._replace(ingest=role or server.role))
    return {g["kind"]: g["statements"] for g in report["gaps"] if g["kind"] in INGEST_GAPS}


def exists(server, role):
    with dbserver.connect(server.runtime) as conn:
        found = conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
    return found is not None


def as_runtime(server, *statements):
    with dbserver.connect(server.runtime, autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)


@pytest.fixture
def server(database, tmp_path):
    """A database in production's state, its run-time login (this test server's superuser, so
    every checked role name is its) and an ingest role made by the reviewed file."""
    superuser = read_credentials(database, "postgres")
    role, name = f"ing_{uuid.uuid4().hex[:10]}", sql.Identifier(superuser["dbname"])
    with dbserver.connect(database, autocommit=True) as conn:
        conn.execute("REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC")
        conn.execute(sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(name))
    me = superuser["user"]
    found = SimpleNamespace(runtime=str(database), superuser=superuser, role=role)
    found.roles = Roles(runtime=me, api=me, admin=me, ingest=role)
    try:
        yield found
    finally:
        if exists(found, role):
            psql(superuser, script("ingest-role-rollback.sql", role), apply=True)
        assert not exists(found, role)


def make_role(server, tmp_path):
    """The reviewed file, applied, and the role's own database file (the test sets a password;
    the operator types it at \\password)."""
    out = psql(server.superuser, script("ingest-role.sql", server.role), apply=True)
    assert "Applied: the role exists." in out
    password = uuid.uuid4().hex
    as_runtime(
        server,
        sql.SQL("ALTER ROLE {} PASSWORD {}").format(
            sql.Identifier(server.role), sql.Literal(password)
        ),
    )
    login = server.superuser | {"user": server.role, "password": password}
    return toml(tmp_path / "ingest.toml", login)


def test_the_reviewed_file_is_what_the_check_asks_for_and_closes_every_gap(server, tmp_path):
    role, text = server.role, script("ingest-role.sql", server.role)
    statements = [line for line in text.splitlines() if line.startswith(("CREATE", "GRANT"))]
    assert ingest_gaps(server) == {"ingest_login": [f'CREATE ROLE "{role}" LOGIN NOINHERIT;']}
    assert statements[0] == f'CREATE ROLE "{role}" LOGIN NOINHERIT;'
    # A bare role (another name): the check's grants are the file's, in its order.
    bare = f"{role}_b"
    as_runtime(server, f'CREATE ROLE "{bare}" LOGIN NOINHERIT')
    try:
        asked = ingest_gaps(server, bare)
    finally:
        as_runtime(server, f'DROP ROLE "{bare}"')
    assert set(asked) == {"ingest_grant"}
    assert [s.replace(bare, role) for s in asked["ingest_grant"]] == statements[1:-1]
    assert statements[-1] == f'GRANT TEMPORARY ON DATABASE :"DBNAME" TO "{role}";'

    out = psql(server.superuser, text)  # the dry run
    assert "Dry run: nothing changed." in out and "test_runs" in out
    assert not exists(server, role)
    make_role(server, tmp_path)
    assert ingest_gaps(server) == {}
    report = check(server.runtime, server.roles)
    assert role not in report["limits"]["temporary"]["missing"]

    dry = psql(server.superuser, script("ingest-role-rollback.sql", role))
    assert "Dry run: nothing changed." in dry and exists(server, role)


def test_the_check_reports_every_right_beyond_adding_runs_and_its_statements_remove_them(
    server, tmp_path
):
    make_role(server, tmp_path)
    storage.ingest(daily_test(tmp_path / "d", 0, ips=20, repeats=5), server.runtime)  # a partition
    role = sql.Identifier(server.role)
    as_runtime(
        server,
        *(
            sql.SQL(statement).format(role)
            for statement in (
                "GRANT DELETE, TRUNCATE ON test_runs TO {}",
                "GRANT UPDATE (status) ON test_runs TO {}",
                "GRANT UPDATE ON scope_stats TO {}",
                "GRANT SELECT ON nodes TO {}",
                "GRANT DELETE ON run_buckets_2026_11_02 TO {}",
                "GRANT CREATE ON SCHEMA public TO {}",
                "ALTER ROLE {} CREATEDB",
                "GRANT pg_read_all_data TO {}",
            )
        ),
    )
    quoted = f'"{server.role}"'
    gaps = ingest_gaps(server)
    assert gaps["ingest_excess"] == [
        f'REVOKE CREATE ON SCHEMA "public" FROM {quoted};',
        f"REVOKE SELECT ON public.nodes FROM {quoted};",
        f"REVOKE DELETE ON public.run_buckets_2026_11_02 FROM {quoted};",
        f"REVOKE UPDATE ON public.scope_stats FROM {quoted};",
        f"REVOKE DELETE, TRUNCATE ON public.test_runs FROM {quoted};",
        f'REVOKE UPDATE ("status") ON public.test_runs FROM {quoted};',
    ]
    assert gaps["ingest_role"] == [
        f"ALTER ROLE {quoted} NOCREATEDB;",
        f'REVOKE "pg_read_all_data" FROM {quoted};',
    ]
    columns = '"metrics", "histogram_steps", "histogram_counts", "updated_at"'
    assert gaps["ingest_grant"] == [f"GRANT UPDATE ({columns}) ON public.scope_stats TO {quoted};"]
    order = ("ingest_excess", "ingest_role", "ingest_grant")
    as_runtime(server, *(s for kind in order for s in gaps[kind]))
    assert ingest_gaps(server) == {}


async def test_the_upload_path_ingests_under_the_role_and_nothing_more(server, tmp_path):
    login = make_role(server, tmp_path)
    runtime = server.runtime
    db = Database(runtime)
    app = build_app(runtime, with_scheduler=False, db=db)
    use_ingest_login(app, login)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        batch_id, run_key = await _census_upload(runtime, db, client, tmp_path)
    finally:
        await client.close()
        await db.close()
    # The day's partitions are the run-time login's: the role made none.
    owners = query(
        runtime,
        "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_inherits i "
        "JOIN pg_class c ON c.oid=i.inhrelid WHERE i.inhparent='run_buckets'::regclass",
    )
    assert owners == [(server.superuser["user"],)]
    assert query(runtime, "SELECT run_key FROM batch_runs WHERE batch_id=%s", (batch_id,)) == [
        (run_key,)
    ]
    _ingest_every_shape(server, login, tmp_path, batch_id)
    for statement, words in REFUSED:
        with dbserver.connect(login, autocommit=True) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match=words):
                conn.execute(statement)


async def _census_upload(runtime, db, client, tmp_path):
    """One census run uploaded, enriched, and sent again (a retry after a lost reply)."""
    now = datetime.now(UTC)
    prepare(runtime, ("DE",), DE=now.time())
    node = issue_token(runtime, provider="fixture", product="residential", region="fra1")
    await batches.census_plan(db, now)
    ((batch_id,),) = query(runtime, "SELECT batch_id FROM test_batches")
    headers = bearer(node["token"])
    assert (await client.get("/v1/work", headers=headers)).status == 200
    directory = await measurement(tmp_path / "census")
    enrichment(directory)
    archive = pack(directory, tmp_path / "census.tar.gz").read_bytes()
    url = f"/v1/results?batch_id={batch_id}"
    replies = []
    for _ in range(2):
        reply = await client.post(url, data=archive, headers=headers)
        assert reply.status == 200, await reply.text()
        replies.append(await reply.json())
    assert replies[0]["imported"] and replies[0]["status"] == "done"
    assert replies[1]["run_key"] == replies[0]["run_key"]
    return batch_id, replies[0]["run_key"]


def _ingest_every_shape(server, login, tmp_path, batch_id):
    """storage.ingest as the upload path calls it, for what a census upload does not reach."""
    daily = daily_test(tmp_path / "daily", 0, ips=40, repeats=10, provider="fixture")
    summary = json.loads((daily / "summary.json").read_text())
    (daily / "summary.json").write_text(json.dumps(summary | {MARKER: DAILY}))
    other = daily_test(tmp_path / "other", 1, ips=40, repeats=10, provider="other")
    beat = pulse(tmp_path / "pulse", 0, geo=20)
    enrichment(beat)
    for directory, options in (
        (daily, {}),
        (other, {}),
        (beat, {"ip_rows": False, "counters": True}),
        (daily, {"batch_id": batch_id}),  # a stored run joins the batch it is uploaded with
    ):
        storage.ingest(directory, login, partitions_file=server.runtime, **options)
    counts = query(
        server.runtime,
        "SELECT (SELECT count(*) FROM country_verdicts),(SELECT count(*) FROM run_geo_buckets),"
        "(SELECT count(*) FROM provider_overlap),(SELECT batch_id FROM test_runs WHERE run_id=%s)",
        (summary["run_id"],),
    )
    assert counts == [(1, 20, 1, batch_id)]


async def test_the_upload_path_chooses_its_file_and_the_jobs_keep_theirs(tmp_path, monkeypatch):
    runtime = toml(tmp_path / "runtime.toml", {"host": "127.0.0.1", "user": "rt", "password": "p"})
    login = toml(tmp_path / "ingest.toml", {"host": "127.0.0.1", "user": "in", "password": "p"})
    calls = []
    monkeypatch.setattr(storage, "ingest", lambda *args, **kw: calls.append((args, kw)) or {})
    for chosen in (None, login):
        app = build_app(runtime, with_scheduler=False, admin_credential=None)
        use_ingest_login(app, chosen)
        assert app[api.DB_KEY].credentials_file == runtime  # the jobs and the run states
        await api._ingest(SimpleNamespace(app=app), 7, tmp_path, ip_rows=True)
    (first, kw_first), (second, kw_second) = calls
    assert (first[1], kw_first["partitions_file"]) == (runtime, None)
    assert (second[1], kw_second["partitions_file"]) == (login, runtime)
    bad = toml(tmp_path / "bad.toml", {"host": "127.0.0.1", "options": "-c x=y"})
    with pytest.raises(ValueError, match="Unsupported database connection field"):
        use_ingest_login(web.Application(), bad)
