"""SNAP-LISTREF-1: payload v3, whose windows reference their lists instead of copying their rows.

Every v3 build is compared with a v2 build of the same inputs on the same PostgreSQL (the old
path: calculator.SCHEMA_VERSION 2 and parts holding the rows): read back, exported and served,
the two are byte-identical after removing the build's ids, clock and implementation and the
schema version. The fixtures are the seeded random observations of test_sqlwindow (three DB-IP
releases, unenriched IPs, runs split by window edges, two runs in one slice, an IP in two
countries, empty-string and NULL labels), the census-pulse scenario (scheduled groups), the
browser scenario of calculated_browser_cases (browser batches), and a late run with an empty
window.
"""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer
from calculated_browser_cases import add_browser_runs, ai_sources, generic  # noqa: F401 (fixture)
from calculated_cases import POLICY, lenient_policy, payload_mismatches, spread
from census_pulse_cases import outage
from census_pulse_cases import scenario as census_scenario
from test_calculated_slim import TINY
from test_sqlwindow import scenario  # noqa: F401 (fixture)

from atlas.browser import contracts
from atlas.calculated import calculator, parts, store
from atlas.calculated.aggregate import BREAKDOWNS
from atlas.calculated.api import RESOURCES, app
from atlas.calculated.calculator import calculate
from atlas.calculated.policy import PAYLOAD
from atlas.calculated.slim import POOL_LISTS, refer, slim
from atlas.storage import connect, ingest

pytest_plugins = ["test_storage", "calculated_template"]

# networks: a v4 head key, none in a v2 build (NET-SUM-1).
VOLATILE = {"snapshot_id", "generated_at", "implementation_id", "schema_version", "networks"}


def capped(tmp_path, policy=POLICY):
    """policy with every list but the country ones capped to one row, so lists are cut."""
    path = tmp_path / "capped.toml"
    caps = "".join(f"{name} = {n}\n" for name, n in TINY.items())
    path.write_text(policy.read_text() + f"\n[payload]\n{caps}")
    return path


def both(database, policy, as_of, monkeypatch):
    """A v3 build and a v2 build (the old path) of the same inputs, neither published."""
    new = calculate(database, policy, as_of=as_of)
    with monkeypatch.context() as old:
        old.setattr(calculator, "SCHEMA_VERSION", 2)
        old.setattr(calculator, "IMPLEMENTATION_ID", "payload-v2")
        old.setattr(store, "refer", lambda product, lists: product)
        legacy = calculate(database, policy, as_of=as_of)
    assert new["schema_version"] == 4 and legacy["schema_version"] == 2
    return new["snapshot_id"], legacy["snapshot_id"]


def without(value):
    return {k: v for k, v in value.items() if k not in VOLATILE}


def exported(database, build, path):
    store.export(database, path, build)
    return json.dumps(without(json.loads(path.read_text())), indent=2)


async def served(database, builds):
    """Every answer of the local API, of each build, in the order it was written."""
    answers = {build: [] for build in builds}
    paths = ["", *(f"/{r}?window={w}" for r in sorted(RESOURCES) for w in ("24h", "7d", "30d"))]
    async with TestClient(TestServer(app(database))) as client:
        for build in builds:
            for path in [*paths, "/breakdowns?window=30d&page=2"]:
                answer = await client.get(f"/v1/snapshots/{build}{path}")
                assert answer.status == 200, path
                answers[build].append(json.dumps(without(await answer.json())))
    return list(answers.values())


def stored_parts(database, build):
    with connect(database) as conn:
        found = conn.execute(
            "SELECT part FROM calculated_snapshot_parts WHERE build_id=%s AND kind='product' "
            "ORDER BY position",
            (int(build),),
        )
        groups = [row[0] for row in found]
        ids = dict(
            conn.execute(
                "SELECT (product_id,cohort,window_hours)::text,list_id FROM calculated_windows "
                "WHERE build_id=%s AND target=''",
                (int(build),),
            ).fetchall()
        )
    return groups, ids


def assert_equal_builds(database, tmp_path, new, legacy, *, cut=True):
    """new (v3) reads, exports and serves as legacy (v2) does, byte for byte. cut: some list is
    longer than its cap, so the v3 parts are smaller (a fixture with one exit IP has none)."""
    a, b = store.read(database, new), store.read(database, legacy)
    assert json.dumps(without(a)) == json.dumps(without(b))
    assert exported(database, new, tmp_path / "v3.json") == exported(
        database, legacy, tmp_path / "v2.json"
    )
    for p in b["products"]:
        found = store.read_parts(database, new, product=str(p["product_id"]), cohort=p["cohort"])
        assert json.dumps(found) == json.dumps([p])
    # Each v3 part holds its window's list reference, the v2 part the rows themselves.
    groups, ids = stored_parts(database, new)
    old, _ = stored_parts(database, legacy)
    truncated = 0
    for group, v2 in zip(groups, old, strict=True):
        for label, w in group["windows"].items():
            full = v2["windows"][label]
            key = f"({group['product_id']},{group['cohort']},{w['period']['hours']})"
            assert w["lists"]["id"] == ids[key]
            assert "breakdowns" not in w and not set(POOL_LISTS) & set(w["pool"])
            kept = {n: len(r) for n, r in full["breakdowns"].items()}
            kept |= {n: len(full["pool"][n]) for n in POOL_LISTS}
            assert w["lists"]["entries"] == kept
            assert w["breakdown_entries"] == full["breakdown_entries"]
            assert w["pool"]["list_entries"] == full["pool"]["list_entries"]
            truncated += any(kept[n] < full["breakdown_entries"][n] for n in BREAKDOWNS)
    assert bool(truncated) == cut
    # The parts no longer copy the lists' rows (with lists of one row or none, the reference is
    # about as long as the rows it replaces).
    assert not cut or sum(map(len, map(json.dumps, groups))) < sum(map(len, map(json.dumps, old)))
    return a


@pytest.mark.asyncio
async def test_v3_with_its_lists_equals_v2_on_seeded_random_observations(
    scenario,  # noqa: F811
    calculated_database,
    tmp_path,
    monkeypatch,
):
    # The scenario is test_sqlwindow's, in its calculated database; its synthetic observations
    # have no request counters (see test_sqlwindow).
    monkeypatch.setattr(store, "validate_payload", lambda payload, products=None: None)
    new, legacy = both(calculated_database, capped(tmp_path), scenario["as_of"], monkeypatch)
    payload = assert_equal_builds(calculated_database, tmp_path, new, legacy)
    # Empty-string labels come back as they were (the API and the export compare them above).
    windows = [w for p in payload["products"] for w in p["windows"].values()]
    rows = [r for w in windows for r in w["breakdowns"]["country"]]
    assert any(r["country_code"] == "" for r in rows)
    v3, v2 = await served(calculated_database, (new, legacy))
    assert v3 == v2


@pytest.mark.asyncio
async def test_v3_with_its_lists_equals_v2_with_scheduled_groups(database, tmp_path, monkeypatch):
    as_of, ids = await census_scenario(database, tmp_path)
    as_of = datetime.fromtimestamp(as_of.timestamp() // 300 * 300, UTC)
    with connect(database) as conn:
        outage(conn, ids["alpha"], as_of - timedelta(hours=6), as_of - timedelta(hours=3))
    policy = capped(tmp_path, lenient_policy(tmp_path))
    new, legacy = both(database, policy, as_of, monkeypatch)
    payload = assert_equal_builds(database, tmp_path, new, legacy)
    assert any(p["criteria"].get("schedule") == "census_pulse" for p in payload["products"])
    assert payload_mismatches(database, int(new)) == []
    # Every serve answer, the breakdowns pages included, is the v2 build's.
    v3, v2 = await served(database, (new, legacy))
    assert v3 == v2
    # A v3 draft publishes (its stored groups validate) and serves as current.
    store.publish(database, new)
    async with TestClient(TestServer(app(database))) as client:
        current = await (await client.get("/v1/breakdowns?window=24h")).json()
        old = await (await client.get(f"/v1/snapshots/{legacy}/breakdowns?window=24h")).json()
    assert json.dumps(without(current)) == json.dumps(without(old)) and current["data"]


@pytest.mark.asyncio
async def test_v3_with_its_lists_equals_v2_with_browser_runs(
    database,
    tmp_path,
    monkeypatch,
    ai_sources,  # noqa: F811
):
    as_of = await generic(database, tmp_path)
    await add_browser_runs(database, tmp_path)
    new, legacy = both(database, capped(tmp_path, lenient_policy(tmp_path)), as_of, monkeypatch)
    payload = assert_equal_builds(database, tmp_path, new, legacy)
    # Every product's windows hold the browser targets (read back in jsonb key order).
    for p in payload["products"]:
        assert set(contracts.BROWSER_TARGETS) <= set(p["windows"]["24h"]["targets"])
    v3, v2 = await served(database, (new, legacy))
    assert v3 == v2


@pytest.mark.asyncio
async def test_v3_with_its_lists_equals_v2_with_a_late_run_and_an_empty_window(
    database, tmp_path, monkeypatch
):
    """A run that ends after the cutoff (its buckets at +0, +5 and +10 minutes are in, +30 is
    not) and a product whose 24h window has no row (its one run is three days old)."""
    store.initialize(database)
    w = datetime(2026, 10, 1, 12, tzinfo=UTC)
    minute = timedelta(minutes=1)
    late = await spread(tmp_path, monkeypatch, w + minute, [0, 300, 600, 1800])
    old = await spread(tmp_path, monkeypatch, w - timedelta(days=3), [0], provider="second")
    for directory in (late, old):
        ingest(directory, database)
    new, legacy = both(database, capped(tmp_path), w + 20 * minute, monkeypatch)
    payload = assert_equal_builds(database, tmp_path, new, legacy, cut=False)
    windows = {p["provider"]: p["windows"] for p in payload["products"]}
    assert windows["fixture"]["24h"]["metrics"]["completed"] == 3
    empty = windows["second"]["24h"]
    assert empty["metrics"]["completed"] == 0 and empty["pool"]["unique_ips"] == 0
    assert not any(empty["breakdowns"].values())
    assert windows["second"]["7d"]["metrics"]["completed"] == 1
    v3, v2 = await served(database, (new, legacy))
    assert v3 == v2


@pytest.mark.asyncio
async def test_a_stored_v3_window_is_validated_and_needs_all_of_its_rows(
    database, tmp_path, monkeypatch
):
    as_of, _ = await census_scenario(database, tmp_path)
    as_of = datetime.fromtimestamp(as_of.timestamp() // 300 * 300, UTC)
    build = calculate(database, capped(tmp_path, lenient_policy(tmp_path)), as_of=as_of)
    build_id = int(build["snapshot_id"])
    with connect(database) as conn:
        head, groups = parts.head(conn, build_id), parts.rows(conn, build_id, "products")
        store.validate_payload(head, groups)
        filled = parts.fill(conn, groups)
        # A group whose windows are already v2 (or a v2 build) reads unchanged, with no query.
        assert parts.fill(conn, filled) == filled == build["products"]
        mixed = groups[0] | {"windows": groups[0]["windows"] | {"30d": filled[0]["windows"]["30d"]}}
        assert parts.fill(conn, [mixed]) == [filled[0]]
    for bad in ({"id": "1", "entries": {}}, {"id": 1, "entries": {"city": -1}}, {"id": 1}):
        broken = copy.deepcopy(groups)
        broken[0]["windows"]["24h"]["lists"] = bad
        with pytest.raises(ValueError, match="Invalid snapshot contract"):
            store.validate_payload(head, broken)
    broken = copy.deepcopy(groups)
    del broken[0]["windows"]["24h"]["lists"]
    with pytest.raises(ValueError, match="Invalid snapshot contract"):
        store.validate_payload(head, broken)
    # A list's rows are checked before they are inserted: write() validates the whole group.
    whole = copy.deepcopy(filled)
    row = whole[0]["windows"]["24h"]["breakdowns"]["country"][0]
    row["successes"] = row["completed"] + 1
    with pytest.raises(ValueError, match="Breakdown counters do not reconcile"):
        store.validate_payload(head, whole)
    # A list that lost a row it is referenced for fails the read.
    window = groups[0]["windows"]["24h"]
    with connect(database) as conn:
        conn.execute(
            "DELETE FROM calculated_list_rows WHERE list_id=%s AND dimension='c' AND position=0",
            (window["lists"]["id"],),
        )
        with pytest.raises(ValueError, match="fewer rows than its window keeps"):
            parts.fill(conn, groups)
        conn.rollback()


def test_refer_names_the_list_and_how_many_rows_of_each_list_it_keeps():
    rows = [{"country_code": "DE", "ips": 3 - i, "completed": 3 - i} for i in range(3)]
    window = {
        "period": {"hours": 24},
        "breakdowns": {name: rows for name in BREAKDOWNS},
        "pool": {"unique_ips": 3} | {name: rows for name in POOL_LISTS},
    }
    group = slim({"products": [{"id": "1:c", "windows": {"24h": window}}]}, {})["products"][0]
    small = slim({"products": [group]}, {"payload": {"city": 1, "geo": 2}})["products"][0]
    referenced = refer(small, {"24h": 7})["windows"]["24h"]
    assert referenced["lists"] == {
        "id": 7,
        "entries": {name: 3 for name in BREAKDOWNS}
        | {"city": 1}
        | {name: min(3, PAYLOAD[name]) for name in POOL_LISTS}
        | {"geo": 2},
    }
    assert "breakdowns" not in referenced and referenced["breakdown_entries"]["city"] == 3
    assert referenced["pool"] == {"unique_ips": 3, "list_entries": dict.fromkeys(POOL_LISTS, 3)}
    assert window["breakdowns"]["city"] == rows  # the input is not changed
