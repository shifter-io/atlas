"""A build's typed result rows, written with a fixed number of statements per build.

lists.persist() hands every row of its product groups to a Batch instead of writing it: the
cohorts, the windows' new lists, the window rows, their scores, score components and score
reasons, the series points and the overlaps. store.write() keeps one Batch for the whole build.

The rows go by COPY into temporary staging tables (STAGE: typed like their target columns, no
keys, dropped at the flush or the commit), CHUNK rows per COPY at most, as they come; a row names
its window by its number in the batch (n). The new lists are written CHUNK list rows at a time by
list_write.write_lists() (a fixed number of statements for every group pending), before the
window rows that name them are staged. flush() then moves each staging table into its target
with one INSERT … SELECT, in foreign-key order (MOVE): the cohorts and score-reason labels
(set-based upserts), the windows, whose result_ids are mapped back to n through their natural key
(batch_ids), then their scores, components, reasons, series points and the overlaps. The number
of statements does not depend on the number of groups, windows, scores or rows, only on the
CHUNKs. Everything stays in the calculation's one transaction; the rows and values are those the
former INSERT per row wrote.
"""

from __future__ import annotations

from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from ..storage import column
from .list_write import write_lists
from .policy import digest

CHUNK = 100_000  # rows per COPY, and the list rows written together
CRITERIA = {
    "kind": "text",
    "country": "text",
    "runner_location": "text",
    "concurrency": "integer",
    "timeout_seconds": "number",
    "rate": "number",
    "proxy_protocol": "text",
    "session_policy": "text",
    "ttl_seconds": "integer",
    "parameters": "json",
    "flags": "json",
    "target_variant": "json",
}
# Place and ASN labels use the shared name lists (names.labels()); reasons stay calculator-only.
LABELS = {"score_reasons": ("reason_id", ("reason",), ("text",))}
WINDOW_COLUMNS = (
    "build_id,product_id,cohort,target,window_hours,completed,successes,errors,body_bytes,"
    "success_rate,timeout_rate,p50,p90,p95,p99,unique_ips,new_ips,recurring_share,countries,"
    "regions,cities,asns,coverage,last_observed,readiness,challenge_rate,block_rate,uptime,"
    "bytes_up,bytes_down,list_id"
)
WINDOW_KEY = "build_id,product_id,cohort,target,window_hours"
SCORES = "profile,score,grade,rank,previous_rank,state,scored_at,valid_until"
COMPONENTS = "profile,component,position,state,weight,value,inputs"
SERIES = "bucket_start,completed,successes,p50,p90,p99,unique_ips,uptime"
OVERLAPS = "build_id,cohort,window_hours,product_a,product_b,shared_ips,ips_a,ips_b"
# Each staging table: the columns COPY writes and the table that gives their types.
STAGE = {
    "cohorts": (f"cohort,{','.join(CRITERIA)},extra", "cohorts"),
    "windows": (f"n,{WINDOW_COLUMNS}", "calculated_windows"),
    "scores": (f"n,{SCORES}", "calculated_scores"),
    "components": (f"n,{COMPONENTS},reason", "calculated_score_components"),
    "reasons": ("n,profile,position,reason", "calculated_score_reasons"),
    "series": (f"n,{SERIES}", "calculated_series"),
    "overlaps": (OVERLAPS, "calculated_overlaps"),
}
TYPED = {"n": "0::bigint AS n", "reason": "''::text AS reason"}  # columns the targets lack
CREATE = ";".join(
    [
        f"CREATE TEMP TABLE batch_{name} ON COMMIT DROP AS SELECT "
        + ",".join(TYPED.get(c, c) for c in columns.split(","))
        + f" FROM {source} WITH NO DATA"
        for name, (columns, source) in STAGE.items()
    ]
    + ["CREATE TEMP TABLE batch_ids(n bigint PRIMARY KEY,result_id bigint) ON COMMIT DROP"]
)
DROP = "DROP TABLE " + ",".join(f"batch_{name}" for name in (*STAGE, "ids"))
# The staging tables into their targets, in foreign-key order.
MOVE = (
    f"INSERT INTO cohorts({STAGE['cohorts'][0]}) SELECT * FROM batch_cohorts "
    "ON CONFLICT DO NOTHING",
    "INSERT INTO score_reasons(reason) SELECT reason FROM batch_components WHERE reason<>'' "
    "UNION SELECT reason FROM batch_reasons ORDER BY 1 ON CONFLICT DO NOTHING",
    f"WITH w AS (INSERT INTO calculated_windows({WINDOW_COLUMNS}) SELECT {WINDOW_COLUMNS} "
    f"FROM batch_windows ORDER BY n RETURNING result_id,{WINDOW_KEY}) INSERT INTO batch_ids "
    f"SELECT b.n,w.result_id FROM w JOIN batch_windows b USING({WINDOW_KEY})",
    f"INSERT INTO calculated_scores(result_id,{SCORES}) "
    f"SELECT i.result_id,{SCORES} FROM batch_scores JOIN batch_ids i USING(n)",
    f"INSERT INTO calculated_score_components(result_id,{COMPONENTS},reason_id) "
    f"SELECT i.result_id,{','.join('c.' + k for k in COMPONENTS.split(','))},r.reason_id "
    "FROM batch_components c JOIN batch_ids i USING(n) "
    "LEFT JOIN score_reasons r ON r.reason=c.reason AND c.reason<>''",
    "INSERT INTO calculated_score_reasons(result_id,profile,position,reason_id) "
    "SELECT i.result_id,c.profile,c.position,r.reason_id FROM batch_reasons c "
    "JOIN batch_ids i USING(n) JOIN score_reasons r USING(reason)",
    f"INSERT INTO calculated_series(result_id,{SERIES}) "
    f"SELECT i.result_id,{SERIES} FROM batch_series JOIN batch_ids i USING(n)",
    f"INSERT INTO calculated_overlaps({OVERLAPS}) SELECT * FROM batch_overlaps",
)
COHORTS = (
    "SELECT cohort,criteria FROM cohort_criteria WHERE cohort IN (SELECT cohort FROM batch_cohorts)"
)


def labels(conn, table, wanted):
    """Map label tuples to shared ids, inserting only the missing ones."""
    key, columns, types = LABELS[table]
    cur = conn.cursor(row_factory=tuple_row)
    query = (
        f"SELECT {key},{','.join(columns)} FROM {table} "
        f"WHERE {columns[0]}=ANY(%s) OR {columns[0]} IS NULL"
    )
    firsts = sorted({w[0] for w in wanted if w[0] is not None})
    found = {tuple(row[1:]): row[0] for row in cur.execute(query, (firsts,))}
    missing = sorted(set(wanted) - set(found), key=str)
    if missing:
        cur.execute(
            f"INSERT INTO {table}({','.join(columns)}) SELECT * FROM unnest("
            + ",".join(f"%s::{t}[]" for t in types)
            + ") ON CONFLICT DO NOTHING",
            [list(values) for values in zip(*missing, strict=True)],
        )
        found = {tuple(row[1:]): row[0] for row in cur.execute(query, (firsts,))}
    return found


class Batch:
    """The rows of a build that wait for their flush.

    A window row's list_id is its list's id, or the (group id, window label) of a new list added
    with lists(); list_ids maps each (group id, label) to its list_id once it is known."""

    def __init__(self, conn):
        self.conn, self.n, self.staged = conn, 0, False
        self.cohorts = set()  # (cohort, criteria digest) of every cohort added
        self.rows = {name: [] for name in STAGE}
        self.pending, self.list_ids = [], {}  # new lists: (build id, (group id, label), list)

    def cohort(self, params, criteria):
        """Add a cohorts row (params, cohort first) whose criteria the flush checks."""
        if (key := (params[0], digest(criteria))) not in self.cohorts:
            self.cohorts.add(key)
            self.add("cohorts", params)

    def lists(self, build_id, keys, prepared):
        """Add the prepared new lists of (group id, label) keys; written CHUNK rows at a time."""
        self.pending += [(build_id, k, p) for k, p in zip(keys, prepared, strict=True)]
        if sum(len(p.rows) for *_, p in self.pending) >= CHUNK:
            self._write_lists()

    def known(self, key, list_id):
        """Record the list_id of a window whose list is already written."""
        self.list_ids[key] = list_id

    def waiting(self):
        """Whether an added list has no id yet."""
        return bool(self.pending)

    def window(self, params):
        """Add a window row; return the number its other rows name it by."""
        self.n += 1
        self.add("windows", (self.n, *params))
        return self.n

    def add(self, table, row):
        self.rows[table].append(row)
        if len(self.rows[table]) >= CHUNK:
            self._stage(table)

    def flush(self):
        """Write every added row; nothing waits afterwards."""
        self._write_lists()
        for table in STAGE:
            self._stage(table)
        if not self.staged:
            return
        cur = self.conn.cursor(row_factory=tuple_row)
        for statement in MOVE:
            cur.execute(statement)
        stored = {cohort: digest(criteria) for cohort, criteria in cur.execute(COHORTS)}
        if any(stored.get(cohort, criteria) != criteria for cohort, criteria in self.cohorts):
            raise ValueError("Cohort criteria cannot be stored losslessly")
        cur.execute(DROP)
        self.staged = False

    def _write_lists(self):
        """Write the pending lists, one write_lists() call per build (a build has one)."""
        pending, self.pending = self.pending, []
        for build_id in sorted({build_id for build_id, *_ in pending}):
            found = [(k, p) for b, k, p in pending if b == build_id]
            ids = write_lists(self.conn, build_id, [p for _, p in found])
            self.list_ids |= dict(zip([k for k, _ in found], ids, strict=True))

    def _stage(self, table):
        """COPY the table's added rows into its staging table."""
        rows, self.rows[table] = self.rows[table], []
        if not rows:
            return
        if table == "windows":
            self._write_lists()
            ids = self.list_ids
            rows = [(*r[:-1], ids[r[-1]] if isinstance(r[-1], tuple) else r[-1]) for r in rows]
        cur = self.conn.cursor()
        if not self.staged:
            cur.execute(CREATE)
            self.staged = True
        with cur.copy(f"COPY batch_{table}({STAGE[table][0]}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)


def cohort_params(cohort, criteria):
    """The cohorts row of criteria (typed CRITERIA columns, the rest in extra)."""
    extra = {k: v for k, v in criteria.items() if k not in CRITERIA}
    values = []
    for key, kind in CRITERIA.items():
        try:
            values.append(column(kind, criteria[key]))
        except (KeyError, TypeError):
            values.append(None)
            if key in criteria:
                extra[key] = criteria[key]
    return (cohort, *values, Jsonb(extra) if extra else None)
