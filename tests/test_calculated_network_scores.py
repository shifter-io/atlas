"""NET-SUM-2: network scores, ranks and ownership overlap (contract K9), PostgreSQL 17.

The unit cases score hand-built network windows (calculated/network_scores.py); the database
cases build snapshots of seeded runs (test_calculated_hourly_reads.History) and compare the
networks with their groups, with the IP sets the database holds, and incremental with full.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from calculated_cases import POLICY, lenient_policy
from calculated_network_scores_cases import (
    AS_OF,
    assert_set_rule,
    coverages,
    place,
    run,
    scoring_policy,
    swapped,
    unit_networks,
)
from test_calculated_hourly_reads import BUCKET, DAY, HOUR, PRODUCT_IPS, History, floor_hour
from test_calculated_incremental import history
from test_calculated_network import assert_one_group, by_provider, stable

from atlas.calculated import network_scores, store
from atlas.calculated.calculator import calculate
from atlas.calculated.policy import DIMENSIONS, grade, load
from atlas.storage import connect

pytest_plugins = ["test_storage"]

OVERLAP_KEYS = {"exclusive_share", "shared_share", "compared_products", "peers"}
INCOMPLETE = "Required benchmark incomplete: generic_http"


def test_hand_computed_scores_and_ranks():
    """Every component but value is 100 x yours / best (best / yours for speed); value is by
    place, 100 x (N - rank + 1) / N on the cost per 1,000 successes ($2/1.0, $4/0.8 and $8/0.5
    on the same wire bytes: #1, #2 and #3 of 3). The composite uses the balanced weights
    (22/14/10/12/8/10/10/14):
    bbb = 0.22x80 + 0.14x50 + 0.10x90 + (0.12 + 0.08 + 0.10 + 0.10)x50 + 0.14x66.7 = 62.93,
    cccc = 0.22x50 + 0.14x25 + 0.10x50 + (0.12 + 0.08 + 0.10 + 0.10)x25 + 0.14x33.3 = 34.17."""
    config, method_id = load(POLICY)
    networks = unit_networks()
    payload = {"as_of": AS_OF, "methodology": {"id": method_id, "settings": config}}
    network_scores.score(networks, payload, None)
    table = {}
    for n in networks:
        w = n["windows"]["30d"]
        s = w["scores"]["balanced"]
        table[n["provider"]] = (
            {k: c["value"] for k, c in s["components"].items()},
            s["score"],
            s["grade"],
            s["rank"],
        )
        assert set(w["scores"]) == {"balanced"} and "_lead" not in w
        assert all("score" not in t for t in w["targets"].values())
        assert (w["readiness"], s["state"], s["scored_at"]) == ("ready", "ready", AS_OF)
        assert s["valid_until"] == AS_OF + timedelta(hours=23)  # last observed + 24 hours
    print("rank table", json.dumps(table, indent=1))
    expected = {
        "aa": ([100, 100, 100, 100, 100, 100, 100, 100], 100.0, "A+", 1),
        "bbb": ([80, 50, 90, 50, 50, 50, 50, 200 / 3], 60.6 + 0.14 * 50 / 3, "C+", 2),
        "cccc": ([50, 25, 50, 25, 25, 25, 25, 100 / 3], 33.0 + 0.14 * 25 / 3, "D", 3),
    }
    keys = ("reliability", "speed", "uptime", "coverage", "diversity", "dcAsns")
    keys += ("authenticity", "value")
    for provider, (components, value, letter, position) in expected.items():
        found = table[provider]
        assert [found[0][k] for k in keys] == pytest.approx(components)
        assert found[1:] == (pytest.approx(value), letter, position)
        assert grade(value, config) == letter
    # Alone in its kind: scored against itself (every component 100 with one peer), no rank.
    alone = networks[3]["windows"]["30d"]["scores"]["balanced"]
    assert alone["score"] == 100.0 and alone["rank"] is None
    assert all(c["inputs"]["peers"] == 1 for c in alone["components"].values())


def test_a_window_without_place_counts_has_no_diversity():
    """The regression (CI tier1 at be7c5e2): a window with IPs but no K10 list (null place
    counts) crashed peer_scores(); its diversity is unavailable and its score incomplete, and
    its peers are scored without it."""
    config, method_id = load(POLICY)
    networks = unit_networks()
    daily = networks[1]["windows"]["30d"]["pool"]["daily"]
    daily |= dict.fromkeys(DIMENSIONS)
    payload = {"as_of": AS_OF, "methodology": {"id": method_id, "settings": config}}
    network_scores.score(networks, payload, None)
    s = networks[1]["windows"]["30d"]["scores"]["balanced"]
    assert s["components"]["diversity"]["state"] == "unavailable"
    assert (s["score"], s["state"]) == (None, "incomplete")
    assert network_scores.UNPLACED in s["reasons"]
    others = [networks[n]["windows"]["30d"]["scores"]["balanced"] for n in (0, 2)]
    assert [o["rank"] for o in others] == [1, 2]
    assert others[0]["components"]["diversity"]["inputs"]["peers"] == 2


def test_previous_rank_comes_from_the_previous_head():
    """A second build carries the first one's rank; a network the previous head lacks gets
    previous_rank null."""
    config, method_id = load(POLICY)
    payload = {"as_of": AS_OF, "methodology": {"id": method_id, "settings": config}}
    first = unit_networks()
    network_scores.score(first, payload, None)
    previous = {
        "as_of": AS_OF.isoformat(),
        "methodology": {"id": method_id},
        "products": [],
        "networks": [
            {
                "id": n["id"],
                "windows": {
                    label: {"scores": store.jsonable(w["scores"]), "targets": {}}
                    for label, w in n["windows"].items()
                },
            }
            for n in first
            if n["provider"] != "cccc"
        ],
    }
    second = unit_networks()
    later = payload | {"as_of": AS_OF + HOUR}
    network_scores.score(second, later, previous)
    found = {n["provider"]: n["windows"]["30d"]["scores"]["balanced"] for n in second}
    assert {p: s["previous_rank"] for p, s in found.items()} == {
        "aa": 1,
        "bbb": 2,
        "cccc": None,
        "d": None,
    }
    assert [found[p]["rank"] for p in ("aa", "bbb", "cccc")] == [1, 2, 3]
    assert found["aa"]["methodology_id"] == method_id
    # A previous head newer than the build is ignored, as for groups.
    third = unit_networks()
    network_scores.score(third, payload, previous | {"as_of": (AS_OF + HOUR).isoformat()})
    assert third[0]["windows"]["30d"]["scores"]["balanced"]["previous_rank"] is None


def test_overlap_shares_without_ips():
    members = [({"kind": "isp", "provider": "a", "product_id": 1}, {})]
    members += [({"kind": "isp", "provider": "b", "product_id": 2}, {})]
    assert network_scores.shares(0, members, {1: (5, 0)}, {}) == {
        "exclusive_share": None,
        "shared_share": None,
        "compared_products": 0,
        "peers": [],
    }
    assert network_scores.shares(1, members, {1: (5, 0)}, {})["exclusive_share"] == 1.0


def test_daily_overlaps_are_carried_within_a_utc_day():
    """7d and 30d overlaps counted the same UTC day for the same networks are carried; the 24h
    one, one of another day or after the cutoff, a changed set of networks or a window without
    any pair is counted."""
    pair = {"provider": "b", "shared_ips": 1}
    counted = {"shared_share": 0.5, "peers": [pair], "as_of": "2026-11-05T00:00:00+00:00"}

    def previous(*ids, at=counted["as_of"], peers=(pair,)):
        overlap = counted | {"as_of": at, "peers": list(peers)}
        windows = {label: {"overlap": overlap} for label in ("24h", "7d", "30d")}
        return {"networks": [{"id": i, "windows": windows} for i in ids]}

    nets = [{"id": "a"}, {"id": "b"}]
    noon = datetime(2026, 11, 5, 12, tzinfo=UTC)
    assert network_scores.carried(nets, previous("a", "b"), noon, "30d") == {
        "a": counted,
        "b": counted,
    }
    assert network_scores.carried(nets, previous("a", "b"), noon, "7d") is not None
    assert network_scores.carried(nets, previous("a", "b"), noon, "24h") is None
    assert network_scores.carried(nets, None, noon, "30d") is None
    assert network_scores.carried(nets, previous("a"), noon, "30d") is None
    assert network_scores.carried(nets, previous("a", "b", "c"), noon, "30d") is None
    before = previous("a", "b", at="2026-11-04T23:55:00+00:00")
    assert network_scores.carried(nets, before, noon, "30d") is None
    after = previous("a", "b", at="2026-11-05T12:05:00+00:00")
    assert network_scores.carried(nets, after, noon, "30d") is None
    unset = {"networks": [{"id": i, "windows": {"30d": {"overlap": None}}} for i in "ab"]}
    assert network_scores.carried(nets, unset, noon, "30d") is None
    assert network_scores.carried(nets, previous("a", "b", peers=()), noon, "7d") is None


@pytest.mark.asyncio
async def test_overlap_counts_the_ips_of_the_pool(database, tmp_path):
    """alpha and beta share 10 of their 100 IPs, gamma none; alpha's run crosses the 24h
    window's start, so its IPs before it count in 7d and 30d only. ips_ours equals the
    window's pool.unique_ips, and every overlap equals the set rule over the database's IPs.
    A later build of the same UTC day, after a gamma run saw one of beta's IPs, counts the 24h
    overlap again and carries the published 7d and 30d ones with their own as_of."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    h = History(database, tmp_path, end)
    alpha = await run(h, "alpha", DAY + BUCKET, 120, split=True)
    beta = await run(h, "beta", 3 * HOUR, 120)
    gamma = await run(h, "gamma", 3 * HOUR, 120)
    with connect(database) as conn:
        ips = [
            conn.execute(
                "INSERT INTO ip_addresses(ip) VALUES (%s) RETURNING ip_id", (f"2001:db8:1::{n:x}",)
            ).fetchone()[0]
            for n in range(1, 261)
        ]
        start = end - DAY
        place(conn, alpha, {start - BUCKET: ips[:5] + ips[250:260], start: ips[:100]})
        place(conn, beta, {end - 3 * HOUR: ips[90:190]})
        place(conn, gamma, {end - 3 * HOUR: ips[200:250]})
        conn.execute(PRODUCT_IPS)
    policy = lenient_policy(tmp_path)
    new = calculate(database, policy, as_of=end, mode="incremental")
    full = calculate(database, policy, as_of=end, mode="full")
    payload = store.read(database, full["snapshot_id"])
    assert stable(store.read(database, new["snapshot_id"])["networks"]) == stable(
        payload["networks"]
    )
    nets = {n["provider"]: n for n in payload["networks"]}
    assert set(nets) == {"alpha", "beta", "gamma"}
    products = {p: n["product_id"] for p, n in nets.items()}
    day = nets["alpha"]["windows"]["24h"]["overlap"]
    assert day == {
        "exclusive_share": pytest.approx(0.9),
        "shared_share": pytest.approx(0.1),
        "compared_products": 2,
        "peers": [
            {
                "provider": "beta",
                "product_id": products["beta"],
                "shared_ips": 10,
                "ips_ours": 100,
                "ips_theirs": 100,
            }
        ],
        "as_of": payload["as_of"],
    }
    gamma_day = nets["gamma"]["windows"]["24h"]["overlap"]
    assert (gamma_day["shared_share"], gamma_day["compared_products"]) == (0, 2)
    assert gamma_day["peers"] == []
    assert nets["alpha"]["windows"]["7d"]["overlap"]["peers"][0]["ips_ours"] == 110
    assert_set_rule(database, nets, ("24h", "7d", "30d"), payload["as_of"])
    store.publish(database, full["snapshot_id"])
    seen = await run(h, "gamma", 2 * HOUR, 40)  # a new run: stored runs never change
    with connect(database) as conn:
        place(conn, seen, {end - 2 * HOUR: ips[150:151]})
        conn.execute(PRODUCT_IPS)
    later = store.read(database, calculate(database, policy, as_of=end + BUCKET)["snapshot_id"])
    again = {n["provider"]: n for n in later["networks"]}
    assert_set_rule(database, again, ("24h",), later["as_of"])
    assert again["gamma"]["windows"]["24h"]["overlap"]["shared_share"] == pytest.approx(1 / 51)
    for label in ("7d", "30d"):
        for provider, n in again.items():
            assert n["windows"][label]["overlap"] == nets[provider]["windows"][label]["overlap"]
        assert again["gamma"]["windows"][label]["pool"]["unique_ips"] == 51


@pytest.mark.asyncio
async def test_a_census_pulse_network_scores_as_its_group(database, tmp_path):
    """Two networks of one census-pulse group each: their windows equal their groups', and
    their scores too, except through diversity, the one input K9 defines differently (the
    window's network place counts, from its K10 list, against the group's daily averages).
    The next build carries the published ranks as previous_rank."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    h = History(database, tmp_path, end)
    for provider, offset in (("alpha", 0 * BUCKET), ("beta", 2 * BUCKET)):  # one batch per instant
        await h.scheduled("census", provider, 30 * HOUR - offset)
        await h.scheduled("pulse", provider, 5 * HOUR - offset)
    policy = scoring_policy(tmp_path)
    first = calculate(database, policy, as_of=end, publish=True)
    payload = store.read(database, first["snapshot_id"])
    nets, groups = by_provider(payload)
    ready = 0
    for provider in ("alpha", "beta"):
        (group,) = groups[provider]
        net = nets[provider]
        assert_one_group(net, group)
        assert net["outages"] == group["outages"]
        for label, w in net["windows"].items():
            g = group["windows"][label]
            s, t = w["scores"]["balanced"], g["scores"]["balanced"]
            assert w["readiness"] == g["readiness"], label
            for key in ("state", "weights", "reasons", "valid_until", "scored_at"):
                assert s[key] == t[key], (label, key)
            others = {k: c for k, c in s["components"].items() if k != "diversity"}
            assert others == {k: c for k, c in t["components"].items() if k != "diversity"}
            counts = {k: w["pool"]["daily"][k] for k in DIMENSIONS}
            diversity = s["components"]["diversity"]["inputs"]
            assert {k: diversity[k]["value"] for k in DIMENSIONS} == counts
            if t["score"] is None:
                assert s["score"] is None
            else:
                assert swapped(s, t) == pytest.approx(t["score"], rel=1e-12)
                ready += 1
            if s["components"]["diversity"] == t["components"]["diversity"]:
                assert (s["score"], s["grade"], s["rank"]) == (t["score"], t["grade"], t["rank"])
            # The same peers: the group's ownership shares are the network's.
            assert {k: w["overlap"][k] for k in OVERLAP_KEYS - {"peers"}} == g["overlap"]
    assert ready >= 2  # both networks are scored at least in one window
    second = calculate(database, policy, as_of=end + HOUR)
    later = {n["id"]: n for n in store.read_head(database, second["snapshot_id"])["networks"]}
    for net in payload["networks"]:
        for label, w in net["windows"].items():
            score = later[net["id"]]["windows"][label]["scores"]["balanced"]
            assert score["previous_rank"] == w["scores"]["balanced"]["rank"], label


@pytest.mark.asyncio
async def test_a_mixed_network_is_scored_on_its_slot_coverage(database, tmp_path):
    """alpha (two daily groups and a census-pulse one) is scored on the scheduled group's
    pulse-slot coverage, beta (daily only) on its bucket coverage: with minimum_coverage between
    alpha's slot coverage and its US group's bucket coverage, alpha's readiness follows the
    slot coverage."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    built = calculate(database, scoring_policy(tmp_path), as_of=end)
    first = store.read(database, built["snapshot_id"])
    nets, groups = by_provider(first)
    (scheduled,) = [g for g in groups["alpha"] if g["criteria"]["country"] is None]
    (daily,) = [g for g in groups["alpha"] if g["criteria"]["country"] == "us"]
    slot, (own, bucket) = coverages(nets["alpha"], [scheduled, daily], "30d")
    assert slot == own != bucket
    beta, (beta_group,) = coverages(nets["beta"], groups["beta"], "30d")
    assert beta == beta_group
    threshold = (slot + bucket) / 2
    second = calculate(database, scoring_policy(tmp_path, f"{threshold!r}"), as_of=end)
    found = {n["provider"]: n for n in store.read(database, second["snapshot_id"])["networks"]}
    for provider, value in (("alpha", slot), ("beta", beta)):
        reasons = found[provider]["windows"]["30d"]["scores"]["balanced"]["reasons"]
        assert (INCOMPLETE in reasons) == (value < threshold), (provider, value, threshold)
