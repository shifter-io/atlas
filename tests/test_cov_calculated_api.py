"""calculated/api.py without a database: the answers' query checks, the overlap filter, the
bounded caches, the refused snapshot ids, resources and live parameters, and serve()."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from atlas.calculated import api
from atlas.calculated.api import Cache, app, project

PERIOD = {"start": "2026-11-01T00:00:00+00:00", "end": "2026-11-02T00:00:00+00:00"}


def head():
    """A build's head with an overlap and a fleet list, and one product group."""
    return {
        "snapshot_id": "7",
        "as_of": PERIOD["end"],
        "methodology": {"id": "m", "name": "draft"},
        "readiness": "ready",
        "periods": {"24h": PERIOD, "7d": PERIOD, "30d": PERIOD},
        "products": [
            {
                "id": "g1",
                "product_id": 1,
                "provider": "p",
                "name": "residential",
                "kind": "residential",
                "cohort": "us",
                "criteria": {"country": "US"},
            }
        ],
        "overlap": [
            {"window": "24h", "cohort": "us", "product_a": 1, "product_b": 2},
            {"window": "24h", "cohort": "us", "product_a": 2, "product_b": 3},
            {"window": "24h", "cohort": "de", "product_a": 1, "product_b": 3},
            {"window": "7d", "cohort": "us", "product_a": 1, "product_b": 3},
        ],
        "fleet": [],
    }


def test_the_bare_snapshot_accepts_only_a_page_and_a_resource_only_its_filters():
    assert project(head(), None, {"page": "1"})["total"] == 1
    with pytest.raises(ValueError, match="Unknown query parameter"):
        project(head(), None, {"window": "24h"})
    query = {"window": "24h", "product": "1", "cohort": "us", "page": "1"}
    assert project(head(), "overlap", query)["total"] == 1
    with pytest.raises(ValueError, match="Unknown query parameter"):
        project(head(), "overlap", query | {"sort": "score"})


def test_overlap_rows_are_those_of_the_window_cohort_and_either_product():
    def pairs(query):
        rows = project(head(), "overlap", query)["data"]
        return [(r["product_a"], r["product_b"]) for r in rows]

    assert pairs({"window": "24h"}) == [(1, 2), (2, 3), (1, 3)]
    assert pairs({"window": "24h", "product": "3"}) == [(2, 3), (1, 3)]
    assert pairs({"window": "24h", "product": "3", "cohort": "us"}) == [(2, 3)]
    assert pairs({"window": "7d", "product": "2"}) == []


async def test_the_head_cache_starts_over_when_it_holds_64_builds(monkeypatch):
    calls = []

    def read(database, snapshot):
        calls.append(snapshot)
        return (int(snapshot), "hash" + snapshot)

    monkeypatch.setattr(api.store, "head", read)
    cache = Cache(clock=lambda: 0.0)
    for build in range(64):
        assert await cache.head(None, str(build)) == (build, f"hash{build}")
    assert len(cache.heads) == 64
    await cache.head(None, "64")
    assert list(cache.heads) == ["64"]
    # The cleared builds are read again; the kept one is not.
    await cache.head(None, "64")
    await cache.head(None, "0")
    assert calls[64:] == ["64", "0"]


async def test_the_answer_cache_keeps_the_newest_answers(monkeypatch):
    reads = []

    def read_head(database, snapshot):
        reads.append(snapshot)
        return head()

    monkeypatch.setattr(api.store, "read_head", read_head)
    monkeypatch.setattr(api, "ANSWERS", 2)
    cache = Cache()
    queries = [{"window": w} for w in ("24h", "7d", "30d")]
    bodies = [await cache.answer(None, 7, "overlap", q) for q in queries]
    assert [key[2] for key in cache.answers] == [(("window", "7d"),), (("window", "30d"),)]
    # A kept answer is the same body; the dropped one is rendered again from the cached head.
    assert await cache.answer(None, 7, "overlap", queries[1]) is bodies[1]
    assert await cache.answer(None, 7, "overlap", queries[0]) == bodies[0]
    assert reads == ["7"] and len(cache.answers) == 2


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(api.store, "head", lambda database, snapshot: (7, "hash"))
    monkeypatch.setattr(api.store, "read_head", lambda database, snapshot: head())
    async with TestClient(TestServer(app("unused"))) as client:
        yield client


@pytest.mark.parametrize("snapshot", ["latest", "-1", "1" * 19])
async def test_an_invalid_snapshot_id_is_refused(client, snapshot):
    response = await client.get(f"/v1/snapshots/{snapshot}/overlap")
    assert response.status == 400
    assert await response.text() == "Invalid snapshot id"


async def test_an_unknown_resource_is_not_found_and_a_known_one_answers(client):
    assert (await client.get("/v1/snapshots/7/everything")).status == 404
    assert (await client.get("/v1/unknown")).status == 404
    response = await client.get("/v1/snapshots/7/overlap?window=7d")
    assert response.status == 200
    assert response.headers["Cache-Control"] == "public,max-age=31536000,immutable"
    assert (await response.json())["total"] == 1


@pytest.mark.parametrize("path", ["/v1/traffic?test=pulse&since=2026", "/v1/traffic?page=1"])
async def test_traffic_refuses_an_unknown_parameter(client, path):
    response = await client.get(path)
    assert response.status == 400
    assert await response.text() == "Unknown query parameter"


def test_serve_runs_the_app_of_the_database(monkeypatch):
    calls = []
    monkeypatch.setattr(
        api.web, "run_app", lambda application, **options: calls.append((application, options))
    )
    api.serve("db.toml", "192.0.2.10", 9000)
    ((application, options),) = calls
    assert application[api.DATABASE] == "db.toml"
    assert isinstance(application[api.CACHE], Cache)
    assert options == {"host": "192.0.2.10", "port": 9000, "print": None}
