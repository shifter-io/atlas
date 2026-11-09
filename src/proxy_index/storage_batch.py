"""storage.ingest_many's session temp tables: each holds the rows of every run of a batch with
the run's index in it (idx), and is dropped at commit. A multi-row statement carries as many rows
as PostgreSQL's PARAMETERS allow; all go in pipeline mode, so filling the tables costs no round
trip."""

from __future__ import annotations

import uuid

from psycopg import RawCursor

from .storage_read import OUTCOME_KEYS

PARAMETERS = 65535  # the most a statement can bind (the protocol's 16-bit count)
# pool_stats' typed pool columns (storage_ingest._profile).
POOL = (
    "target_id target_url target_engine target_version requested_country runner_location "
    "proxy_protocol gateway_host gateway_port adapter adapter_version account_pool parameters "
    "session_policy ttl_seconds extra_flags extra"
).split()
# Rows fill each table's leading columns; incoming_runs takes its types from the tables its rows
# go to. A report to add has the run_key of a stored run, a new one's after insert_runs.
TABLES = {
    "incoming_runs": "ON COMMIT DROP AS SELECT 0 AS idx,t.run_id,t.started_at,t.finished_at,"
    "t.status,t.summary_sha256,t.summary,t.batch_id,p.provider,p.name AS product,s.scope_hash,"
    "s.concurrency,s.timeout_seconds,s.requests_per_second,g.url,g.engine,l.pool_hash,"
    + ",".join(f"l.{name}" for name in POOL)
    + ",s.product_id,s.pool_id,s.scope_id,t.run_key FROM test_runs t,products p,targets g,"
    "scope_stats s,pool_stats l WITH NO DATA",
    "incoming_buckets": "(idx integer,bucket_start timestamptz,completed bigint,successes bigint,"
    "response_body_bytes bigint,errors jsonb,status_codes jsonb,histogram_steps integer[],"
    "histogram_counts integer[],extra jsonb,outcome_counters boolean,"
    + ",".join(f"{name} bigint" for name in OUTCOME_KEYS)
    + ") ON COMMIT DROP",
    "incoming_ips": "(idx integer,bucket timestamptz,ip inet,count bigint,first_seen timestamptz,"
    "last_seen timestamptz,PRIMARY KEY(idx,bucket,ip)) ON COMMIT DROP",
    "incoming_releases": "(idx integer,release text,metadata jsonb,checksum text,run_key bigint,"
    "release_id integer,PRIMARY KEY(idx,release)) ON COMMIT DROP",
    "incoming_enrichment": "(idx integer,release text,bucket timestamptz,ip inet,count bigint,"
    "attributes jsonb,PRIMARY KEY(idx,release,bucket,ip)) ON COMMIT DROP",
}
# The batch's stored runs, with each one's imported enrichment releases (sha256: checksum).
STORED = (
    "SELECT t.run_id,to_jsonb(t)-'summary',(SELECT jsonb_object_agg(d.sha256,e.artifact_sha256) "
    "FROM run_enrichments e JOIN dbip_releases d USING(release_id) WHERE e.run_key=t.run_key) "
    "FROM test_runs t WHERE t.run_id=ANY(%s::uuid[])"
)
# Each new run's scope before the batch, and its summary as the stored rows rebuild it.
SCOPES = (
    "SELECT r.idx,r.run_key,s.scope_id,s.metrics,s.histogram_steps,s.histogram_counts,"
    "stored_run_summary(r.run_key) FROM incoming_runs r JOIN scope_stats s USING(scope_id) "
    "ORDER BY r.idx"
)


def load(conn, rows):
    """Create the temp tables, add their rows ({table: [row]}) and give the planner their sizes."""
    for table, columns in TABLES.items():
        conn.execute(f"CREATE TEMP TABLE {table} {columns}")
        many(conn, f"INSERT INTO {table} VALUES ", "", None, rows[table])
    conn.execute("ANALYZE " + ",".join(TABLES))


def many(conn, head, tail, types, rows):
    """Run head + a VALUES list + tail over rows, as many at a time as PARAMETERS allows; types
    casts each value. The placeholders are PostgreSQL's ($n): psycopg would parse thousands of
    %s again every time."""
    if not rows:
        return
    casts = [f"::{kind}" for kind in types] if types else [""] * len(rows[0])
    width = len(casts)
    step = PARAMETERS // width
    for start in range(0, len(rows), step):
        part = rows[start : start + step]
        marks = ",".join(
            "(" + ",".join(f"${r * width + c + 1}{cast}" for c, cast in enumerate(casts)) + ")"
            for r in range(len(part))
        )
        RawCursor(conn).execute(head + marks + tail, [value for row in part for value in row])


def link(conn, links):
    """Join stored runs to the batches they are imported with: (run_key, batch_id) pairs."""
    head, types = "UPDATE test_runs t SET batch_id=v.batch_id FROM (VALUES ", ("bigint", "bigint")
    many(conn, head, ") v(run_key,batch_id) WHERE t.run_key=v.run_key", types, links)


def update_scopes(conn, scopes):
    """Write scopes' metrics and lifetime histograms: (scope_id, metrics, steps, counts)."""
    head = "UPDATE scope_stats s SET metrics=v.metrics,histogram_steps=v.steps,"
    head += "histogram_counts=v.counts,updated_at=now() FROM (VALUES "
    tail = ") v(scope_id,metrics,steps,counts) WHERE s.scope_id=v.scope_id"
    many(conn, head, tail, ("bigint", "jsonb", "integer[]", "bigint[]"), scopes)


def existing(runs, rows):
    """Check stored runs (STORED rows) as a replay would; return the new runs. Each run's reports
    not yet imported go to run.added."""
    found = {str(run_id): (row, releases or {}) for run_id, row, releases in rows}
    for run in runs:
        run.stored, releases = found.get(str(uuid.UUID(run.run_id)), (None, {}))
        run.run_key = run.stored and run.stored["run_key"]
        if run.stored and run.stored["summary_sha256"] != run.summary_hash:
            raise ValueError("Run ID already exists with different content")
        batch = run.stored and run.stored.get("batch_id")
        if batch is not None and run.batch_id is not None and batch != run.batch_id:
            raise ValueError("Run already belongs to another batch")
        if any(releases.get(report[0], report[2]) != report[2] for report in run.reports):
            raise ValueError("Previously imported enrichment has changed")
        run.added = [report for report in run.reports if report[0] not in releases]
    return [run for run in runs if not run.stored]


def upsert_scopes(conn):
    """Add the new runs' providers, products, targets, pools and scopes where new (a new row takes
    the batch's first run's values), and give incoming_runs their ids."""
    conn.execute(
        "INSERT INTO providers SELECT DISTINCT provider FROM incoming_runs ORDER BY 1 "
        "ON CONFLICT DO NOTHING"
    )
    conn.execute(
        "INSERT INTO products(provider,name) SELECT DISTINCT provider,product FROM incoming_runs "
        "ORDER BY 1,2 ON CONFLICT DO NOTHING"
    )
    conn.execute(
        "UPDATE incoming_runs i SET product_id=p.product_id FROM products p "
        "WHERE (p.provider,p.name)=(i.provider,i.product)"
    )
    # Pools and scopes reference targets; a target nobody seeded joins unscheduled.
    conn.execute(
        "INSERT INTO targets(target_id,url,engine,census,pulse_enabled) SELECT DISTINCT ON "
        "(target_id) target_id,url,engine,false,false FROM incoming_runs ORDER BY target_id,idx "
        "ON CONFLICT DO NOTHING"
    )
    pool, performance = ",".join(POOL), "concurrency,timeout_seconds,requests_per_second"
    conn.execute(
        f"INSERT INTO pool_stats(product_id,pool_hash,{pool}) SELECT DISTINCT ON "
        f"(product_id,pool_hash) product_id,pool_hash,{pool} FROM incoming_runs "
        "ORDER BY product_id,pool_hash,idx ON CONFLICT DO NOTHING"
    )
    conn.execute(
        "UPDATE incoming_runs i SET pool_id=l.pool_id FROM pool_stats l "
        "WHERE (l.product_id,l.pool_hash)=(i.product_id,i.pool_hash)"
    )
    conn.execute(
        f"INSERT INTO scope_stats(product_id,target,scope_hash,pool_id,{performance}) SELECT "
        f"DISTINCT ON (product_id,target_id,scope_hash) product_id,target_id,scope_hash,pool_id,"
        f"{performance} FROM incoming_runs ORDER BY product_id,target_id,scope_hash,idx "
        "ON CONFLICT DO NOTHING"
    )
    conn.execute(
        "UPDATE incoming_runs i SET scope_id=s.scope_id FROM scope_stats s "
        "WHERE (s.product_id,s.target,s.scope_hash)=(i.product_id,i.target_id,i.scope_hash)"
    )
