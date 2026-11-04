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
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from psycopg.rows import tuple_row

from .aggregate import iso, series

KINDS = {"products": "product", "runs": "runs"}
RUNS_PER_PART = 2000
HEAD_BYTES = 64 * 1024 * 1024
# A build's product groups (one per row) or runs (a list of up to RUNS_PER_PART per row) in
# payload order: PARTS reads its parts in their primary-key order, LEGACY the single payload of a
# build stored before calculated v8. product and cohort keep one product id's or cohort's groups.
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
    "WHERE s.build_id=%(build)s AND NOT s.payload ? 'parts' AND "
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
    """The payload of a head and its parts, as one jsonb payload row reads back; a head without
    "parts" (a build stored before calculated v8) is the whole payload already."""
    if "parts" not in head:
        return head
    body = {k: v for k, v in head.items() if k != "parts"}
    return jsonb_order(body | {"products": products, "runs": runs})


def _rows(conn, build_id, kind, value="r.value", named=False, **filters):
    """Each value in order: a cursor per query, server-side (a few rows at a time) if named."""
    params = {"build": int(build_id), "kind": KINDS[kind], "key": kind, "keys": None}
    params |= {"product": None, "cohort": None} | filters
    if params["keys"]:
        value, params["keys"] = KEYS, [*params["keys"]]
    for query in (PARTS, LEGACY):
        with conn.cursor(name=f"snapshot_{kind}" if named else "", row_factory=tuple_row) as cursor:
            if named:
                cursor.itersize = 8
            cursor.execute(query.format(value=value), params)
            for (found,) in cursor:
                yield from found if kind == "runs" and query is PARTS else (found,)


def rows(conn, build_id, kind, product=None, cohort=None, keys=None):
    """A build's product groups ("products") or runs ("runs") in order. product and cohort
    keep the groups of one product id or cohort; keys keeps only those keys of each value."""
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
    return assemble(found, rows(conn, build_id, "products"), rows(conn, build_id, "runs"))


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
            text = json.dumps(value, indent=2, allow_nan=False).replace("\n", "\n    ")
            out.write(("[" if empty else ",") + "\n    " + text)
            empty = False
        out.write("[]" if empty else "\n  ]")
    out.write("\n}\n")
    return found["snapshot_id"]


def dense(window):
    """window with every bucket of its series: a 24h series stored sparse (aggregate.series(
    sparse=True)) gets back the empty points it left out, exactly as series() writes them."""
    if window["period"]["hours"] != 24:
        return window
    start, end = (datetime.fromisoformat(window["period"][k]) for k in ("start", "end"))
    timed = (window.get("latency") or {}).get("target")
    kept = {point["at"]: point for point in window["series"]}
    points = [
        kept.get(iso(point["at"])) or _empty(point)
        for point in series([], [], start, end, 24, latency_target=timed)
    ]
    return window | {"series": points}


def _empty(point):
    point = json.loads(json.dumps(point, default=iso))
    return jsonb_order({k: jsonb_order(v) if isinstance(v, dict) else v for k, v in point.items()})
