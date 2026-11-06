"""NET-SUM-1: network summaries in the head of payload v4 (contract K9), PostgreSQL 17.

The 3-day fixture (test_calculated_incremental.history()) has four networks: alpha (two daily
countries, US and DE, plus a census-pulse group and a browser run), beta (one daily group),
gamma (one group with empty windows) and delta (a daily and a census-pulse group). Its 70
synthetic IPs are drawn at random per bucket, so alpha's groups share IPs and places.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from calculated_cases import lenient_policy, payload_mismatches
from test_calculated_hourly_reads import HOUR, floor_hour
from test_calculated_incremental import history, without
from test_calculated_listref import served
from test_calculated_listref import without as stable_head
from test_calculated_netlists import network_lists
from test_scale_gen import POLICY as SCALE_POLICY
from test_scale_gen import run as generate_scale

from proxy_index.browser import contracts
from proxy_index.calculated import browser, calculator, netlists, network, store
from proxy_index.calculated.aggregate import timed
from proxy_index.calculated.calculator import calculate
from proxy_index.calculated.latency import KEYS
from proxy_index.calculated.parts import dense
from proxy_index.calculated.policy import latency_target, slot_settings
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

WINDOW_KEYS = {
    "period",
    "readiness",
    "metrics",
    "uptime",
    "tests",
    "pool",
    "targets",
    "series",
    "success_delta",
    "scores",
    "overlap",
    "lists",
    "breakdown_entries",
}
OVERLAP = {"exclusive_share", "shared_share", "compared_products", "peers", "as_of"}
# Distinct IPs of a provider's runs (all of one kind here) in a window.
DISTINCT = (
    "SELECT count(DISTINCT o.ip_id) FROM ip_observations o JOIN test_runs r USING(run_key) "
    "JOIN scope_stats s USING(scope_id) JOIN products p USING(product_id) "
    "WHERE p.provider=%s AND o.bucket_start>=%s AND o.bucket_start<%s"
)


def stable(networks):
    """networks (JSON values) without their K10 list ids: each build writes new lists."""
    return without({"networks": networks})["networks"]


def by_provider(payload):
    nets = {n["provider"]: n for n in payload["networks"]}
    groups = {}
    for g in payload["products"]:
        groups.setdefault(g["provider"], []).append(g)
    return nets, groups


def capture_inputs(monkeypatch):
    """Record what build() hands network.summaries()."""
    seen, original = {}, network.summaries

    def wrapped(fleet_groups, bucket_groups, by_product, flagged, as_of, config):
        seen.update(buckets=bucket_groups, by_product=by_product, flagged=flagged, config=config)
        return original(fleet_groups, bucket_groups, by_product, flagged, as_of, config)

    monkeypatch.setattr(network, "summaries", wrapped)
    return seen


def assert_one_group(net, group):
    """A network of one group equals it on every K9 key; cohort, id and lists are the
    network's, the pool's place counts are its K10 list's, and readiness, scores and overlap are
    against the networks of its kind (test_calculated_network_scores.py)."""
    assert net["criteria"]["groups"] == 1 and net["product_id"] == group["product_id"]
    assert (net["pricing"], net["evidence"]) == (
        group["pricing"],
        group["windows"]["30d"]["evidence"],
    )
    for label, w in net["windows"].items():
        g = dense(group["windows"][label])
        assert set(w) - {"estimated"} == WINDOW_KEYS
        for key in ("period", "metrics", "uptime", "tests", "success_delta", "series"):
            assert w[key] == g[key], (label, key)
        score = w["scores"]["balanced"]
        assert set(w["scores"]) == {"balanced"} and set(w["overlap"]) == OVERLAP
        assert w["readiness"] == (
            "ready"
            if score["state"] == "ready"
            else "partial"
            if g["metrics"]["completed"]
            else "unavailable"
        )
        assert w["targets"] == {
            t: {"metrics": v["metrics"], "readiness": v["readiness"]}
            | ({"browser": v["browser"]} if "browser" in v else {})
            for t, v in g["targets"].items()
        }
        for key in (*network.SHARES, "unique_ips", "new_ips", "churn_rate", "observations"):
            assert w["pool"][key] == g["pool"][key], (label, key)
        assert {k: w["pool"]["daily"][k] for k in ("days", "unique_ips")} == {
            k: g["pool"]["daily"][k] for k in ("days", "unique_ips")
        }
        entries = (w["lists"] or {"entries": {}})["entries"]
        assert w["breakdown_entries"] == w["pool"]["list_entries"] == entries
        for name, dimension in network.PLACES.items():
            assert w["pool"][name] == w["pool"]["daily"][name] == entries.get(dimension)
        if "estimated" in g:
            e, f = w["estimated"], g["estimated"]
            assert (
                e["unique_ips"] == f["unique_ips"] and e["breakdown_scale"] == f["breakdown_scale"]
            )
            assert e["series"] == [
                {k: p[k] for k in ("start", "unique_ips", "requests")} for p in f["series"]
            ]


def assert_three_groups(database, net, groups, seen):
    """alpha: requests and tests summed, a shared IP counted once, latency timed() over the
    union's rows, uptime from the census-pulse group only, estimates summed."""
    assert len(groups) == 3 and net["criteria"]["requested_countries"] == ["DE", "US"]
    (scheduled,) = [g for g in groups if g["criteria"]["country"] is None]
    config = seen["config"]
    minimum, target = slot_settings(config)["min_latency_successes"], latency_target(config)
    pulses = seen["by_product"][1][scheduled["product_id"]]
    slots = network.slot_runs(
        pulses, scheduled["scoring"]["slots"]["minutes"] * 60, seen["flagged"]
    )
    rows = [r for g in groups for r in seen["buckets"][g["product_id"], g["cohort"]]]
    rows = browser.split(rows)[0]
    for label, w in net["windows"].items():
        windows = [g["windows"][label] for g in groups]
        since, end = (datetime.fromisoformat(w["period"][k]) for k in ("start", "end"))
        m = w["metrics"]
        assert m["completed"] == sum(x["metrics"]["completed"] for x in windows)
        assert m["successes"] == sum(x["metrics"]["successes"] for x in windows)
        for test, n in w["tests"].items():
            assert n == sum(x["tests"][test] for x in windows)
        with connect(database) as conn:
            distinct = conn.execute(DISTINCT, ("alpha", since, end)).fetchone()[0]
        assert w["pool"]["unique_ips"] == distinct
        assert distinct <= sum(x["pool"]["unique_ips"] for x in windows)
        if label == "30d":  # the groups share IPs: each is counted once
            assert 0 < distinct < sum(x["pool"]["unique_ips"] for x in windows)
        inside = [r for r in rows if since <= r["bucket_start"] < end]
        now = [d for d in slots if since <= d["scheduled_for"] < end]
        expected = timed(inside, w["period"]["hours"], minimum, now, m["time_slots"]["minutes"])
        expected = json.loads(json.dumps(expected, default=str))
        for key in ("latency_ms", "success_rate", "request_success_rate"):
            assert m[key] == pytest.approx(expected[key], rel=1e-12), (label, key)
        assert m["time_slots"] == expected["time_slots"]
        assert target in m["time_slots"]["latency_targets"] or m["latency_ms"]["p50"] is None
        # The head's latency objects carry the mean, other targets' included (all None there).
        assert all(set(t["metrics"]["latency_ms"]) == set(KEYS) for t in w["targets"].values())
        assert w["uptime"] == scheduled["windows"][label]["uptime"]
        blocks = [x["estimated"] for x in windows if "estimated" in x]
        assert blocks and w["estimated"]["unique_ips"]["value"] == pytest.approx(
            sum(b["unique_ips"]["value"] for b in blocks), rel=1e-12
        )
        assert w["estimated"]["breakdown_scale"] == pytest.approx(
            w["estimated"]["unique_ips"]["value"] / distinct, rel=1e-12
        )
        # The browser run counts per target only (the BP-CALC rule).
        assert "browser" in w["targets"][contracts.PAGE_TARGETS[0]]


@pytest.mark.asyncio
async def test_networks_follow_the_group_rules_over_the_union(database, tmp_path, monkeypatch):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    seen = capture_inputs(monkeypatch)
    new = calculate(database, policy, as_of=end, mode="incremental")
    full = calculate(database, policy, as_of=end, mode="full")
    payload = store.read(database, new["snapshot_id"])
    assert payload["schema_version"] == 4
    nets, groups = by_provider(payload)
    assert [n["id"] for n in payload["networks"]] == sorted(n["id"] for n in payload["networks"])
    assert set(nets) == {"alpha", "beta", "gamma", "delta"}
    for provider in ("beta", "gamma"):
        assert_one_group(nets[provider], groups[provider][0])
    assert nets["beta"]["windows"]["30d"]["metrics"]["completed"] > 0
    assert_three_groups(database, nets["alpha"], groups["alpha"], seen)
    assert nets["delta"]["criteria"] == {
        "country": None,
        "requested_countries": ["US"],
        "groups": 2,
    }
    # Each window names its K10 list; the incremental build equals the full one.
    lists = network_lists(database, new["snapshot_id"])
    for net in payload["networks"]:
        for w in net["windows"].values():
            assert (net["product_id"], w["period"]["hours"]) in lists
    other = stable(store.read(database, full["snapshot_id"])["networks"])
    assert stable(payload["networks"]) == other
    assert stable(json.loads(json.dumps(new["networks"], default=str))) == other
    assert payload_mismatches(database, int(new["snapshot_id"])) == []


@pytest.mark.asyncio
async def test_v4_changes_nothing_but_the_head_networks(database, tmp_path, monkeypatch):
    """The regression: a v3 build (main) has no networks; v4 adds one per network and leaves
    the product parts, group lists, typed rows and serve answers byte-identical."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    with monkeypatch.context() as old:
        old.setattr(calculator, "SCHEMA_VERSION", 3)
        before = calculate(database, policy, as_of=end, mode="incremental")
    assert "networks" not in store.read_head(database, before["snapshot_id"])
    old_capture = await published(database, before["snapshot_id"])
    with connect(database) as conn:
        store.discard(conn, int(before["snapshot_id"]))
    after = calculate(database, policy, as_of=end, mode="incremental")
    head = store.read_head(database, after["snapshot_id"])
    assert len(head["networks"]) == 4
    assert await published(database, after["snapshot_id"]) == old_capture


async def published(database, build):
    """A build's payload without its networks, typed windows, group lists and serve answers."""
    with connect(database) as conn:
        windows = conn.execute(
            "SELECT to_jsonb(w)-'result_id'-'build_id'-'list_id',l.content_hash,l.entries "
            "FROM calculated_windows w LEFT JOIN calculated_lists l USING(list_id) "
            "WHERE w.build_id=%s ORDER BY w.product_id,w.cohort,w.target,w.window_hours",
            (int(build),),
        ).fetchall()
        groups = conn.execute(
            "SELECT product_id,cohort,window_hours,content_hash,entries FROM calculated_lists "
            "WHERE cohort<>'network' ORDER BY 1,2,3,4"
        ).fetchall()
    payload = stable_head(store.read(database, str(build)))
    payload.pop("networks", None)
    return json.dumps(payload), windows, groups, await served(database, [build])


def test_a_network_window_is_validated():
    window = {
        "metrics": {"completed": 2, "successes": 1, "errors": 1, "uptime": 1.0}
        | {"challenge_rate": 0.0, "block_rate": 0.0},
        "series": [{"uptime": None}],
        "targets": {},
        "pool": {"unique_ips": 1, "observations": 1},
        "lists": {"id": 7, "entries": {"country": 1}},
    }
    payload = {"schema_version": 4, "networks": [{"cohort": "network", "windows": {"24h": window}}]}
    network.validate(payload)
    for change in (
        {"series": [{"uptime": 2}]},
        {"metrics": window["metrics"] | {"errors": 2}},
        {"pool": {"unique_ips": None, "observations": 1}},
        {"lists": {"id": "7", "entries": {}}},
        {"_runs": [1]},
        {"_lead": {}},
    ):
        bad = {
            "schema_version": 4,
            "networks": [{"cohort": "network", "windows": {"24h": window | change}}],
        }
        with pytest.raises(ValueError):
            network.validate(bad)
    for entry in ({"cohort": "group", "windows": {}}, {"cohort": "network", "windows": {"1h": {}}}):
        with pytest.raises(ValueError, match="Invalid network summary"):
            network.validate({"schema_version": 4, "networks": [entry]})
    network.validate({"schema_version": 3, "networks": [{"cohort": "group"}]})


def test_pool_shares_are_weighted_by_unique_ips():
    pools = [
        {"geo_accuracy": 0.5, "unique_ips": 10},
        {"geo_accuracy": 1.0, "unique_ips": 30},
        {"geo_accuracy": None, "unique_ips": 99},
    ]
    assert network.weighted(pools, "geo_accuracy") == pytest.approx(0.875)
    assert network.weighted(pools[1:], "geo_accuracy") == 1.0
    assert network.weighted(pools[2:], "geo_accuracy") is None
    assert network.weighted([{"geo_accuracy": 0.5, "unique_ips": 0}] * 2, "geo_accuracy") is None


def test_the_lead_group_is_the_headline_products_scheduled_group():
    def group(gid, product, completed):
        return {
            "id": gid,
            "product_id": product,
            "windows": {"30d": {"metrics": {"completed": completed}}},
        }

    groups = [group("2:a", 2, 5), group("2:b", 2, 1), group("3:a", 3, 6)]
    assert netlists.headline(groups) == 2  # 6 requests against 6: the lowest id
    assert network.lead(groups, set())["id"] == "2:a"
    assert network.lead(groups, {"2:b"})["id"] == "2:b"


def test_estimates_are_summed_per_hour():
    hour = datetime(2026, 11, 1, tzinfo=UTC)

    def block(value, points):
        return {
            "formula_versions": [1],
            "hours": len(points),
            "unique_ips": {
                "value": value,
                "estimated": True,
                "formula_version": 1,
                "method": "distinct",
            },
            "requests": {"value": 10, "estimated": True, "formula_version": 1},
            "series": [
                {"start": hour + timedelta(hours=h), "unique_ips": n, "requests": 5}
                | {"estimated": True, "formula_version": 1}
                for h, n in points
            ],
        }

    found = network.estimates(
        [block(4.0, [(0, 2), (1, 2)]), block(6.0, [(1, 6)])], {"unique_ips": 5}
    )
    assert found["unique_ips"]["value"] == 10.0 and found["breakdown_scale"] == 2.0
    assert found["requests"]["value"] == 20 and found["hours"] == 3
    assert found["series"] == [
        {"start": hour, "unique_ips": 2, "requests": 5},
        {"start": hour + HOUR, "unique_ips": 8, "requests": 10},
    ]
    assert network.estimates([block(1.0, [])], {"unique_ips": 0})["breakdown_scale"] is None


def test_the_scale_fixture_head_stays_small(databases):
    """The scripts/scale fixture at --scale 0.0625 (two networks, five countries, two days):
    at most 0.2 MB of head per network."""
    config = databases()
    generate_scale(config, "1..2", countries="AQ,BR,DE,FR,US", scale=0.0625)
    store.initialize(config)
    payload = calculate(config, SCALE_POLICY, as_of=datetime(2026, 10, 3, tzinfo=UTC))
    head = store.read_head(config, payload["snapshot_id"])
    sizes = {n["id"]: len(json.dumps(n)) for n in head["networks"]}
    print("network head bytes", sizes)
    assert len(sizes) == 2 and all(0 < n <= 200_000 for n in sizes.values()), sizes
