"""A build's typed result rows, written a batch at a time, never a round trip per row.

lists.persist() hands every row of its windows to a Batch instead of writing it: the cohorts,
the window rows, their scores, score components and score reasons, the series points and the
overlaps. flush() writes them in foreign-key order with a fixed number of statements, however
many rows wait: one executemany per table (psycopg pipelines it, one round trip per batch), one
COPY for the score components and one COPY per CHUNK series points, which are most of the rows.

A window's result_id comes from its INSERT … RETURNING in the flush, so the rows of a window
name it by its position in the batch until then (Batch.window()). Reason texts are resolved to
their shared ids by one labels() call per flush. store.write() keeps one Batch for a whole build:
lists.persist() flushes it after a product group once CHUNK series points wait, and store.write()
flushes the rest before the network lists. Everything stays in the calculation's one transaction.
The rows and their values are those the former INSERT per row wrote.
"""

from __future__ import annotations

from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from ..storage import column
from .policy import digest

CHUNK = 100_000  # series points per COPY, and the points that make lists.persist() flush
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
COHORT = (
    f"INSERT INTO cohorts(cohort,{','.join(CRITERIA)},extra) VALUES ("
    + ",".join(["%s"] * (len(CRITERIA) + 2))
    + ") ON CONFLICT DO NOTHING"
)
WINDOW = (
    f"INSERT INTO calculated_windows({WINDOW_COLUMNS}) VALUES ("
    + ",".join(["%s"] * 31)
    + ") RETURNING result_id"
)
SCORE = (
    "INSERT INTO calculated_scores(result_id,profile,score,grade,rank,previous_rank,state,"
    "scored_at,valid_until) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)
COMPONENTS = (
    "COPY calculated_score_components(result_id,profile,component,position,state,weight,value,"
    "inputs,reason_id) FROM STDIN"
)
REASON = "INSERT INTO calculated_score_reasons VALUES (%s,%s,%s,%s)"
SERIES = (
    "COPY calculated_series(result_id,bucket_start,completed,successes,p50,p90,p99,unique_ips,"
    "uptime) FROM STDIN"
)
OVERLAP = "INSERT INTO calculated_overlaps VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
TABLES = ("cohorts", "windows", "scores", "components", "reasons", "series", "overlaps")


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
    """The rows of a build that wait for their flush, by table (TABLES).

    A row of scores, components, reasons or series starts with its window's position in the
    batch; a component row ends with its reason text (or None) and a reason row is (window,
    profile, position, reason text)."""

    def __init__(self, conn):
        self.conn, self.cohorts = conn, set()  # (cohort, criteria digest) of every flush
        self.rows = {table: [] for table in TABLES}

    def cohort(self, params, criteria):
        """Add a cohorts row (params, cohort first) whose criteria the flush checks."""
        if (key := (params[0], digest(criteria))) not in self.cohorts:
            self.cohorts.add(key)
            self.rows["cohorts"].append(params)

    def window(self, params):
        """Add a window row; return the position its other rows name it by."""
        self.rows["windows"].append(params)
        return len(self.rows["windows"]) - 1

    def add(self, table, row):
        self.rows[table].append(row)

    def full(self):
        return len(self.rows["series"]) >= CHUNK

    def flush(self):
        """Write the waiting rows; nothing waits afterwards."""
        rows, self.rows = self.rows, {table: [] for table in TABLES}
        cur = self.conn.cursor(row_factory=tuple_row)
        if rows["cohorts"]:
            cur.executemany(COHORT, rows["cohorts"])
            self._check(cur, {params[0] for params in rows["cohorts"]})
        ids = self._windows(cur, rows["windows"])
        texts = {(r[-1],) for r in rows["components"] if r[-1]}
        texts |= {(r[-1],) for r in rows["reasons"]}
        found = labels(self.conn, "score_reasons", texts) if texts else {}
        reasons = {k[0]: v for k, v in found.items()}
        if rows["scores"]:
            cur.executemany(SCORE, [(ids[r[0]], *r[1:]) for r in rows["scores"]])
        components = [(ids[r[0]], *r[1:-1], reasons.get(r[-1])) for r in rows["components"]]
        _copy(cur, COMPONENTS, components)
        if rows["reasons"]:
            cur.executemany(REASON, [(ids[w], p, n, reasons[t]) for w, p, n, t in rows["reasons"]])
        for start in range(0, len(rows["series"]), CHUNK):
            chunk = rows["series"][start : start + CHUNK]
            _copy(cur, SERIES, [(ids[r[0]], *r[1:]) for r in chunk])
        if rows["overlaps"]:
            cur.executemany(OVERLAP, rows["overlaps"])

    def _check(self, cur, cohorts):
        """Raise ValueError unless each cohort's stored criteria rebuild the criteria added."""
        query = "SELECT cohort,criteria FROM cohort_criteria WHERE cohort=ANY(%s)"
        stored = dict(cur.execute(query, (sorted(cohorts),)).fetchall())
        for cohort, criteria in self.cohorts:
            if cohort in cohorts and digest(stored.get(cohort)) != criteria:
                raise ValueError("Cohort criteria cannot be stored losslessly")

    @staticmethod
    def _windows(cur, windows):
        """Insert the window rows in order; return their result_ids in that order."""
        if not windows:
            return []
        cur.executemany(WINDOW, windows, returning=True)
        ids = [cur.fetchone()[0]]
        while cur.nextset():
            ids.append(cur.fetchone()[0])
        return ids


def _copy(cur, statement, rows):
    if rows:
        with cur.copy(statement) as copy:
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
