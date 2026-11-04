"""A stored snapshot: one small head row and its product groups and runs in parts (contract K1).

calculated_snapshots.payload holds the head: every top-level key of the payload except products
and runs, plus "parts": {"products": n, "runs": m}, the number of rows of each kind. Each product
group is one 'product' row of calculated_snapshot_parts, and the runs, in order, fill 'runs' rows
of at most RUNS_PER_PART runs each, all in the build's LIST partition
(calculated_snapshot_parts_b<build_id>). A part's content_hash is policy.digest() of it, and the
snapshot's is content_hash() of the head's digest and the part digests (products, then runs, each
in position order). So no jsonb value nears PostgreSQL's limits (a container's elements may not
exceed 268,435,455 bytes, any value 1 GB): a part is one group and the head stays under
HEAD_BYTES. Builds stored before calculated v8 keep their single payload row (it has no "parts"
key); every reader here accepts both.

A payload v3 window references its list (slim.refer()); read(), export() and store.read_parts()
fill it back from calculated_list_rows (fill()), so every reader gets the v2 window.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from psycopg.rows import tuple_row

from ..names import NETWORK, PLACE
from .aggregate import BREAKDOWNS, iso, pool_lists, series
from .slim import POOL_LISTS

KINDS = {"products": "product", "runs": "runs"}
RUNS_PER_PART = 2000
HEAD_BYTES = 64 * 1024 * 1024
# A build's product groups (one per row) or runs (a list of up to RUNS_PER_PART per row) in
# payload order: PARTS reads its parts in their primary-key order, LEGACY the single payload of a
# build stored before calculated v8 (it has no parts partition, nor a database before v8 a parts
# table). product and cohort keep one product id's or cohort's groups.
FILTER = (
    "(%(product)s::text IS NULL OR {}=%(product)s) AND (%(cohort)s::text IS NULL OR {}=%(cohort)s)"
)
PARTS = (
    "SELECT {value} FROM (SELECT p.part AS value,p.position FROM calculated_snapshot_parts p "
    "WHERE p.build_id=%(build)s AND p.kind=%(kind)s AND "
    + FILTER.format("p.product_id::text", "p.cohort")
    + ") r ORDER BY r.position"
)
LEGACY = (
    "SELECT {value} FROM (SELECT e.value,e.n FROM calculated_snapshots s CROSS JOIN LATERAL "
    "jsonb_array_elements(s.payload->%(key)s) WITH ORDINALITY e(value,n) "
    "WHERE s.build_id=%(build)s AND "
    + FILTER.format("e.value->>'product_id'", "e.value->>'cohort'")
    + ") r ORDER BY r.n"
)
# Only the keys of each value listed in %(keys)s.
KEYS = (
    "(SELECT coalesce(jsonb_object_agg(k.key,k.value),'{}') FROM jsonb_each(r.value) k "
    "WHERE k.key=ANY(%(keys)s))"
)
# What the next calculation keeps of the previous build's groups: id and the score subtrees.
SCORES = (
    "jsonb_build_object('id',r.value->'id','windows',(SELECT coalesce(jsonb_object_agg(w.key,"
    "jsonb_build_object('scores',w.value->'scores','targets',(SELECT coalesce(jsonb_object_agg("
    "t.key,jsonb_build_object('score',t.value->'score')),'{}') FROM jsonb_each(w.value->'targets') "
    "t))),'{}') FROM jsonb_each(r.value->'windows') w))"
)


# The rows a v3 window keeps, from calculated_list_rows with their labels (%s: list ids,
# dimension codes, the rows kept by position and the pool list's rows kept, one entry per list
# and dimension). A pool list (POOL_DIMENSIONS) is picked by IPs from all of its dimension's
# rows; rank() keeps every row tied with its last, and aggregate.pool_lists() orders them.
ROW = (
    "{0}.list_id,{0}.dimension,{0}.position,{0}.place_id,{0}.asn_label_id,{0}.ips,"
    "{0}.observations,{0}.completed,{0}.successes,{0}.challenges,{0}.blocks,{0}.p50,{0}.p90,{0}.p99"
)
LIST_ROWS = (
    'WITH w(list_id,dimension,kept,top) AS (SELECT * FROM unnest(%s::bigint[],%s::"char"[],'
    "%s::integer[],%s::integer[])) SELECT r.list_id,r.dimension,r.position,p.country_code,"
    "p.country,p.region_code,p.region,p.city,a.asn,a.name,a.usage_type,r.ips,r.observations,"
    "r.completed,r.successes,r.challenges,r.blocks,r.p50,r.p90,r.p99 FROM (SELECT "
    + ROW.format("r")
    + " FROM w JOIN calculated_list_rows r USING(list_id,dimension) WHERE r.position<w.kept "
    "UNION SELECT "
    + ROW.format("q")
    + " FROM w CROSS JOIN LATERAL (SELECT q.*,rank() OVER (ORDER BY q.ips DESC) AS k "
    "FROM calculated_list_rows q WHERE (q.list_id,q.dimension)=(w.list_id,w.dimension) "
    "AND q.ips>0) q WHERE w.top>0 AND q.k<=w.top) r LEFT JOIN place_names p USING(place_id) "
    "LEFT JOIN asn_label_names a USING(asn_label_id) ORDER BY r.list_id,r.dimension,r.position"
)
# Each pool list's breakdown, as aggregate.pool_lists() picks it.
POOL_DIMENSIONS = {"geo": "city", "countries_breakdown": "country", "asns_breakdown": "asn"}
# A breakdown row's label fields (lists.FIELDS) and counters, as aggregate.breakdowns() writes it.
LABELS = {
    "country": PLACE[:2],
    "region": PLACE[:4],
    "city": PLACE,
    "asn": NETWORK,
    "country_asn": PLACE[:2] + NETWORK,
    "city_asn": PLACE + NETWORK,
}
COUNTERS = ("ips", "observations", "completed", "successes", "challenges", "blocks")
NAMES = {code: name for name, code in BREAKDOWNS.items()}


def jsonb_order(value):
    """value with its keys in jsonb's order (shorter first, then bytewise), as jsonb reads back."""
    return {k: value[k] for k in sorted(value, key=lambda k: (len(k.encode()), k.encode()))}


def content_hash(hashes):
    """The snapshot's hash: SHA-256 of its head digest followed by its part digests, in hex."""
    return hashlib.sha256("".join(hashes).encode()).hexdigest()


def chunks(runs):
    """The runs in parts of at most RUNS_PER_PART, in order."""
    return [runs[i : i + RUNS_PER_PART] for i in range(0, len(runs), RUNS_PER_PART)]


def head_text(head):
    """The head as stored, refused when it reaches HEAD_BYTES."""
    text = json.dumps(head, allow_nan=False)
    if len(text.encode()) >= HEAD_BYTES:
        raise ValueError(f"Snapshot head is {len(text.encode())} bytes, over {HEAD_BYTES}")
    return text


def assemble(head, products, runs):
    """The payload of a head (with "parts") and its parts, as one jsonb payload row reads back."""
    body = {k: v for k, v in head.items() if k != "parts"}
    return jsonb_order(body | {"products": products, "runs": runs})


def _row(name, found):
    """A breakdown row of list row found (LIST_ROWS columns from country_code), in jsonb order."""
    named = dict(zip((*PLACE, *NETWORK), found[:8], strict=True))
    counters = dict(zip((*COUNTERS, "p50", "p90", "p99"), found[8:], strict=True))
    rate = counters["successes"] / counters["completed"] if counters["completed"] else None
    return jsonb_order({k: named[k] for k in LABELS[name]} | counters | {"success_rate": rate})


def fill(conn, groups):
    """groups with every v3 window (it has "lists") back in its v2 form: its breakdowns and pool
    lists are the rows it keeps of its list, taken from calculated_list_rows in one query, in
    jsonb order as the v2 window read back. Raises ValueError when a list has fewer rows."""
    want = {}  # (list_id, dimension): [rows kept by position, rows of its pool list kept]
    for group in groups:
        for window in group.get("windows", {}).values():
            found = window.get("lists")
            if found is None:
                continue
            for name, code in BREAKDOWNS.items():
                entry = want.setdefault((found["id"], code), [0, 0])
                entry[0] = max(entry[0], found["entries"].get(name, 0))
            for pool, name in POOL_DIMENSIONS.items():
                entry = want[found["id"], BREAKDOWNS[name]]
                entry[1] = max(entry[1], found["entries"].get(pool, 0))
    if not want:
        return groups
    rows = {key: [] for key in want}
    columns = [list(column) for column in zip(*[(*k, *v) for k, v in want.items()], strict=True)]
    for found in conn.cursor(row_factory=tuple_row).execute(LIST_ROWS, columns):
        rows[found[0], found[1]].append(found[2:])
    return [_filled(group, rows) for group in groups]


def _filled(group, rows):
    windows = {}
    for label, window in group["windows"].items():
        if "lists" not in window:
            windows[label] = window
            continue
        list_id, kept = window["lists"]["id"], window["lists"]["entries"]
        full = {name: rows[list_id, code] for name, code in BREAKDOWNS.items()}
        breakdowns = {
            name: [_row(name, r[1:]) for r in full[name] if r[0] < kept.get(name, 0)]
            for name in BREAKDOWNS
        }
        picked = pool_lists(
            {name: [_row(name, r[1:]) for r in full[name]] for name in POOL_DIMENSIONS.values()}
        )
        lists = {
            name: [jsonb_order(r) for r in picked[name][: kept[name]]]
            for name in kept
            if name in POOL_LISTS
        }
        if any(
            len(found) != kept[name] for name, found in (breakdowns | lists).items() if name in kept
        ):
            raise ValueError(f"List {list_id} has fewer rows than its window keeps")
        window = {k: v for k, v in window.items() if k != "lists"}
        window["breakdowns"] = jsonb_order(breakdowns)
        window["pool"] = jsonb_order(window["pool"] | lists)
        windows[label] = jsonb_order(window)
    return group | {"windows": windows}


def _rows(conn, build_id, kind, value="r.value", named=False, **filters):
    """Each value in order: a cursor per query, server-side (a few rows at a time) if named."""
    params = {"build": int(build_id), "kind": KINDS[kind], "key": kind, "keys": None}
    params |= {"product": None, "cohort": None} | filters
    if params["keys"]:
        value, params["keys"] = KEYS, [*params["keys"]]
    split = conn.cursor(row_factory=tuple_row).execute(
        "SELECT to_regclass(%s) IS NOT NULL", (f"calculated_snapshot_parts_b{int(build_id)}",)
    )
    query = PARTS if split.fetchone()[0] else LEGACY
    with conn.cursor(name=f"snapshot_{kind}" if named else "", row_factory=tuple_row) as cursor:
        if named:
            cursor.itersize = 8
        cursor.execute(query.format(value=value), params)
        for (found,) in cursor:
            yield from found if kind == "runs" and query is PARTS else (found,)


def rows(conn, build_id, kind, product=None, cohort=None, keys=None):
    """A build's product groups ("products") or runs ("runs") in order, or None when the build
    has no snapshot (never stored, pruned or expired): no groups is not a missing build. product
    and cohort keep the groups of one product id or cohort; keys keeps only those keys of each
    group. Runs take none of them. Run it in one snapshot (REPEATABLE READ) for a consistent
    answer."""
    if kind == "runs" and (product or cohort or keys):
        raise ValueError("Runs are read whole: no product, cohort or keys")
    found = conn.cursor(row_factory=tuple_row).execute(
        "SELECT FROM calculated_snapshots WHERE build_id=%s", (int(build_id),)
    )
    if found.fetchone() is None:
        return None
    return list(_rows(conn, build_id, kind, product=product, cohort=cohort, keys=keys))


def stream(conn, build_id, kind):
    """rows(), fetched a few at a time through a server-side cursor."""
    return _rows(conn, build_id, kind, named=True)


def head(conn, build_id):
    """The stored head row's payload (the whole payload of a build before calculated v8)."""
    cursor = conn.cursor(row_factory=tuple_row)
    row = cursor.execute(
        "SELECT payload FROM calculated_snapshots WHERE build_id=%s", (int(build_id),)
    ).fetchone()
    return row[0] if row else None


def read(conn, build_id):
    """A build's whole stored payload, or None when it has no snapshot."""
    found = head(conn, build_id)
    if found is None or "parts" not in found:
        return found
    groups = fill(conn, rows(conn, build_id, "products"))
    return assemble(found, groups, rows(conn, build_id, "runs"))


def previous(conn):
    """The current build's as_of, methodology id and groups with only their id and scores (what
    inputs.previous_products() reads), projected in SQL; None without a current build."""
    found = conn.cursor(row_factory=tuple_row).execute(
        "SELECT build_id,payload->'as_of',payload->'methodology'->'id' FROM calculated_snapshots "
        "JOIN current_calculation USING(build_id)"
    )
    row = found.fetchone()
    if row is None:
        return None
    groups = list(_rows(conn, row[0], "products", value=SCORES))
    return {"as_of": row[1], "methodology": {"id": row[2]}, "products": groups}


def export(conn, build_id, out):
    """Write the payload to out exactly as json.dumps(payload, indent=2) followed by a newline,
    reading its parts a few at a time; returns its snapshot_id (None without a snapshot)."""
    found = head(conn, build_id)
    if found is None or "parts" not in found:
        if found is not None:
            out.write(json.dumps(found, indent=2, allow_nan=False) + "\n")
        return found and found["snapshot_id"]
    body = assemble(found, None, None)
    for i, key in enumerate(body):
        out.write(("," if i else "{") + "\n  " + json.dumps(key) + ": ")
        if key not in KINDS:
            out.write(json.dumps(body[key], indent=2, allow_nan=False).replace("\n", "\n  "))
            continue
        empty = True
        for value in stream(conn, build_id, key):
            value = fill(conn, [value])[0] if key == "products" else value
            text = json.dumps(value, indent=2, allow_nan=False).replace("\n", "\n    ")
            out.write(("[" if empty else ",") + "\n    " + text)
            empty = False
        out.write("[]" if empty else "\n  ]")
    out.write("\n}\n")
    return found["snapshot_id"]


def sparse(points, every=False):
    """A series without its empty points: buckets with no stored request and no availability
    (completed 0, every other value null; dense() restores them), unless every is set."""
    if every:
        return points
    return [p for p in points if p["unique_ips"] is not None or p["uptime"] is not None]


def dense(window):
    """window with every bucket of its series: a 24h series stored sparse (sparse()) gets back
    the empty points it left out, exactly as aggregate.series() writes them."""
    if window["period"]["hours"] != 24:
        return window
    start, end = (datetime.fromisoformat(window["period"][k]) for k in ("start", "end"))
    step = timedelta(minutes=5)
    timed = (window.get("latency") or {}).get("target")
    # The one empty point series() writes for a bucket without data; only "at" differs.
    empty = series([], [], start, start + step, 24, latency_target=timed)[0]
    empty = json.loads(json.dumps(empty, default=iso))
    empty = jsonb_order({k: jsonb_order(v) if isinstance(v, dict) else v for k, v in empty.items()})
    kept = {point["at"]: point for point in window["series"]}
    points, at = [], start
    while at < end:
        points.append(kept.get(iso(at)) or empty | {"at": iso(at)})
        at += step
    return window | {"series": points}
