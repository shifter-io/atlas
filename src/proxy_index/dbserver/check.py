"""`db-server check`: a read-only report on the database server and its privilege gaps.

Everything is read in one READ ONLY transaction, rolled back at the end. Settings are recorded,
not judged (the server's owner tunes it). Gaps are judged against `Roles`:

- the run-time role writes every table and creates, drops and ANALYZEs the run-time
  partitions, so it needs CREATE on the schema and ownership of every partitioned table and of
  its partitions (CREATE TABLE ... PARTITION OF needs the parent's owner);
- the API role reads every framework table and view;
- both run every function in the schema (EXECUTE): calculations and ingest call them, and a
  view's functions are checked against the role reading the view;
- the admin role runs migrations, so new tables and functions get default privileges for both
  roles (with no global entry for its functions, PostgreSQL's built-in default gives EXECUTE
  to PUBLIC), it keeps acting on the tables the run-time role owns through membership in that
  role, and it needs CREATEDB for scratch databases.

Each gap carries its statements in the order they must run and who runs them: `admin` (the
admin config) or `superuser` (role attributes and memberships the admin role cannot grant
itself).
"""

from __future__ import annotations

from psycopg import sql
from psycopg.rows import dict_row

from ..errors import UserError
from . import SCHEMA, Roles, connect

NAMED_SETTINGS = [
    "autovacuum",
    "default_statistics_target",
    "effective_cache_size",
    "jit",
    "maintenance_work_mem",
    "max_connections",
    "max_parallel_workers_per_gather",
    "max_wal_size",
    "random_page_cost",
    "shared_buffers",
    "shared_preload_libraries",
    "ssl",
    "work_mem",
]
DEFAULT_ROLES = Roles()
WRITE = ["SELECT", "INSERT", "UPDATE", "DELETE"]
LARGEST = 10

SERVER = """
SELECT current_setting('server_version') AS version, current_database() AS database,
       pg_get_userbyid(d.datdba) AS database_owner, current_user AS connected_as,
       current_setting('transaction_read_only') AS read_only, coalesce(s.ssl, false) AS ssl,
       s.version AS ssl_version, s.cipher AS ssl_cipher, pg_database_size(d.oid) AS bytes
FROM pg_database d LEFT JOIN pg_stat_ssl s ON s.pid = pg_backend_pid()
WHERE d.datname = current_database()"""
# Settings a session or client changed (this check's own statement timeout too) are left out.
SETTINGS = """
SELECT name, setting, unit, source FROM pg_settings
WHERE source NOT IN ('default', 'client', 'session', 'override') OR name = ANY(%s)
ORDER BY name"""
EXTENSIONS = """
SELECT name, installed_version, default_version FROM pg_available_extensions
WHERE installed_version IS NOT NULL OR name = 'pg_stat_statements' ORDER BY name"""
# A partition's size counts toward its root table.
TABLES = """
SELECT coalesce(pg_partition_root(c.oid), c.oid::regclass)::text AS name,
       sum(pg_total_relation_size(c.oid))::bigint AS bytes,
       count(*) FILTER (WHERE c.relispartition) AS partitions
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'm') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT %s"""
ROLES = """
SELECT r.rolname::text AS name, r.rolsuper AS superuser, r.rolinherit AS inherit,
       r.rolcreaterole AS createrole, r.rolcreatedb AS createdb, r.rolcanlogin AS login,
       r.rolbypassrls AS bypassrls, r.rolconnlimit AS connection_limit,
       ARRAY(SELECT g.rolname::text FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
             WHERE m.member = r.oid ORDER BY 1) AS member_of
FROM pg_roles r WHERE r.rolname !~ '^pg_' ORDER BY 1"""
# Names are schema-qualified and quoted, ready for the statements.
PARTITIONED = """
SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname) AS name,
       pg_get_userbyid(c.relowner) AS owner,
       pg_has_role(%(runtime)s::name, c.relowner, 'USAGE') AS runtime_owns,
       has_table_privilege(%(runtime)s::name, c.oid, 'MAINTAIN')
         OR pg_has_role(%(runtime)s::name, d.datdba, 'USAGE') AS runtime_may_analyze,
       ARRAY(SELECT quote_ident(pn.nspname) || '.' || quote_ident(p.relname)
             FROM pg_inherits i JOIN pg_class p ON p.oid = i.inhrelid
             JOIN pg_namespace pn ON pn.oid = p.relnamespace
             WHERE i.inhparent = c.oid AND NOT pg_has_role(%(runtime)s::name, p.relowner, 'USAGE')
             ORDER BY 1) AS partitions_not_owned
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_database d ON d.datname = current_database()
WHERE c.relkind = 'p' AND NOT c.relispartition AND n.nspname = %(schema)s::name
ORDER BY c.relname"""
ACCESS = """
SELECT has_schema_privilege(%(runtime)s::name, %(schema)s::text, 'USAGE') AS runtime_usage,
       has_schema_privilege(%(runtime)s::name, %(schema)s::text, 'CREATE') AS runtime_create,
       has_schema_privilege(%(api)s::name, %(schema)s::text, 'USAGE') AS api_usage,
       pg_has_role(%(admin)s::name, %(runtime)s::name, 'USAGE')
         AND pg_has_role(%(admin)s::name, %(runtime)s::name, 'SET') AS admin_acts_as_runtime"""
# Partitions are read through their parent, so only the parents are checked.
OBJECTS = """
SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname) AS name,
       has_table_privilege(%(api)s::name, c.oid, 'SELECT') AS api_select,
       ARRAY(SELECT w FROM unnest(CASE WHEN c.relkind IN ('r', 'p') THEN %(write)s::text[]
                                       ELSE ARRAY['SELECT'] END) w
             WHERE NOT has_table_privilege(%(runtime)s::name, c.oid, w)) AS runtime_missing
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %(schema)s::name AND c.relkind IN ('r', 'p', 'v', 'm') AND NOT c.relispartition
ORDER BY c.relname"""
DEFAULTS = """
SELECT pg_get_userbyid(d.defaclrole) AS creator, coalesce(n.nspname, '*') AS schema,
       d.defaclobjtype::text AS type,
       CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END AS grantee,
       a.privilege_type AS privilege
FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
CROSS JOIN LATERAL aclexplode(d.defaclacl) a ORDER BY 1, 2, 3, 4, 5"""
# Functions of an extension are the extension's; their signatures are ready for the statements.
FUNCTIONS = """
SELECT quote_ident(n.nspname) || '.' || quote_ident(p.proname) AS name,
       pg_get_function_identity_arguments(p.oid) AS arguments,
       ARRAY(SELECT g FROM unnest(ARRAY[%(runtime)s, %(api)s]::text[]) g
             WHERE NOT has_function_privilege(g::name, p.oid, 'EXECUTE')) AS missing
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = %(schema)s::name AND p.prokind = 'f' AND NOT EXISTS (
  SELECT 1 FROM pg_depend e
  WHERE e.classid = 'pg_proc'::regclass AND e.objid = p.oid AND e.deptype = 'e')
ORDER BY p.proname, 2"""
# Privileges on the admin role's new objects of `type` that `grantee` would not get (directly or
# by role). A global entry replaces the built-in default (`acldefault`: PUBLIC runs functions);
# a schema entry adds to whichever of the two applies.
DEFAULTS_MISSING = """
WITH admin AS (SELECT oid FROM pg_roles WHERE rolname = %(admin)s::name),
acl AS (
  SELECT coalesce((SELECT d.defaclacl FROM pg_default_acl d
                   WHERE d.defaclrole = admin.oid AND d.defaclobjtype = %(type)s::"char"
                     AND d.defaclnamespace = 0),
                  acldefault(%(type)s::"char", admin.oid)) AS acl FROM admin
  UNION ALL
  SELECT d.defaclacl FROM pg_default_acl d JOIN admin ON d.defaclrole = admin.oid
  WHERE d.defaclobjtype = %(type)s::"char"
    AND d.defaclnamespace = (SELECT oid FROM pg_namespace WHERE nspname = %(schema)s::name))
SELECT w AS privilege FROM unnest(%(privileges)s::text[]) w WHERE NOT EXISTS (
  SELECT 1 FROM acl CROSS JOIN LATERAL aclexplode(acl.acl) a
  WHERE a.privilege_type = w
    AND (a.grantee = 0 OR pg_has_role(%(grantee)s::name, a.grantee, 'USAGE')))"""
# What each role needs on the admin role's new objects: (type in pg_default_acl, privileges).
DEFAULTS_WANTED = {
    "TABLES": ("r", {"api": ["SELECT"], "runtime": WRITE}),
    "FUNCTIONS": ("f", {"api": ["EXECUTE"], "runtime": ["EXECUTE"]}),
}


def check(credentials_file, roles: Roles = DEFAULT_ROLES) -> dict:
    """The report: server facts, the judged privileges and `gaps` (empty when nothing lacks)."""
    with connect(credentials_file) as conn:
        conn.read_only = True
        try:
            report = _collect(conn.cursor(row_factory=dict_row), roles)
            report["gaps"] = _gaps(conn, report, roles)
        finally:
            conn.rollback()
    return report


def _collect(cur, roles: Roles) -> dict:
    settings = {r.pop("name"): r for r in cur.execute(SETTINGS, (NAMED_SETTINGS,)).fetchall()}
    cur.execute("SET LOCAL statement_timeout = '60s'")
    role_rows = cur.execute(ROLES).fetchall()
    missing = set(roles) - {r["name"] for r in role_rows}
    if missing:
        raise UserError(f"No such role on this server: {', '.join(sorted(missing))}")
    names = roles._asdict() | {"schema": SCHEMA, "write": WRITE}
    preload = settings.get("shared_preload_libraries")
    return {
        "server": cur.execute(SERVER).fetchone(),
        "settings": settings,
        "extensions": cur.execute(EXTENSIONS).fetchall(),
        "pg_stat_statements_preloaded": (
            None if preload is None else "pg_stat_statements" in preload["setting"]
        ),
        "largest_tables": cur.execute(TABLES, (LARGEST,)).fetchall(),
        "roles": role_rows,
        "checked_roles": roles._asdict(),
        "access": cur.execute(ACCESS, names).fetchone(),
        "partitioned": cur.execute(PARTITIONED, names).fetchall(),
        "objects": [r for r in cur.execute(OBJECTS, names).fetchall() if _lacks(r)],
        "functions": [r for r in cur.execute(FUNCTIONS, names).fetchall() if r["missing"]],
        "default_privileges": cur.execute(DEFAULTS).fetchall(),
        "default_privileges_missing": _defaults_missing(cur, names),
    }


def _defaults_missing(cur, names: dict) -> dict:
    """Per kind of object (TABLES, FUNCTIONS) and role, what the admin role's new ones lack."""
    return {
        objects: {
            role: [
                r["privilege"]
                for r in cur.execute(
                    DEFAULTS_MISSING,
                    names | {"type": kind, "grantee": names[role], "privileges": privileges},
                ).fetchall()
            ]
            for role, privileges in wanted.items()
        }
        for objects, (kind, wanted) in DEFAULTS_WANTED.items()
    }


def _lacks(row) -> bool:
    return not row["api_select"] or bool(row["runtime_missing"])


def _gaps(conn, report: dict, roles: Roles) -> list[dict]:
    """The gaps, in the order their statements must run."""
    runtime, api, admin = (sql.Identifier(name) for name in roles)
    admin_role = {r["name"]: r for r in report["roles"]}[roles.admin]
    found = [
        *_role_gaps(report, admin_role, runtime, api, admin),
        *_grant_gaps(report, runtime, api),
        *_default_gaps(report, runtime, api, admin),
        *_partition_gaps(report, admin_role["superuser"], runtime, admin),
    ]
    return [
        {"kind": kind, "detail": detail, "run_as": run_as}
        | {"statements": [s.as_string(conn) + ";" for s in statements]}
        for kind, detail, run_as, statements in found
        if statements
    ]


def _role_gaps(report, admin_role, runtime, api, admin):
    if not (admin_role["createdb"] or admin_role["superuser"]):
        statement = sql.SQL("ALTER ROLE {} CREATEDB").format(admin)
        yield "admin_createdb", "the admin role cannot create databases", "superuser", [statement]
    grant = sql.SQL("GRANT {} ON SCHEMA {} TO {}")
    wanted = (
        ("runtime_usage", "USAGE", runtime),
        ("runtime_create", "CREATE", runtime),
        ("api_usage", "USAGE", api),
    )
    yield (
        "schema",
        f"privileges on schema {SCHEMA}",
        "admin",
        [
            grant.format(sql.SQL(privilege), sql.Identifier(SCHEMA), role)
            for flag, privilege, role in wanted
            if not report["access"][flag]
        ],
    )


def _grant_gaps(report, runtime, api):
    objects = report["objects"]
    yield (
        "api_select",
        "framework tables and views the API role cannot read",
        "admin",
        [
            sql.SQL("GRANT SELECT ON {} TO {}").format(sql.SQL(o["name"]), api)
            for o in objects
            if not o["api_select"]
        ],
    )
    yield (
        "runtime_write",
        "framework tables the run-time role cannot write",
        "admin",
        [
            sql.SQL("GRANT {} ON {} TO {}").format(
                _list(o["runtime_missing"]), sql.SQL(o["name"]), runtime
            )
            for o in objects
            if o["runtime_missing"]
        ],
    )
    yield (
        "execute",
        "framework functions the run-time or API role cannot run",
        "admin",
        [
            sql.SQL("GRANT EXECUTE ON FUNCTION {}({}) TO {}").format(
                sql.SQL(f["name"]),
                sql.SQL(f["arguments"]),
                sql.SQL(", ").join(map(sql.Identifier, f["missing"])),
            )
            for f in report["functions"]
        ],
    )


def _default_gaps(report, runtime, api, admin):
    defaults = sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT {} ON {} TO {}")
    grantees = {"api": api, "runtime": runtime}
    yield (
        "default_privileges",
        "privileges the admin role's new tables and functions would not give",
        "admin",
        [
            defaults.format(
                admin, sql.Identifier(SCHEMA), _list(missing), sql.SQL(objects), grantees[role]
            )
            for objects, by_role in report["default_privileges_missing"].items()
            for role, missing in by_role.items()
            if missing
        ],
    )


def _list(privileges):
    return sql.SQL(", ").join(map(sql.SQL, privileges))


def _partition_gaps(report, admin_is_superuser, runtime, admin):
    """Ownership of the partitioned parents and their partitions, then ANALYZE rights."""
    tables = report["partitioned"]
    changes = [
        sql.SQL("ALTER TABLE {} OWNER TO {}").format(sql.SQL(name), runtime)
        for t in tables
        for name in ([] if t["runtime_owns"] else [t["name"]]) + t["partitions_not_owned"]
    ]
    if changes and not (admin_is_superuser or report["access"]["admin_acts_as_runtime"]):
        statement = sql.SQL("GRANT {} TO {}").format(runtime, admin)
        detail = "the admin role must keep acting on the tables the run-time role will own"
        yield "admin_membership", detail, "superuser", [statement]
    detail = "the run-time role must own the partitioned tables and their partitions"
    yield "partition_owner", detail, "admin", changes
    yield (
        "analyze",
        "the run-time role cannot ANALYZE these partitioned tables",
        "admin",
        [
            sql.SQL("GRANT MAINTAIN ON TABLE {} TO {}").format(sql.SQL(t["name"]), runtime)
            for t in tables
            if t["runtime_owns"] and not t["runtime_may_analyze"]
        ],
    )
