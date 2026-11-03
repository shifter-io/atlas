import json

import pytest

from proxy_index.calculated import api
from proxy_index.calculated.answers import MAX_ANSWER_BYTES, bounds, size
from proxy_index.calculated.api import RESOURCES, Cache, project

WINDOWS = {"24h": 24, "7d": 168, "30d": 720}
# A QA-1-sized daily build per network: one product group per requested country, each window
# with the capped lists a published snapshot keeps (country up to 1000, 25 of the others).
NETWORKS, COUNTRIES, EXITS = 18, 8, 240


def place_rows(n, **label):
    return [
        label
        | {"country_code": f"C{i:03d}", "ips": n - i, "completed": 3 * (n - i), "p50": 400 + i}
        for i in range(n)
    ]


def window(hours):
    lists = {"country": EXITS, "region": 25, "city": 25, "asn": 25, "country_asn": 25}
    points = 288 if hours == 24 else hours // 24
    return {
        "period": {"hours": hours},
        "readiness": "ready",
        "metrics": {"completed": 1000, "successes": 900},
        "scores": {"default": {"score": 71.5, "rank": 3, "components": {"speed": {"value": 0.9}}}},
        "targets": {"generic_http": {"metrics": {"completed": 1000}}},
        "series": [{"at": f"t{i}", "completed": 3, "successes": 2} for i in range(points)],
        "breakdowns": {name: place_rows(n) for name, n in lists.items()},
        "breakdown_entries": {name: n * 10 for name, n in lists.items()},
        "pool": {
            "unique_ips": 5000,
            "geo": place_rows(50),
            "countries_breakdown": place_rows(EXITS),
        },
    }


def daily_payload():
    products = [
        {
            "id": f"{n}:{c}",
            "product_id": n * 100 + c,
            "provider": f"net{n:02d}",
            "name": f"Net {n:02d}",
            "kind": "residential",
            "cohort": f"daily-{c:03d}",
            "criteria": {"country": f"C{c:03d}"},
            "windows": {label: window(hours) for label, hours in WINDOWS.items()},
        }
        for n in range(NETWORKS)
        for c in range(COUNTRIES)
    ]
    periods = {label: {"hours": hours} for label, hours in WINDOWS.items()}
    return {
        "snapshot_id": "7",
        "schema_version": 2,
        "as_of": "2026-11-03T04:10:00+00:00",
        "readiness": "partial",
        "methodology": {"id": "m1", "settings": {"weights": {"speed": 1}}},
        "periods": periods,
        "products": products,
        "fleet": [{"window": w, "cohort": p["cohort"]} for p in products for w in WINDOWS],
        "overlap": [],
        "targets": ["generic_http"],
        "runs": [{"run_key": i, "status": "complete"} for i in range(850)],
    }


@pytest.fixture(scope="module")
def payload():
    return daily_payload()


def test_the_payload_is_too_large_to_send_whole(payload):
    assert size(payload) > 20 * MAX_ANSWER_BYTES


def every_page(payload, resource, query):
    first = project(payload, resource, query)
    if "pages" not in first:
        return first, [first]
    pages = [first] + [
        project(payload, resource, query | {"page": str(n)}) for n in range(2, first["pages"] + 1)
    ]
    return first, pages


def test_every_answer_and_page_fits_the_cap_and_the_pages_hold_every_row(payload):
    first, pages = every_page(payload, None, {})
    assert first["products"] == NETWORKS * COUNTRIES and "windows" not in json.dumps(first["data"])
    assert [r["id"] for p in pages for r in p["data"]] == [p["id"] for p in payload["products"]]
    for resource in sorted(RESOURCES):
        for label in WINDOWS:
            first, pages = every_page(payload, resource, {"window": label})
            rows = [r for p in pages for r in p["data"]] if isinstance(first["data"], list) else []
            if isinstance(first["data"], list):
                assert len(rows) == first["total"], (resource, label)
            for p in pages:
                assert size(p) <= MAX_ANSWER_BYTES, (resource, label, p["page"], size(p))


def test_a_page_past_the_end_is_empty_and_a_bad_page_is_refused(payload):
    last = project(payload, "products", {"window": "30d", "page": "100000"})
    assert last["data"] == [] and last["total"] == NETWORKS * COUNTRIES
    for bad in ("0", "-1", "x", "100001"):
        with pytest.raises(ValueError):
            project(payload, "scores", {"page": bad})
    with pytest.raises(ValueError):
        project(payload, None, {"window": "30d"})


def test_bounds_keep_whole_rows_and_give_a_large_row_its_own_page():
    rows = [{"x": "a" * 10}, {"x": "b" * 500}, {"x": "c"}, {"x": "d"}]
    cuts = bounds(rows, 100)
    assert cuts == [(0, 1), (1, 2), (2, 4)]
    assert bounds([], 100) == [(0, 0)]


async def test_requests_do_no_database_work_once_cached(monkeypatch, payload):
    calls = {"head": 0, "read": 0}
    now = [0.0]

    def head(database, snapshot):
        calls["head"] += 1
        return (7, "hash")

    def read(database, snapshot):
        calls["read"] += 1
        return payload

    monkeypatch.setattr(api.store, "head", head)
    monkeypatch.setattr(api.store, "read", read)
    cache = Cache(clock=lambda: now[0])
    for _ in range(3):
        token = await cache.head(None, "current")
        body = await cache.answer(None, token[0], "scores", {"window": "7d"})
    assert calls == {"head": 1, "read": 1} and len(body) <= MAX_ANSWER_BYTES
    await cache.answer(None, 7, "scores", {"window": "24h"})
    assert calls["read"] == 1
    now[0] += api.HEAD_SECONDS + 1
    await cache.head(None, "current")
    assert calls["head"] == 2
