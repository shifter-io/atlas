"""`db-server check`: the ingest role, the login of the manager's upload path (SEC-INGEST-ROLE-1).

The upload path (orchestrator/api.py, `storage.ingest` of an uploaded run) adds a run: its rows,
the addresses and labels it brings, and the lifetime counters a new run moves. GRANTS is what its
statements need, read from them (storage.py, storage_batch.py, storage_ingest.py, names.py,
verdicts.RECORD) and proven by tests/test_ingest_role.py, which runs the upload path under a role
holding exactly these: SELECT and INSERT where it reads and adds, UPDATE only on the counter
columns (and test_runs.batch_id, which joins a stored run to the batch it is uploaded with),
EXECUTE on the functions `stored_run_summary` calls, USAGE on the schema and TEMPORARY on the
database (its temporary tables). Nothing else: no DELETE, TRUNCATE or DDL, no other table. A
missing day's partition is made by the run-time role (storage.ingest's partitions_file).

Gaps: `ingest_login` (the role does not exist), `ingest_excess` (any other right on a table,
view or partition of the schema, CREATE on the schema or database, role attributes and
memberships), then `ingest_grant` (what GRANTS lacks). TEMPORARY is limits.temporary_gaps'.
scripts/sql/ingest-role.sql creates the role and runs the same grants, for the operator.
"""

from __future__ import annotations

from psycopg import sql

from . import SCHEMA

READ, ADD = ("SELECT",), ("SELECT", "INSERT")
# Per table or view: (table privileges, columns it may UPDATE).
GRANTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "asn_names": (ADD, ()),
    "country_verdicts": (ADD, ()),
    "dbip_releases": (ADD, ()),
    "enrichment_locations": (ADD, ()),
    "enrichment_networks": (ADD, ()),
    "ip_addresses": (ADD, ()),
    "ip_enrichment": (("INSERT",), ()),
    "ip_enrichment_fields": (READ, ()),
    "ip_observations": (("INSERT",), ()),
    "place_names": (ADD, ()),
    "pool_ips": (ADD, ()),
    "pool_stats": (ADD, ("unique_ips",)),
    "product_ips": (ADD, ("first_seen", "last_seen", "observation_count", "run_count")),
    "products": (ADD, ()),
    "provider_overlap": (ADD, ("shared_ips", "updated_at")),
    "providers": (("INSERT",), ()),
    "run_buckets": (ADD, ()),
    "run_enrichments": (ADD, ()),
    "run_geo_buckets": (("INSERT",), ()),
    "schema_versions": (READ, ()),
    "scope_details": (READ, ()),
    "scope_ips": (ADD, ()),
    "scope_stats": (ADD, ("metrics", "histogram_steps", "histogram_counts", "updated_at")),
    "targets": (("INSERT",), ()),
    "test_runs": (ADD, ("batch_id",)),
}
FUNCTIONS = ("artifact_time", "histogram", "histogram_bins", "run_bucket_counters")
FUNCTIONS += ("stored_run_summary",)
COLUMN_LEVEL = ["SELECT", "INSERT", "UPDATE", "REFERENCES"]
TABLE_LEVEL = ["DELETE", "TRUNCATE", "TRIGGER", "MAINTAIN"]

# Per table, view or partition: what the role holds at all (`held`, a column's included), on
# the whole table (`whole`), and the columns it may UPDATE.
TABLES = """
SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname) AS name, c.relname::text AS short,
       ARRAY(SELECT p FROM unnest(%(columns)s::text[]) p
             WHERE has_any_column_privilege(%(ingest)s::name, c.oid, p)
             UNION ALL SELECT p FROM unnest(%(tables)s::text[]) p
             WHERE has_table_privilege(%(ingest)s::name, c.oid, p)) AS held,
       ARRAY(SELECT p FROM unnest(%(columns)s::text[]) p
             WHERE has_table_privilege(%(ingest)s::name, c.oid, p)) AS whole,
       ARRAY(SELECT a.attname::text FROM pg_attribute a
             WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
               AND has_column_privilege(%(ingest)s::name, c.oid, a.attnum, 'UPDATE')
             ORDER BY a.attnum) AS updates
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %(schema)s::name AND c.relkind IN ('r', 'p', 'v', 'm')
ORDER BY c.relname"""
# USAGE and EXECUTE count only granted to the role itself: PUBLIC's may be revoked (#1013).
ACCESS = """
SELECT r.rolsuper AS superuser, r.rolcreaterole AS createrole, r.rolcreatedb AS createdb,
       r.rolreplication AS replication, r.rolbypassrls AS bypassrls,
       ARRAY(SELECT g.rolname::text FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
             WHERE m.member = r.oid ORDER BY 1) AS member_of,
       EXISTS (SELECT 1 FROM pg_namespace n
               CROSS JOIN LATERAL aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a
               WHERE n.nspname = %(schema)s::name AND a.grantee = r.oid
                 AND a.privilege_type = 'USAGE') AS usage,
       has_schema_privilege(r.oid, %(schema)s::text, 'CREATE') AS schema_create,
       has_database_privilege(r.oid, current_database(), 'CREATE') AS database_create,
       ARRAY(SELECT quote_ident(n.nspname) || '.' || quote_ident(p.proname) || '('
                    || pg_get_function_identity_arguments(p.oid) || ')'
             FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = %(schema)s::name AND p.proname = ANY(%(functions)s::name[])
               AND NOT EXISTS (
                 SELECT 1 FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
                 WHERE a.grantee = r.oid AND a.privilege_type = 'EXECUTE')
             ORDER BY 1) AS execute_missing
FROM pg_roles r WHERE r.rolname = %(ingest)s::name"""
ATTRIBUTES = ("superuser", "createrole", "createdb", "replication", "bypassrls")


def collect(cur, names: dict) -> dict | None:
    """The ingest role's rights in the schema; None when the role does not exist."""
    names = names | {"columns": COLUMN_LEVEL, "tables": TABLE_LEVEL, "functions": list(FUNCTIONS)}
    access = cur.execute(ACCESS, names).fetchone()
    if access is None:
        return None
    return access | {"tables": cur.execute(TABLES, names).fetchall()}


def gaps(report, role: str):
    """The role's gaps, in the order their statements run: made, stripped, then granted."""
    ingest, found = sql.Identifier(role), report["ingest"]
    if found is None:
        create = sql.SQL("CREATE ROLE {} LOGIN NOINHERIT").format(ingest)
        detail = "the ingest login does not exist (set its password with \\password after)"
        yield "ingest_login", detail, "superuser", [create]
        return
    detail = "the ingest role holds rights beyond adding runs"
    yield "ingest_excess", detail, "admin", list(_excess(found, ingest, report))
    detail = "the ingest role's attributes and memberships"
    yield "ingest_role", detail, "superuser", list(_attributes(found, ingest))
    detail = "the ingest role lacks what the upload path's import needs"
    yield "ingest_grant", detail, "admin", list(_grants(found, ingest))


def _excess(found, ingest, report):
    database = sql.Identifier(report["server"]["database"])
    if found["schema_create"]:
        yield sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(sql.Identifier(SCHEMA), ingest)
    if found["database_create"]:
        yield sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(database, ingest)
    for row in found["tables"]:
        privileges, columns = GRANTS.get(row["short"], ((), ()))
        table = sql.SQL(row["name"])
        # A table-wide UPDATE goes whole (ingest_grant gives the columns back); else the
        # columns beyond the counters'.
        whole = "UPDATE" in row["whole"] or ("UPDATE" in row["held"] and not columns)
        extra = [p for p in row["held"] if p not in privileges and (p != "UPDATE" or whole)]
        if extra:
            yield sql.SQL("REVOKE {} ON {} FROM {}").format(_list(extra), table, ingest)
        stray = [] if whole else [c for c in row["updates"] if c not in columns]
        if stray:
            revoke = sql.SQL("REVOKE UPDATE ({}) ON {} FROM {}")
            yield revoke.format(_columns(stray), table, ingest)


def _attributes(found, ingest):
    lifted = [name for name in ATTRIBUTES if found[name]]
    if lifted:
        attributes = sql.SQL(" ").join(sql.SQL("NO" + name.upper()) for name in lifted)
        yield sql.SQL("ALTER ROLE {} {}").format(ingest, attributes)
    for group in found["member_of"]:
        yield sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(group), ingest)


def _grants(found, ingest):
    if not found["usage"]:
        yield sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(SCHEMA), ingest)
    for row in found["tables"]:
        privileges, columns = GRANTS.get(row["short"], ((), ()))
        table = sql.SQL(row["name"])
        lacking = [p for p in privileges if p not in row["whole"]]
        if lacking:
            yield sql.SQL("GRANT {} ON {} TO {}").format(_list(lacking), table, ingest)
        held = [] if "UPDATE" in row["whole"] else row["updates"]
        lacking = [c for c in columns if c not in held]
        if lacking:
            yield sql.SQL("GRANT UPDATE ({}) ON {} TO {}").format(_columns(lacking), table, ingest)
    for function in found["execute_missing"]:
        yield sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(sql.SQL(function), ingest)


def _list(privileges):
    return sql.SQL(", ").join(map(sql.SQL, privileges))


def _columns(columns):
    return sql.SQL(", ").join(map(sql.Identifier, columns))
