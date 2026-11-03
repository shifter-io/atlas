"""Small answers from a published snapshot: the API never sends a whole payload.

A daily build publishes one product group per network and requested country, so its payload
runs to hundreds of MB (179 MB for 18 networks x 40 countries). Every answer of the API is at
most MAX_ANSWER_BYTES of JSON: the bare snapshot is its summary (ids, times, readiness,
methodology, periods) with one page of the product-group index, and every list resource is cut
into pages that each fit the cap. A page holds whole rows; a single row larger than the cap
(a product group window with a very long breakdown) is a page of its own.
"""

from __future__ import annotations

import json

MAX_ANSWER_BYTES = 200 * 1024
INDEX_PAGE = 50
SUMMARY = (
    "snapshot_id",
    "schema_version",
    "implementation_id",
    "as_of",
    "generated_at",
    "readiness",
    "refresh_seconds",
    "resolution_seconds",
    "methodology",
    "periods",
    "dbip_release",
)


def size(value) -> int:
    """Bytes of value as the API writes it."""
    return len(json.dumps(value, separators=(",", ":"), allow_nan=False, default=str))


def summary(payload: dict, page: int) -> dict:
    """The bare snapshot answer: its summary and one page of its product groups."""
    index = [
        {
            "id": p["id"],
            "product_id": p["product_id"],
            "provider": p["provider"],
            "name": p["name"],
            "kind": p["kind"],
            "cohort": p["cohort"],
            "country": (p.get("criteria") or {}).get("country"),
        }
        for p in payload["products"]
    ]
    head = {k: payload[k] for k in SUMMARY if k in payload} | {"products": len(index)}
    start = (page - 1) * INDEX_PAGE
    return head | {
        "page": page,
        "pages": max(1, -(-len(index) // INDEX_PAGE)),
        "total": len(index),
        "data": index[start : start + INDEX_PAGE],
    }


def bounds(rows: list, budget: int) -> list[tuple[int, int]]:
    """Page boundaries over rows: consecutive runs whose JSON fits budget (at least one row)."""
    out, start, used = [], 0, 2
    for i, row in enumerate(rows):
        n = size(row) + 1
        if i > start and used + n > budget:
            out.append((start, i))
            start, used = i, 2
        used += n
    out.append((start, len(rows)))
    return out


def paged(envelope: dict, rows: list, page: int) -> dict:
    """envelope with page `page` of rows, each page within MAX_ANSWER_BYTES."""
    budget = MAX_ANSWER_BYTES - size(envelope) - 128
    cuts = bounds(rows, budget)
    start, end = cuts[page - 1] if page <= len(cuts) else (len(rows), len(rows))
    pages = {"page": page, "pages": len(cuts), "total": len(rows)}
    return envelope | pages | {"data": rows[start:end]}
