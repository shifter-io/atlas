"""Typed rows of a calculated payload, and the shared breakdown lists (contract K2).

persist() writes a payload's cohorts, windows, scores, series and overlaps. Each target-free
window's full breakdowns are its list: one calculated_lists row and its calculated_list_rows,
copied in one COPY per persist(), never a round trip per row; the window row names the list in
list_id. A list is written once and may outlive its build; store.collect() deletes the lists no
window references.

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
from psycopg.types.json import Jsonb

from ..names import labels as shared_labels
from ..storage import column
from .aggregate import BREAKDOWNS, NETWORK, PLACE
from .policy import digest
from .slim import POOL_LISTS

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
# Label fields of each breakdown: its place columns and whether it names an ASN.
FIELDS = {
    "country": (PLACE[:2], False),
    "region": (PLACE[:4], False),
    "city": (PLACE, False),
    "asn": ((), True),
    "country_asn": (PLACE[:2], True),
    "city_asn": (PLACE, True),
}
COUNTERS = ("ips", "observations", "completed", "successes", "success_rate", "challenges", "blocks")
LATENCY = ("p50", "p90", "p99")
# Place and ASN labels use the shared name lists (shared_labels); reasons stay calculator-only.
LABELS = {"score_reasons": ("reason_id", ("reason",), ("text",))}


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


def store_cohort(conn, cohort, criteria):
    extra = {k: v for k, v in criteria.items() if k not in CRITERIA}
    values = []
    for key, kind in CRITERIA.items():
        try:
            values.append(column(kind, criteria[key]))
        except (KeyError, TypeError):
            values.append(None)
            if key in criteria:
                extra[key] = criteria[key]
    conn.execute(
        "INSERT INTO cohorts(cohort,"
        + ",".join(CRITERIA)
        + ",extra) VALUES ("
        + ",".join(["%s"] * (len(CRITERIA) + 2))
        + ") ON CONFLICT DO NOTHING",
        (cohort, *values, Jsonb(extra) if extra else None),
    )
    rebuilt = conn.execute(
        "SELECT criteria FROM cohort_criteria WHERE cohort=%s", (cohort,)
    ).fetchone()["criteria"]
    if digest(rebuilt) != digest(criteria):
        raise ValueError("Cohort criteria cannot be stored losslessly")


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


# A list's figures: the window's pool figures that do not depend on its start (new_ips and
# churn_rate do; the daily averages depend on the cutoff), plus its pool lists' entry counts.
# lists.py is part of IMPLEMENTATION_ID, so a change to FIGURES or content_hash changes every
# signature. A list the incremental calculation counted in SQL also stores "first_seen", its IPs
# per 5-minute bin of their product's first sighting from the window's start on (CALC-INC-2's
# new IPs), outside content_hash: a late run moves product_ips.first_seen, which no signature
# names (UNHASHED).
FIGURES = (
    "unique_ips",
    "observations",
    "recurring_share",
    "countries",
    "regions",
    "cities",
    "asns",
    "datacenter_asn_share",
    "classification_coverage",
    "geo_accuracy",
    "enrichment_coverage",
)
UNHASHED = ("first_seen",)
WINDOW_COLUMNS = (
    "build_id,product_id,cohort,target,window_hours,completed,successes,errors,body_bytes,"
    "success_rate,timeout_rate,p50,p90,p95,p99,unique_ips,new_ips,recurring_share,countries,"
    "regions,cities,asns,coverage,last_observed,readiness,challenge_rate,block_rate,uptime,"
    "bytes_up,bytes_down,list_id"
)
LIST_ROW_COLUMNS = (
    "list_id,dimension,position,place_id,asn_label_id,ips,observations,completed,successes,"
    "challenges,blocks,p50,p90,p99"
)


SIGNATURE_VERSION = 1
STORED = (
    "SELECT list_id,content_hash FROM calculated_lists "
    "WHERE product_id=%s AND cohort=%s AND window_hours=%s AND signature=%s"
)
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
    artifact_sha256 (None when unenriched), its first and last bucket in the window and light.
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
                for k, (first, last) in sorted(spans[group, hours].items())
            ]
            signed[(*group, hours)] = digest(head | {"hours": hours, "runs": entries})
    return signed


def reusing(conn, payload, runs, buckets):
    """The persist() of a built payload: with its windows' signatures, so it reuses lists."""
    return partial(persist, signed=signatures(conn, payload, runs, buckets))


def persist(conn, payload, signed=None):
    """Write payload's typed rows; signed: the signatures() of its windows (none: NULL). Returns
    each window's list_id by (group id, window label)."""
    build_id = int(payload["snapshot_id"])
    for p in payload["products"]:
        store_cohort(conn, p["cohort"], p["criteria"])
    reason_ids = _reasons(conn, payload["products"])
    rows, lists = [], {}
    for p in payload["products"]:
        for label, w in p["windows"].items():
            signature = (signed or {}).get((p["product_id"], p["cohort"], w["period"]["hours"]))
            list_id = lists[p["id"], label] = (
                w["lists"]["id"]  # a stored list the incremental calculation reused
                if "lists" in w
                else write_list(conn, build_id, p, w, rows, signature)
            )
            _results(conn, (build_id, list_id), p, w, reason_ids)
    copy_rows(conn, rows)
    for o in payload["overlap"]:
        _overlap(conn, build_id, payload["periods"], o)
    return lists


def _results(conn, ids, p, w, reason_ids):
    """Store window w's result rows: its own on its list (ids: build id, list id) with its scores
    and series, then one per target (no list) with its balanced score."""
    build_id, list_id = ids
    for target, scores in [
        ("", w["scores"]),
        *[(t, {"balanced": v["score"]}) for t, v in w["targets"].items()],
    ]:
        result = _window(conn, (build_id, None if target else list_id), p, w, target)
        for profile, s in scores.items():
            _score(conn, (result, profile), s, not target, reason_ids)
        if not target:
            _series(conn, result, w["series"])


def _overlap(conn, build_id, periods, o):
    """Store one overlap row, its products in id order."""
    a, b = o["product_a"], o["product_b"]
    na, nb = o["ips_a"], o["ips_b"]
    if a > b:
        a, b, na, nb = b, a, nb, na
    conn.execute(
        "INSERT INTO calculated_overlaps VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (build_id, o["cohort"], periods[o["window"]]["hours"], a, b, o["shared_ips"], na, nb),
    )


def _reasons(conn, products):
    """Reason ids of every stored reason; derived reasons are rebuilt from components."""
    texts = set()
    for p in products:
        for w in p["windows"].values():
            for target, score in [("", s) for s in w["scores"].values()] + [
                (t, v["score"]) for t, v in w["targets"].items()
            ]:
                texts.update(c["reason"] for c in score["components"].values() if c["reason"])
                texts.update(r for _, r in split_reasons(score, not target))
    return {k[0]: v for k, v in labels(conn, "score_reasons", {(t,) for t in texts}).items()}


def figures(pool):
    """The list figures of a window's pool (see FIGURES)."""
    return {k: pool[k] for k in FIGURES if k in pool} | {
        "list_entries": {name: len(pool[name]) for name in POOL_LISTS if name in pool}
    }


def write_list(conn, build_id, product, window, rows, signature=None):
    """Return the list_id of the window's list: the stored list with its signature (verified),
    else a new list, inserted with its rows appended to rows for copy_rows(). content_hash is the
    digest of the full breakdowns and the figures; window["first_seen"], when present, is stored
    with the figures (see FIGURES)."""
    breakdowns, numbers = window["breakdowns"], figures(window["pool"])
    key = (product["product_id"], product["cohort"], window["period"]["hours"], signature)
    content_hash = digest({"breakdowns": breakdowns, "figures": numbers})
    cur = conn.cursor(row_factory=tuple_row)
    if signature is not None and (stored := cur.execute(STORED, key).fetchone()):
        if stored[1] != content_hash:
            raise ValueError("list signature is incomplete")
        return stored[0]
    list_id = cur.execute(
        "INSERT INTO calculated_lists(product_id,cohort,window_hours,signature,content_hash,"
        "entries,figures,created_build) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING list_id",
        (
            *key,
            content_hash,
            Jsonb({name: len(breakdowns[name]) for name in BREAKDOWNS}),
            Jsonb(numbers | {k: window[k] for k in UNHASHED if k in window}),
            build_id,
        ),
    ).fetchone()[0]
    for name, code in BREAKDOWNS.items():
        places, named = FIELDS[name]
        for position, row in enumerate(breakdowns[name]):
            label = set(row) - set(COUNTERS) - set(LATENCY)
            if label != set(places) | (set(NETWORK) if named else set()):
                raise ValueError("Unexpected breakdown label")
            place = tuple(row.get(f) for f in PLACE) if places else None
            network = tuple(row[f] for f in NETWORK) if named else None
            rows.append((list_id, code, position, place, network, row))
    return list_id


def copy_rows(conn, rows):
    """COPY the list rows write_list() collected, their labels resolved to shared ids."""
    places, networks = shared_labels(conn, *({r[i] for r in rows if r[i]} for i in (3, 4)))
    with conn.cursor().copy(f"COPY calculated_list_rows({LIST_ROW_COLUMNS}) FROM STDIN") as copy:
        for list_id, code, position, place, network, row in rows:
            copy.write_row(
                (
                    list_id,
                    code,
                    position,
                    places[place] if place else None,
                    networks[network] if network else None,
                    *[row[k] for k in ("ips", "observations", "completed", "successes")],
                    *[row[k] for k in ("challenges", "blocks", *LATENCY)],
                )
            )


def _window(conn, ids, p, w, target):
    """Insert one window row (ids: its build and list) and return its result_id."""
    m = w["targets"][target]["metrics"] if target else w["metrics"]
    ps = w["pool"] if not target else {}
    pool = ("unique_ips", "new_ips", "recurring_share", "countries", "regions", "cities", "asns")
    return (
        conn.cursor(row_factory=tuple_row)
        .execute(
            f"INSERT INTO calculated_windows({WINDOW_COLUMNS}) "
            "VALUES (" + ",".join(["%s"] * 31) + ") RETURNING result_id",
            (
                ids[0],
                p["product_id"],
                p["cohort"],
                target,
                w["period"]["hours"],
                *[
                    m[k]
                    for k in (
                        "completed",
                        "successes",
                        "errors",
                        "body_bytes",
                        "success_rate",
                        "timeout_rate",
                    )
                ],
                *[m["latency_ms"][k] for k in ("p50", "p90", "p95", "p99")],
                *[ps.get(k) for k in pool],
                m["coverage"],
                m["last_observed"],
                w["readiness"],
                *[
                    m[k]
                    for k in ("challenge_rate", "block_rate", "uptime", "bytes_up", "bytes_down")
                ],
                ids[1],
            ),
        )
        .fetchone()[0]
    )


def _score(conn, key, s, product, reason_ids):
    """Insert score s with its components and reasons (key: its result_id and profile)."""
    result, profile = key
    explicit = split_reasons(s, product)
    conn.execute(
        "INSERT INTO calculated_scores(result_id,profile,score,grade,rank,"
        "previous_rank,state,scored_at,valid_until) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            result,
            profile,
            *[
                s[k]
                for k in (
                    "score",
                    "grade",
                    "rank",
                    "previous_rank",
                    "state",
                    "scored_at",
                    "valid_until",
                )
            ],
        ),
    )
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO calculated_score_components(result_id,profile,component,"
            "position,state,weight,value,inputs,reason_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                (
                    result,
                    profile,
                    k,
                    i,
                    c["state"],
                    column("number", s["weights"][k]),
                    column("number", c["value"]),
                    column("json", c["inputs"]),
                    reason_ids.get(c["reason"]),
                )
                for i, (k, c) in enumerate(s["components"].items())
            ],
        )
        cur.executemany(
            "INSERT INTO calculated_score_reasons VALUES (%s,%s,%s,%s)",
            [(result, profile, i, reason_ids[r]) for i, r in explicit],
        )


def _series(conn, result, series):
    for row in series:
        conn.execute(
            "INSERT INTO calculated_series VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                result,
                row["at"],
                row["completed"],
                row["successes"],
                *[row["latency_ms"][k] for k in ("p50", "p90", "p99")],
                row["unique_ips"],
                row["uptime"],
            ),
        )
