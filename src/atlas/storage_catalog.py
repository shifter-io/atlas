"""SEED-ON-DEPLOY-1: the site catalog's targets (site.targets), seeded with the framework's.

Each target's catalog row is its category and sort order from configs/targets.toml and its
difficulty from the methodology ([http_difficulty] and [browser_difficulty], the owner's fixed
values; a group without one takes its hardest source's). api/scripts/catalog-targets.sql holds
the same rows for an operator (tests/test_catalog_targets.py fails when they differ).

`atlas db-seed --only targets` writes the rows that are missing or differ, in one statement, and
nothing when every row matches: site.revision, which every write bumps, then stays as it was, so
a repeated deploy starts no rebuild. A login that may not write site.targets (the run-time role
before the owner's grant, `db-server check`'s `site_targets_write`) leaves them pending.
`--require-catalog` (the deploy, scripts/server-setup.sh) then stops with the fix when a row the
methodology's targets need is pending: a published group must never lack its catalog row.
"""

from __future__ import annotations

from typing import Any

from .errors import UserError

# Every write to site.targets bumps site.revision (its statement trigger runs as the writer).
GRANT = (
    "GRANT SELECT, INSERT, UPDATE ON site.targets TO proxy_reviews; "
    "GRANT UPDATE ON site.revision TO proxy_reviews"
)
FIX = (
    "run api/scripts/catalog-targets.sql as the catalog writer, or have the owner run once "
    f"with the admin login: {GRANT}"
)
COLUMNS = ("category", "difficulty", "sort_order", "is_demo")
EXISTS = "SELECT to_regclass('site.targets') IS NOT NULL"
WRITABLE = (
    "SELECT has_table_privilege('site.targets', 'INSERT')"
    " AND has_table_privilege('site.targets', 'UPDATE')"
    " AND has_table_privilege('site.revision', 'UPDATE')"
)
CURRENT = (
    "SELECT target_id, category, difficulty, sort_order, is_demo FROM site.targets"
    " WHERE target_id = ANY(%s)"
)
UPSERT = """
INSERT INTO site.targets(target_id, category, difficulty, sort_order, is_demo)
SELECT t, c, d, o, false
FROM unnest(%s::text[], %s::text[], %s::smallint[], %s::integer[]) AS v(t, c, d, o)
ON CONFLICT (target_id) DO UPDATE
SET category = EXCLUDED.category, difficulty = EXCLUDED.difficulty,
    sort_order = EXCLUDED.sort_order, is_demo = false"""


def required(config) -> set[str]:
    """Every target the methodology publishes: each group, its sources and each scored target."""
    from .calculated.policy import browser_difficulty, http_difficulty

    groups = config.get("target_groups", {})
    found = set(groups).union(*groups.values())
    return found | set(browser_difficulty(config)) | set(http_difficulty(config))


def wanted(targets: list[dict[str, Any]], config) -> dict[str, tuple]:
    """Each catalogued target's row (category, difficulty, sort_order, is_demo), by id; a
    methodology target without a category or difficulty is a config error."""
    from .calculated.policy import browser_difficulty, http_difficulty

    difficulty = http_difficulty(config) | browser_difficulty(config)
    rows = {}
    for t in targets:
        if t.get("category") is None:
            continue
        if t["target_id"] not in difficulty:
            raise ValueError(f"Target {t['target_id']} has a category but no difficulty")
        rows[t["target_id"]] = (t["category"], difficulty[t["target_id"]], t["sort_order"], False)
    if lacking := sorted(required(config) - set(rows)):
        raise ValueError(f"Methodology targets without a category in targets.toml: {lacking}")
    return rows


def seed(cursor, targets: list[dict[str, Any]], config) -> dict[str, Any] | None:
    """Write the catalog rows that are missing or differ; None without schema site."""
    if not cursor.execute(EXISTS).fetchone()[0]:
        return None
    rows = wanted(targets, config)
    have = {r[0]: tuple(r[1:]) for r in cursor.execute(CURRENT, (list(rows),)).fetchall()}
    changed = [t for t, row in rows.items() if have.get(t) != row]
    if changed and cursor.execute(WRITABLE).fetchone()[0]:
        columns = [[rows[t][i] for t in changed] for i in range(3)]
        cursor.execute(UPSERT, [changed, *columns])
        return {"written": changed, "pending": []}
    return {"written": [], "pending": changed}


def require(result: dict[str, Any] | None, config) -> None:
    """Stop when the catalog lacks schema site or a row the methodology's targets need."""
    if result is None:
        raise UserError("No schema site (run `siteapi migrate` first): the catalog is incomplete")
    if missing := sorted(required(config) & set(result["pending"])):
        raise UserError(f"site.targets lacks or differs for {', '.join(missing)}: {FIX}")
