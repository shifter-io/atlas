"""`db-server check`: what the API role may not do, and the database rights PUBLIC must not keep.

- the API role reads exactly the framework tables and views of `api_tables.txt` (the API's
  statements) and schema `site`: any other table, view or partition in the schema it can read is
  the gap `api_excess`, and a default privilege that gives it SELECT on new tables there is
  `api_defaults` (a table the API starts reading is added to the list instead);
- its role carries its limits, so they hold whatever its client asks for (`api_role`);
- TEMPORARY on the database is held by the run-time, admin and ingest roles themselves (ingest
  and the migrations make temporary tables) and revoked from PUBLIC (`temporary`,
  `temporary_public`);
- PUBLIC cannot create large objects (`large_objects`);
- PUBLIC cannot connect to the server's other databases: every other login keeps CONNECT where
  it has it, so the API role alone loses it (`connect`). A database whose objects or grants
  name the API role is the API role's own, and is left alone. A login made later needs
  `GRANT CONNECT` on those databases.
"""

from __future__ import annotations

from pathlib import Path

from psycopg import sql

from . import SCHEMA

API_TABLES_FILE = Path(__file__).with_name("api_tables.txt")
CONNECTION_LIMIT = 20
API_SETTINGS = {
    "default_transaction_read_only": "on",
    "statement_timeout": "30s",
    "idle_in_transaction_session_timeout": "60s",
    "temp_file_limit": "1GB",
}
LARGE_OBJECT_MAKERS = ["lo_create", "lo_creat", "lo_from_bytea", "lo_import"]

# A partition of a listed parent is read through it and left alone.
EXCESS = """
SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname) AS name
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_class r ON r.oid = coalesce(pg_partition_root(c.oid), c.oid)
JOIN pg_namespace rn ON rn.oid = r.relnamespace
WHERE n.nspname = %(schema)s::name AND c.relkind IN ('r', 'p', 'v', 'm')
  AND has_table_privilege(%(api)s::name, c.oid, 'SELECT')
  AND quote_ident(rn.nspname) || '.' || quote_ident(r.relname) <> ALL(%(api_tables)s::text[])
ORDER BY 1"""
DEFAULTS = """
SELECT DISTINCT pg_get_userbyid(d.defaclrole) AS creator, d.defaclnamespace <> 0 AS in_schema
FROM pg_default_acl d CROSS JOIN LATERAL aclexplode(d.defaclacl) a
WHERE d.defaclobjtype = 'r' AND a.privilege_type = 'SELECT'
  AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = %(api)s::name)
  AND d.defaclnamespace IN (0, (SELECT oid FROM pg_namespace WHERE nspname = %(schema)s::name))
ORDER BY 1, 2"""
ROLE = """
SELECT rolconnlimit AS connection_limit, coalesce(rolconfig, '{}') AS config
FROM pg_roles WHERE rolname = %(api)s::name"""
# Who holds TEMPORARY other than through PUBLIC (a database owner always does).
TEMPORARY = """
WITH d AS (SELECT datdba, coalesce(datacl, acldefault('d', datdba)) AS acl
           FROM pg_database WHERE datname = current_database())
SELECT EXISTS (SELECT 1 FROM aclexplode(d.acl) a
               WHERE a.grantee = 0 AND a.privilege_type = 'TEMPORARY') AS public,
       EXISTS (SELECT 1 FROM aclexplode(d.acl) a JOIN pg_roles g ON g.oid = a.grantee
               WHERE g.rolname = %(api)s::name AND a.privilege_type = 'TEMPORARY') AS api,
       ARRAY(SELECT w FROM unnest(%(temporary)s::text[]) w
             WHERE NOT EXISTS (SELECT 1 FROM aclexplode(d.acl) a
                               WHERE a.grantee <> 0 AND a.privilege_type = 'TEMPORARY'
                                 AND pg_has_role(w::name, a.grantee, 'USAGE'))) AS missing,
       pg_has_role(%(admin)s::name, d.datdba, 'USAGE') AS admin_owns_database
FROM d"""
LARGE_OBJECTS = """
SELECT format('pg_catalog.%%I(%%s)', p.proname, pg_get_function_identity_arguments(p.oid)) AS name
FROM pg_proc p
WHERE p.pronamespace = 'pg_catalog'::regnamespace AND p.proname = ANY(%(makers)s::name[])
  AND has_function_privilege(%(api)s::name, p.oid, 'EXECUTE')
ORDER BY 1"""
# `logins`: the other logins that connect only through PUBLIC (superusers need nothing).
CONNECT = """
WITH api AS (SELECT oid FROM pg_roles WHERE rolname = %(api)s::name)
SELECT quote_ident(d.datname) AS name,
       EXISTS (SELECT 1 FROM aclexplode(acl.acl) a
               WHERE a.grantee = 0 AND a.privilege_type = 'CONNECT') AS public,
       EXISTS (SELECT 1 FROM aclexplode(acl.acl) a
               WHERE a.grantee = api.oid AND a.privilege_type = 'CONNECT') AS api,
       ARRAY(SELECT quote_ident(r.rolname) FROM pg_roles r
             WHERE r.rolcanlogin AND NOT r.rolsuper AND r.oid <> api.oid AND NOT EXISTS (
               SELECT 1 FROM aclexplode(acl.acl) a
               WHERE a.grantee <> 0 AND a.privilege_type = 'CONNECT'
                 AND pg_has_role(r.oid, a.grantee, 'USAGE'))
             ORDER BY 1) AS logins
FROM pg_database d CROSS JOIN api
CROSS JOIN LATERAL (SELECT coalesce(d.datacl, acldefault('d', d.datdba)) AS acl) acl
WHERE d.datallowconn AND d.datname <> current_database()
  AND has_database_privilege(api.oid, d.oid, 'CONNECT')
  AND NOT EXISTS (SELECT 1 FROM pg_shdepend s WHERE s.dbid = d.oid
                    AND s.refclassid = 'pg_authid'::regclass AND s.refobjid = api.oid)
ORDER BY d.datname"""


def api_tables() -> list[str]:
    """The schema-qualified tables and views the API reads (api_tables.txt)."""
    lines = (line.strip() for line in API_TABLES_FILE.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def collect(cur, names: dict) -> dict:
    """What the API role and PUBLIC may do beyond what they should."""
    names = names | {"makers": LARGE_OBJECT_MAKERS}
    return {
        "api_tables": names["api_tables"],
        "excess": [r["name"] for r in cur.execute(EXCESS, names).fetchall()],
        "defaults": cur.execute(DEFAULTS, names).fetchall(),
        "role": cur.execute(ROLE, names).fetchone(),
        "temporary": cur.execute(TEMPORARY, names).fetchone(),
        "large_objects": [r["name"] for r in cur.execute(LARGE_OBJECTS, names).fetchall()],
        "connect": cur.execute(CONNECT, names).fetchall(),
    }


def temporary_gaps(report, api):
    """TEMPORARY held by the run-time and admin roles themselves, then revoked from PUBLIC."""
    lacks, database = report["limits"]["temporary"], sql.Identifier(report["server"]["database"])
    grant = sql.SQL("GRANT TEMPORARY ON DATABASE {} TO {}")
    roles = sql.SQL(", ").join(map(sql.Identifier, lacks["missing"]))
    run_as = "admin" if report["access"]["admin_grants_temporary"] else "superuser"
    detail = "the run-time, admin and ingest roles must hold TEMPORARY for their temporary tables"
    yield "temporary", detail, run_as, [grant.format(database, roles)] if lacks["missing"] else []
    revoke = sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM {}")
    statements = [
        revoke.format(database, grantee)
        for flag, grantee in (("public", sql.SQL("PUBLIC")), ("api", api))
        if lacks[flag]
    ]
    run_as = "admin" if lacks["admin_owns_database"] else "superuser"
    detail = "PUBLIC or the API role can create temporary tables"
    yield "temporary_public", detail, run_as, statements


def excess_gap(report, api):
    detail = "tables and views the API role can read but does not use (api_tables.txt)"
    revoke = sql.SQL("REVOKE SELECT ON {} FROM {}")
    statements = [revoke.format(sql.SQL(name), api) for name in report["limits"]["excess"]]
    yield "api_excess", detail, "admin", statements


def defaults_gap(report, api):
    """Default privileges that would give the API role SELECT on new tables in the schema."""
    defaults = report["limits"]["defaults"]
    schema = sql.SQL(" IN SCHEMA {}").format(sql.Identifier(SCHEMA))
    revoke = sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {}{} REVOKE SELECT ON TABLES FROM {}")
    statements = [
        revoke.format(sql.Identifier(d["creator"]), schema if d["in_schema"] else sql.SQL(""), api)
        for d in defaults
    ]
    admin_only = {d["creator"] for d in defaults} <= {report["checked_roles"]["admin"]}
    run_as = "admin" if admin_only else "superuser"
    yield "api_defaults", "new tables would be readable by the API role", run_as, statements


def role_gaps(report, api):
    """The API role's limits, large objects, and CONNECT on the other databases."""
    limits = report["limits"]
    role, alter = limits["role"], sql.SQL("ALTER ROLE {} ").format(api)
    statements = []
    if not 0 < role["connection_limit"] <= CONNECTION_LIMIT:
        statements.append(alter + sql.SQL("CONNECTION LIMIT {}").format(CONNECTION_LIMIT))
    statements += [
        alter + sql.SQL("SET {} = {}").format(sql.SQL(name), sql.Literal(value))
        for name, value in API_SETTINGS.items()
        if f"{name}={value}" not in role["config"]
    ]
    yield "api_role", "the API role's own limits", "superuser", statements
    revoke = sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM PUBLIC")
    statements = [revoke.format(sql.SQL(name)) for name in limits["large_objects"]]
    yield "large_objects", "the API role can create large objects", "superuser", statements
    statements = [s for database in limits["connect"] for s in _connect(database, api)]
    detail = "the API role can connect to the server's other databases"
    yield "connect", detail, "superuser", statements


def _connect(database, api):
    name = sql.SQL(database["name"])
    if database["public"] and database["logins"]:
        logins = sql.SQL(", ").join(map(sql.SQL, database["logins"]))
        yield sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(name, logins)
    if database["public"]:
        yield sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(name)
    if database["api"]:
        yield sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(name, api)
