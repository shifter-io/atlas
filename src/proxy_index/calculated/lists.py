"""Typed rows of a calculated payload, and the shared breakdown lists (contract K2).

persist() writes a payload's cohorts, windows, scores, series and overlaps through a batch
(batch.py: a fixed number of statements per build, never one per group, window or row). Each
target-free window's full breakdowns are its list: one calculated_lists row and its
calculated_list_rows, written for many product groups at once by list_write.write_lists() (one
INSERT, one label lookup and one COPY); the window row names the list in list_id. A list is
written once and may outlive its build; store.collect() deletes the lists no window references.

A daily group's window has a list signature (signatures(), v1): every input its list is computed
from. A build whose window has the signature of a stored list of the same product, cohort and
window reuses that list and writes no rows. The list is still computed, and a content_hash other
than the stored one raises ValueError (verify-on-hit), so a missed input fails the calculation
and the previous build keeps serving. Census-pulse groups have no signature (NULL) yet.
"""

from __future__ import annotations

from collections import defaultdict
from functools import partial

from psycopg.rows import tuple_row

from ..storage import column
from .batch import CRITERIA as CRITERIA
from .batch import Batch, cohort_params
from .list_write import prepare
from .policy import digest


def store_cohort(conn, cohort, criteria):
    """Store one cohort now (its criteria checked); persist() batches its cohorts instead."""
    batch = Batch(conn)
    batch.cohort(cohort_params(cohort, criteria), criteria)
    batch.flush()


def split_reasons(score, product):
    """Reasons a product score derives from its weighted, not-ready components, plus the rest."""
    if set(score["weights"]) != set(score["components"]):
        raise ValueError("Score weights and components differ")
    derived = [
        c["reason"] or f"{k} not ready"
        for k, c in score["components"].items()
        if product and score["weights"][k] > 0 and c["state"] != "ready"
    ]
    reasons, size = score["reasons"], len(derived)
    start = next(
        (i for i in range(len(reasons) - size + 1) if reasons[i : i + size] == derived), None
    )
    if start is None:
        raise ValueError("Score reasons cannot be rebuilt from their components")
    return [(i - start, r) for i, r in enumerate(reasons[:start])] + [
        (i, r) for i, r in enumerate(reasons[start + size :], 1)
    ]


SIGNATURE_VERSION = 1
# A run's enrichment: its newest run_enrichments row, the release its observations are read with
# (releases.RUN_RELEASES).
ENRICHMENTS = (
    "SELECT DISTINCT ON (run_key) run_key,release_id,artifact_sha256 FROM run_enrichments "
    "WHERE run_key=ANY(%s) ORDER BY run_key,release_id DESC"
)


def signatures(conn, payload, runs, buckets):
    """List signature v1 of every daily group's window: {(product_id, cohort, hours): sha256}.

    runs and buckets are the calculation's, after build() gave each run its group and light (its
    histograms count for the breakdowns' latency). The signature is the digest of the
    implementation, methodology, window hours, country names and, sorted by run_key, each run of
    the group with buckets in the window: run_key, summary_sha256, enrichment release_id and
    artifact_sha256 (None when unenriched), its first and last bucket in the window and light,
    and its geo artifact when its summary names one (geo_artifact()).
    Census-pulse (batch) groups are left out: their lists keep signature NULL.
    """
    cur = conn.cursor(row_factory=tuple_row)
    countries = digest(sorted(cur.execute("SELECT country_code,name FROM countries").fetchall()))
    daily = {r["run_key"]: r for r in runs if r.get("batch_id") is None}
    periods = [(p["start"], p["end"], p["hours"]) for p in payload["periods"].values()]
    spans = defaultdict(dict)  # (group, hours) -> run_key -> [first, last] bucket in the window
    for b in buckets:  # ordered by bucket_start
        r, at = daily.get(b["run_key"]), b["bucket_start"]
        if r is None:
            continue
        for start, end, hours in periods:
            if start <= at < end:
                spans[r["group"], hours].setdefault(r["run_key"], [at, at])[1] = at
    signed_runs = sorted({k for runs_of in spans.values() for k in runs_of})  # only these
    enriched = {k: (rel, sha) for k, rel, sha in cur.execute(ENRICHMENTS, (signed_runs,))}
    head = {"v": SIGNATURE_VERSION, "impl": payload["implementation_id"], "countries": countries}
    head["method"] = payload["methodology"]["id"]
    signed = {}
    for group in {r["group"] for r in daily.values()}:
        for _, _, hours in periods:
            entries = [
                [k, daily[k]["summary_sha256"], *enriched.get(k, (None, None))]
                + [first.isoformat(), last.isoformat(), daily[k]["light"]]
                + ([geo] if (geo := geo_artifact(daily[k])) else [])
                for k, (first, last) in sorted(spans[group, hours].items())
            ]
            signed[(*group, hours)] = digest(head | {"hours": hours, "runs": entries})
    return signed


def geo_artifact(run):
    """The geo artifact run's stored summary names, or None. Geo rows added to a stored run
    (a run outside a batch, #870) add it, so the run's inputs change with them."""
    return ((run.get("summary") or {}).get("artifacts") or {}).get("geo")


def reusing(conn, payload, runs, buckets):
    """The persist() of a built payload: with its windows' signatures, so it reuses lists."""
    return partial(persist, signed=signatures(conn, payload, runs, buckets))


def persist(conn, payload, signed=None, batch=None):
    """Write payload's typed rows; signed: the signatures() of its windows (none: NULL).

    The rows and new lists go to batch (batch.py), written a chunk at a time and at its flush;
    without one they are all written before persist() returns. Returns the list_id of each
    window (by group id and window label) known so far: all of them without a batch."""
    rows = Batch(conn) if batch is None else batch
    build_id = int(payload["snapshot_id"])
    for p in payload["products"]:
        rows.cohort(cohort_params(p["cohort"], p["criteria"]), p["criteria"])
    for p in payload["products"]:
        # A window that names a list already has it: the incremental calculation wrote it.
        new = [(label, w) for label, w in p["windows"].items() if "lists" not in w]
        keys = [(p["product_id"], p["cohort"], w["period"]["hours"]) for _, w in new]
        signatures = [(signed or {}).get(k) for k in keys]
        prepared = [prepare(p, w, s) for (_, w), s in zip(new, signatures, strict=True)]
        rows.lists(build_id, [(p["id"], label) for label, _ in new], prepared)
        for label, w in p["windows"].items():
            if "lists" in w:
                rows.known((p["id"], label), w["lists"]["id"])
            _results(rows, (build_id, (p["id"], label)), p, w)
    for o in payload["overlap"]:
        rows.add("overlaps", _overlap(build_id, payload["periods"], o))
    if batch is None:
        rows.flush()
    return rows.list_ids


def _results(rows, ids, p, w):
    """Add window w's result rows: its own on its list (ids: build id, list key or id) with its
    scores and series, then one per target (no list) with its balanced score."""
    build_id, list_id = ids
    for target, scores in [
        ("", w["scores"]),
        *[(t, {"balanced": v["score"]}) for t, v in w["targets"].items()],
    ]:
        result = rows.window(_window((build_id, None if target else list_id), p, w, target))
        for profile, s in scores.items():
            _score(rows, (result, profile), s, not target)
        if not target:
            for row in w["series"]:
                rows.add("series", _point(result, row))


def _overlap(build_id, periods, o):
    """One overlap row, its products in id order."""
    a, b = o["product_a"], o["product_b"]
    na, nb = o["ips_a"], o["ips_b"]
    if a > b:
        a, b, na, nb = b, a, nb, na
    return (build_id, o["cohort"], periods[o["window"]]["hours"], a, b, o["shared_ips"], na, nb)


def _window(ids, p, w, target):
    """One window row (ids: its build and list)."""
    m = w["targets"][target]["metrics"] if target else w["metrics"]
    ps = w["pool"] if not target else {}
    pool = ("unique_ips", "new_ips", "recurring_share", "countries", "regions", "cities", "asns")
    counts = ("completed", "successes", "errors", "body_bytes", "success_rate", "timeout_rate")
    return (
        ids[0],
        p["product_id"],
        p["cohort"],
        target,
        w["period"]["hours"],
        *[m[k] for k in counts],
        *[m["latency_ms"][k] for k in ("p50", "p90", "p95", "p99")],
        *[ps.get(k) for k in pool],
        m["coverage"],
        m["last_observed"],
        w["readiness"],
        *[m[k] for k in ("challenge_rate", "block_rate", "uptime", "bytes_up", "bytes_down")],
        ids[1],
    )


def _score(rows, key, s, product):
    """Add score s with its components and reasons (key: its window and profile)."""
    result, profile = key
    fields = ("score", "grade", "rank", "previous_rank", "state", "scored_at", "valid_until")
    rows.add("scores", (result, profile, *[s[k] for k in fields]))
    for i, (k, c) in enumerate(s["components"].items()):
        weight, value = column("number", s["weights"][k]), column("number", c["value"])
        component = (result, profile, k, i, c["state"], weight, value, column("json", c["inputs"]))
        rows.add("components", (*component, c["reason"]))
    for i, r in split_reasons(s, product):
        rows.add("reasons", (result, profile, i, r))


def _point(result, row):
    """One series row of window result."""
    latency = [row["latency_ms"][k] for k in ("p50", "p90", "p99")]
    counts = (row["completed"], row["successes"])
    return (result, row["at"], *counts, *latency, row["unique_ips"], row["uptime"])
