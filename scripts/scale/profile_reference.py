"""SIM-PROFILE-1: the `reference` section, the real runs' sizes in a database (SIMULATE.md).

Everything is read in one read-only, repeatable-read transaction. A run's kind is `daily` (its
summary has test_kind daily) or `browser` (its batch's kind); other runs are left out. Rows per
run: the run's own rows of the tables keyed by run_key; of the shared tables (ip_addresses,
product_ips, pool_ips, ip_enrichment) the rows its observations reference; test_events: the
events of its batch and product.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime

from profile_browser import spread
from profile_hours import ProfileError, share
from psycopg import IsolationLevel, sql

from proxy_index.browser.engines import DEFAULT_ENGINE
from proxy_index.calculated.browser import CODES, COUNTERS, OUTCOMES
from proxy_index.stats import Histogram
from proxy_index.storage import connect

TABLES = ("test_runs", "run_buckets", "run_geo_buckets", "ip_observations", "run_enrichments")
TABLES += ("ip_enrichment", "ip_addresses", "product_ips", "pool_ips", "estimated_hours")
TABLES += ("batch_runs", "test_events")
_OWN = "(SELECT count(*) FROM {} x WHERE x.run_key=r.run_key)"
_IPS = "(SELECT o.ip_id FROM ip_observations o WHERE o.run_key=r.run_key)"
COUNTS = {
    "test_runs": "1",
    **{name: _OWN.format(name) for name in ("run_buckets", "run_geo_buckets", "ip_observations")},
    "run_enrichments": _OWN.format("run_enrichments"),
    "ip_enrichment": f"(SELECT count(*) FROM ip_enrichment e WHERE e.ip_id IN {_IPS} AND "
    "e.release_id IN (SELECT x.release_id FROM run_enrichments x WHERE x.run_key=r.run_key))",
    "ip_addresses": "(SELECT count(DISTINCT o.ip_id) FROM ip_observations o "
    "WHERE o.run_key=r.run_key)",
    "product_ips": "(SELECT count(*) FROM product_ips m WHERE m.product_id=r.product_id "
    f"AND m.ip_id IN {_IPS})",
    "pool_ips": f"(SELECT count(*) FROM pool_ips m WHERE m.pool_id=r.pool_id AND m.ip_id IN {_IPS})",
    "estimated_hours": _OWN.format("estimated_hours"),
    "batch_runs": _OWN.format("batch_runs"),
}
RUNS = (
    "WITH r AS (SELECT t.run_key,t.batch_id,s.product_id,s.pool_id,p.provider,"
    "CASE WHEN t.summary->>'test_kind'='daily' THEN 'daily' "
    "WHEN b.kind='browser' THEN 'browser' END AS kind "
    "FROM test_runs t JOIN scope_stats s USING(scope_id) JOIN products p USING(product_id) "
    "LEFT JOIN test_batches b USING(batch_id) WHERE {filter}) "
    "SELECT r.run_key,r.kind,r.provider,r.batch_id,r.product_id,{counts} FROM r "
    "WHERE r.kind IS NOT NULL ORDER BY r.run_key"
)
EVENTS = (
    "SELECT batch_id,product_id,count(*) FROM test_events WHERE batch_id=ANY(%s) GROUP BY 1,2"
)
# Each table with its partitions: its own bytes (with TOAST) and its indexes' bytes.
SIZES = (
    "SELECT c.relname,sum(pg_table_size(coalesce(t.relid,c.oid)))::bigint,"
    "sum(pg_indexes_size(coalesce(t.relid,c.oid)))::bigint FROM pg_class c "
    "JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN LATERAL pg_partition_tree(c.oid) t "
    "ON true WHERE n.nspname=current_schema() AND c.relname=ANY(%s) AND c.relkind IN ('r','p') "
    "AND NOT c.relispartition GROUP BY 1"
)
_JOINS = "FROM run_buckets b JOIN test_runs t USING(run_key) JOIN scope_details d USING(scope_id)"
_KEYS = "WHERE b.run_key=ANY(%(keys)s)"
_ENGINE = "coalesce(d.scope->'config'->>'browser_engine',%(engine)s)"
_CODES = ",".join(f"sum(coalesce((b.errors->>'{code}')::bigint,0))::bigint" for code in CODES)
_COUNTERS = ",".join(f"sum(b.{column})::bigint" for column in COUNTERS.values())
OUTCOME_SUMS = (
    f"SELECT d.provider,d.target,{_ENGINE},count(DISTINCT b.run_key),sum(b.completed)::bigint,"
    f"sum(b.successes)::bigint,{_COUNTERS},{_CODES} {_JOINS} {_KEYS} GROUP BY 1,2,3"
)
# A bucket's histogram holds its successful tries only (calculated.browser.page_load).
LOADS = (
    f"SELECT d.provider,d.target,{_ENGINE},h.ms,sum(h.n)::bigint {_JOINS} "
    f"CROSS JOIN LATERAL histogram(b.histogram_steps,b.histogram_counts) h {_KEYS} "
    "GROUP BY 1,2,3,4"
)
BYTES_BASIS = (
    "each table's bytes and its indexes' bytes (all partitions) over its rows (count(*)), times "
    "the kind's mean rows per run"
)
_RANGE = re.compile(r"^(\d+)(?:-(\d+))?$")


def run_filter(text: str) -> tuple[sql.Composable, dict]:
    """`marker:<summary key>` or `batches:<id>[-<id>][,...]` as a WHERE condition on t."""
    kind, _, value = text.partition(":")
    if kind == "marker" and value:
        return sql.SQL("t.summary ? %(marker)s"), {"marker": value}
    if kind == "batches" and value:
        ids = []
        for part in value.split(","):
            found = _RANGE.match(part.strip())
            if not found:
                raise ProfileError(f"--runs {text!r}: {part!r} is not a batch id or range")
            first, last = int(found[1]), int(found[2] or found[1])
            ids.extend(range(first, last + 1))
        return sql.SQL("t.batch_id=ANY(%(batches)s)"), {"batches": ids}
    raise ProfileError(f"--runs {text!r}: use marker:<summary key> or batches:<id>-<id>")


def runs(conn, text: str) -> list[dict]:
    condition, params = run_filter(text)
    counts = sql.SQL(",").join(sql.SQL(f"{query} AS {name}") for name, query in COUNTS.items())
    statement = sql.SQL(RUNS).format(filter=condition, counts=counts)
    cursor = conn.execute(statement, params)
    names = [column.name for column in cursor.description]
    found = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    batches = sorted({run["batch_id"] for run in found if run["batch_id"] is not None})
    events = {(b, p): n for b, p, n in conn.execute(EVENTS, (batches,)).fetchall()}
    for run in found:
        run["test_events"] = events.get((run["batch_id"], run["product_id"]), 0)
    return found


def rows_per_run(found: list[dict]) -> dict:
    """Per table, the spread of the runs' rows."""
    return {name: spread([run[name] for run in found]) for name in TABLES}


def sizes(conn, kinds: dict[str, list[dict]]) -> dict:
    """Each table's rows and bytes, and per kind the bytes of its mean run (BYTES_BASIS)."""
    tables = {}
    for name, table_bytes, index_bytes in conn.execute(SIZES, (list(TABLES),)).fetchall():
        query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))
        rows = conn.execute(query).fetchone()[0]
        tables[name] = {"rows": rows, "table_bytes": table_bytes, "index_bytes": index_bytes}
    per_run = {}
    for kind, found in kinds.items():
        means = {name: stats["mean"] or 0 for name, stats in rows_per_run(found).items()}
        per_run[kind] = {
            name: {
                field: round(size[field] / size["rows"] * means[name]) if size["rows"] else 0
                for field in ("table_bytes", "index_bytes")
            }
            for name, size in sorted(tables.items())
        }
    return {"basis": BYTES_BASIS, "tables": dict(sorted(tables.items())), "per_run": per_run}


def browser(conn, keys: list[int]) -> dict:
    """network -> target -> engine -> runs, tries, outcome counts and page-load p50/p90."""
    params = {"keys": keys, "engine": DEFAULT_ENGINE}
    found: dict[str, dict] = {}
    for provider, target, engine, count, completed, *counts in conn.execute(
        OUTCOME_SUMS, params
    ).fetchall():
        outcomes = dict(zip(("ok", *COUNTERS, *CODES), counts, strict=True))
        outcomes["other_error"] = completed - sum(outcomes.values())
        entry = {"runs": count, "tries": completed, "outcomes": {k: outcomes[k] for k in OUTCOMES}}
        entry["ok_share"] = share(outcomes["ok"], completed)
        found.setdefault(provider, {}).setdefault(target, {})[engine] = entry
    loads: dict[tuple, Counter] = {}
    for provider, target, engine, ms, n in conn.execute(LOADS, params).fetchall():
        loads.setdefault((provider, target, engine), Counter())[ms] = n
    for (provider, target, engine), bins in loads.items():
        percentiles = Histogram(bins).percentiles()
        entry = found[provider][target][engine]
        entry["page_load_ms"] = {"p50": percentiles["p50"], "p90": percentiles["p90"]}
    return found


def read(path, text: str) -> dict:
    """The reference entries of the runs `text` selects, by kind (only kinds with runs)."""
    with connect(path) as conn:
        conn.read_only, conn.isolation_level = True, IsolationLevel.REPEATABLE_READ
        with conn.transaction():
            return _read(conn, text)


def _read(conn, text: str) -> dict:
    found = runs(conn, text)
    kinds: dict[str, list[dict]] = {}
    for run in found:
        kinds.setdefault(run["kind"], []).append(run)
    byte_sizes = sizes(conn, kinds)
    at = datetime.now(UTC).isoformat(timespec="seconds")
    database = conn.execute("SELECT current_database()").fetchone()[0]
    entries = {}
    for kind, own in sorted(kinds.items()):
        networks: dict[str, list[dict]] = {}
        for run in own:
            networks.setdefault(run["provider"], []).append(run)
        entries[kind] = {
            "runs_filter": text,
            "database": database,
            "read_at": at,
            "runs": len(own),
            "rows_per_run": rows_per_run(own),
            "networks": {
                name: {"runs": len(n), "rows_per_run": rows_per_run(n)}
                for name, n in sorted(networks.items())
            },
            "bytes": byte_sizes | {"per_run": byte_sizes["per_run"][kind]},
        }
        if kind == "browser":
            entries[kind]["targets"] = browser(conn, [run["run_key"] for run in own])
    return entries
