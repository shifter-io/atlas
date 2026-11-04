"""`db-server check|analyze` on a PostgreSQL 17 database with an admin-owned schema.

The schema belongs to a non-superuser admin role; the run-time and API roles own nothing. The
check must report every gap without changing anything, and the statements it prints, run as
it says (admin config or superuser), must close them: the run-time role can then create, drop
and ANALYZE partitions, the API role can read, and the admin role can still migrate.
"""

import json
import secrets
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import sql

from proxy_index import dbserver
from proxy_index.calculated.store import initialize as initialize_calculated
from proxy_index.cli import main
from proxy_index.dbserver import Roles
from proxy_index.dbserver.analyze import analyze
from proxy_index.dbserver.check import check
from proxy_index.partitions import partition
from proxy_index.providers.credentials import read_credentials
from proxy_index.storage import initialize

pytest_plugins = ["test_storage"]

PARENTS = {"ip_observations", "run_buckets", "run_geo_buckets", "test_events"}
JAN, FEB = datetime(2026, 1, 15, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)


def toml(path, credentials):
    path.write_text(
        "[postgres]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in credentials.items())
    )
    path.chmod(0o600)
    return path


@pytest.fixture
def server(databases, tmp_path):
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
    initialize_calculated(files[roles.admin])
    with dbserver.connect(files[roles.admin], autocommit=True) as conn:
        partition(conn, "run_buckets", JAN)  # an admin-owned partition, made before the fix
    try:
        yield SimpleNamespace(roles=roles, files=files, superuser=superuser)
    finally:
        names = [sql.Identifier(role) for role in roles]
        with psycopg.connect(**superuser, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {} CASCADE").format(sql.SQL(", ").join(names)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.SQL(", ").join(names)))


def apply(server, gaps):
    """Run each gap's statements the way it says: the admin config, or a superuser."""
    for gap in gaps:
        file = server.files[server.roles.admin if gap["run_as"] == "admin" else "superuser"]
        with dbserver.connect(file, autocommit=True) as conn:
            for statement in gap["statements"]:
                conn.execute(statement)


def test_check_reports_the_gaps_and_its_statements_close_them(server):
    roles, files = server.roles, server.files
    report = check(files["superuser"], roles)
    assert report["server"]["read_only"] == "on" and report["server"]["ssl"] is False
    assert report["server"]["version"].startswith("17")
    assert {"shared_buffers", "work_mem"} <= set(report["settings"])
    assert report["pg_stat_statements_preloaded"] is False
    assert {r["name"] for r in report["roles"]} >= set(roles)
    owners = {t["name"]: t["owner"] for t in report["partitioned"]}
    calculated = {"calculated_breakdowns", "calculated_snapshot_parts"}
    assert owners == {f"public.{name}": roles.admin for name in PARENTS | calculated}
    kinds = [g["kind"] for g in report["gaps"]]
    assert kinds == [
        "admin_createdb",
        "schema",
        "api_select",
        "runtime_write",
        "default_privileges",
        "admin_membership",
        "partition_owner",
    ]
    gaps = {g["kind"]: g for g in report["gaps"]}
    runtime, api = f'"{roles.runtime}"', f'"{roles.api}"'
    assert gaps["schema"]["statements"] == [f'GRANT CREATE ON SCHEMA "public" TO {runtime};']
    assert (
        f"ALTER TABLE public.run_buckets_2026_01_15 OWNER TO {runtime};"
        in (gaps["partition_owner"]["statements"])
    )
    assert f"GRANT SELECT ON public.run_buckets TO {api};" in gaps["api_select"]["statements"]

    # Before the fix: the run-time role can neither partition nor ANALYZE.
    with dbserver.connect(files[roles.runtime]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            partition(conn, "run_buckets", FEB)
    before = analyze(files[roles.runtime])
    assert before["db_server_analyze"] == "failed"
    assert {before["tables"][name]["status"] for name in PARENTS} == {"warning"}
    # The check changed nothing: the same gaps a second time.
    assert check(files["superuser"], roles)["gaps"] == report["gaps"]

    apply(server, report["gaps"])
    assert check(files["superuser"], roles)["gaps"] == []
    with dbserver.connect(files[roles.runtime], autocommit=True) as conn:
        partition(conn, "run_buckets", FEB)
        partition(conn, "test_events", FEB)
        conn.execute("DROP TABLE run_buckets_2026_01_15")  # the archive drops old days
    after = analyze(files[roles.runtime])
    assert after["db_server_analyze"] == "ok", after
    # Calculated v8 creates the snapshot parts, so the optional parent is analyzed too.
    assert after["tables"]["calculated_snapshot_parts"]["status"] == "ok"
    with dbserver.connect(files[roles.admin], autocommit=True) as conn:
        conn.execute("ALTER TABLE run_buckets ADD COLUMN dbcheck_probe integer")
        conn.execute("CREATE TABLE dbcheck_new (x integer)")
    with dbserver.connect(files[roles.runtime], autocommit=True) as conn:
        conn.execute("INSERT INTO dbcheck_new VALUES (1)")
    with dbserver.connect(files[roles.api]) as conn:
        assert conn.execute("SELECT count(*) FROM run_buckets").fetchone() == (0,)
        assert conn.execute("SELECT x FROM dbcheck_new").fetchall() == [(1,)]


def test_analyze_reports_a_lock_timeout_and_a_database_without_the_tables(server, databases):
    files = server.files
    with dbserver.connect(files["superuser"]) as holder:
        holder.execute("LOCK TABLE test_events IN ACCESS EXCLUSIVE MODE")
        line = analyze(files["superuser"], lock_timeout="200ms")
    assert line["db_server_analyze"] == "failed"
    assert line["tables"]["test_events"]["status"] == "failed"
    assert "lock" in line["tables"]["test_events"]["error"]
    assert line["tables"]["run_buckets"]["status"] == "ok"
    credentials = read_credentials(databases(), "postgres") | {"sslmode": "disable"}
    empty = toml(files["superuser"].with_name("empty.toml"), credentials)
    tables = analyze(empty)["tables"]
    assert {tables[name]["status"] for name in PARENTS} == {"missing"}


def test_the_command_line_prints_no_password(server, capsys):
    roles, files = server.roles, server.files
    password = server.superuser["password"]
    flags = ["--runtime-role", roles.runtime, "--api-role", roles.api, "--admin-role", roles.admin]
    command = ["db-server", "check", "--database-file", str(files["superuser"]), *flags]
    assert main(command) == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report["gaps"]) == 7
    assert main([*command, "--sql"]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("-- the admin role cannot create databases (run as superuser)\n")
    assert f'GRANT "{roles.runtime}" TO "{roles.admin}";' in printed.splitlines()
    assert main(["db-server", "analyze", "--database-file", str(files[roles.runtime])]) == 1
    line = capsys.readouterr().out
    assert line.count("\n") == 1 and json.loads(line)["db_server_analyze"] == "failed"
    assert main(["db-server", "analyze", "--database-file", str(files["superuser"])]) == 0
    assert json.loads(capsys.readouterr().out)["db_server_analyze"] == "ok"
    assert main([*command[:4], "--runtime-role", "nobody_dbcheck"]) == 2
    assert "No such role on this server: nobody_dbcheck" in capsys.readouterr().err
    wrong_password = server.superuser | {"password": password[::-1]}
    wrong = toml(files["superuser"].with_name("wrong.toml"), wrong_password)
    assert main(["db-server", "check", "--database-file", str(wrong)]) == 2
    out, err = capsys.readouterr()
    assert "Command failed (OperationalError)" in err
    for file in files.values():
        secret = read_credentials(file, "postgres")["password"]
        assert secret not in out + err + printed + line + json.dumps(report)


def test_a_file_without_sslmode_requires_ssl(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(psycopg, "connect", lambda **options: seen.append(options))
    base = {"host": "db.invalid", "dbname": "d", "user": "u", "password": "p"}
    dbserver.connect(toml(tmp_path / "a.toml", base))
    dbserver.connect(toml(tmp_path / "b.toml", base | {"sslmode": "verify-full"}))
    assert [options["sslmode"] for options in seen] == ["require", "verify-full"]
    with pytest.raises(ValueError, match="Unsupported database connection field"):
        dbserver.connect(toml(tmp_path / "c.toml", base | {"options": "-c x=y"}))
