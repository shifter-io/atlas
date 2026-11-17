"""`db-server check`: the run-time role seeds the site catalog's targets (SEED-ON-DEPLOY-1).

The deploy (scripts/server-setup.sh) runs `atlas db-seed --only targets` with the manager's login,
the run-time role, before the API switch. It writes the rows of site.targets that are missing or
differ (storage_catalog), so it needs SELECT, INSERT and UPDATE on that table, and UPDATE on
site.revision, which the table's statement trigger bumps as the writer. Schema site stays the
admin role's (`siteapi migrate`), and the role reads the rest of it (`site_read`).
Gap `site_targets_write`, run by the admin role, only once site.targets exists.
"""

from __future__ import annotations

from psycopg import sql

WANTED = {"targets": ("SELECT", "INSERT", "UPDATE"), "revision": ("UPDATE",)}
LACKS = """
SELECT ARRAY(SELECT w FROM unnest(%(privileges)s::text[]) w
             WHERE NOT has_table_privilege(%(runtime)s::name, t.oid, w)) AS missing
FROM (SELECT to_regclass(%(table)s)::oid AS oid) t WHERE t.oid IS NOT NULL"""


def collect(cur, names: dict) -> dict | None:
    """What the run-time role lacks on each table; None before `siteapi migrate` makes them."""
    lacks = {}
    for table, privileges in WANTED.items():
        params = names | {"privileges": list(privileges), "table": f"site.{table}"}
        row = cur.execute(LACKS, params).fetchone()
        if row is None:
            return None
        lacks[table] = row["missing"]
    return lacks


def gaps(report: dict, runtime):
    """The grants the deploy's seed needs, for each table the role lacks any of them on."""
    statements = []
    for table, missing in (report.get("site_targets") or {}).items():
        if missing:
            privileges = sql.SQL(", ").join(map(sql.SQL, WANTED[table]))
            name = sql.Identifier("site", table)
            statements.append(sql.SQL("GRANT {} ON {} TO {}").format(privileges, name, runtime))
    detail = "the run-time role cannot seed site.targets on deploy"
    yield "site_targets_write", detail, "admin", statements
