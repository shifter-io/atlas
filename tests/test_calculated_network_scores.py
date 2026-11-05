"""NET-SUM-2: network scores, ranks and ownership overlap (contract K9), PostgreSQL 17.

The unit cases score hand-built network windows (calculated/network_scores.py); the database
cases build snapshots of seeded runs (test_calculated_hourly_reads.History) and compare the
networks with their groups, with the IP sets the database holds, and incremental with full.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_calculated import POLICY, lenient_policy
from test_calculated_hourly_reads import BUCKET, DAY, HOUR, PRODUCT_IPS, History, floor_hour
from test_calculated_incremental import history
from test_calculated_network import assert_one_group, by_provider, stable
from test_storage import measurement

from proxy_index.calculated import network_scores, store
from proxy_index.calculated.calculator import calculate
from proxy_index.calculated.policy import DIMENSIONS, composite, grade, load
from proxy_index.partitions import partition
from proxy_index.storage import connect, ingest

pytest_plugins = ["test_storage"]

AS_OF = datetime(2026, 11, 5, 12, tzinfo=UTC)
OVERLAP_KEYS = {"exclusive_share", "shared_share", "compared_products", "peers"}
INCOMPLETE = "Required benchmark incomplete: generic_http"
# Each provider's distinct IPs in a window (no browser run here records IPs).
IPS = (
    "SELECT p.provider,o.ip_id FROM ip_observations o JOIN test_runs r USING(run_key) "
    "JOIN scope_stats s USING(scope_id) JOIN products p USING(product_id) "
    "WHERE o.bucket_start>=%s AND o.bucket_start<%s"
)


def scoring_policy(tmp_path, coverage="0"):
    """The lenient policy scoring what the fixtures measure: generic_http only, on reliability,
    coverage and diversity (no load test, price, uptime batches or DB-IP usage types)."""
    text = lenient_policy(tmp_path).read_text()
    for old, new in (
        (
            'required_targets = ["generic_http", "generic_https"]',
            'required_targets = ["generic_http"]',
        ),
        ("generic_http = 1\ngeneric_https = 1\n", "generic_http = 1\n"),
        ("reliability = 80\nspeed = 20\n", "reliability = 80\nspeed = 0\n"),
        (
            "[weights]\nreliability = 22\nspeed = 14\nuptime = 10\ncoverage = 12\ndiversity = 8\n"
            "dcAsns = 10\nauthenticity = 10\nvalue = 14\n",
            "[weights]\nreliability = 50\nspeed = 0\nuptime = 0\ncoverage = 30\ndiversity = 20\n"
            "dcAsns = 0\nauthenticity = 0\nvalue = 0\n",
        ),
        ("minimum_coverage = 0\n", f"minimum_coverage = {coverage}\n"),
    ):
        assert old in text, old
        text = text.replace(old, new, 1)
    policy = tmp_path / f"scoring-{coverage}.toml"
    policy.write_text(text)
    return policy


def unit_network(provider, kind, success, latency, up, ips, scale, price):
    """A network as attach() completes it: one 30d window with known raw values. scale is the
    pool's share of the leader's places, dcAsns' clean share and geo accuracy."""

    def measured(rate):
        return {
            "completed": 5000,
            "successes": int(5000 * rate),
            "success_rate": rate,
            "coverage": 1.0,
            "last_observed": AS_OF - HOUR,
            "latency_under_load_ms": {"p50": latency},
            "under_load": {},
        }

    daily = {"unique_ips": ips} | {k: int(8 * 2**n * scale) for n, k in enumerate(DIMENSIONS)}
    window = {
        "metrics": measured(success),
        "targets": {t: {"metrics": measured(success)} for t in ("generic_http", "generic_https")},
        "pool": {"unique_ips": ips, "daily": daily}
        | {"datacenter_asn_share": 1 - scale, "geo_accuracy": scale},
        "uptime": {"batches": 20, "value": up},
        "scores": {},
        "_lead": {"evidence": {}, "evidence_sources": []},
    }
    return {
        "id": f"{provider}:{kind}:network",
        "provider": provider,
        "product": "residential",
        "product_id": len(provider),
        "kind": kind,
        "pricing": {"lowest_per_gb": price},
        "windows": {"30d": window},
    }


def unit_networks():
    return [
        unit_network("aa", "residential", 1.0, 100, 1.0, 1000, 1, 2),
        unit_network("bbb", "residential", 0.8, 200, 0.9, 500, 0.5, 4),
        unit_network("cccc", "residential", 0.5, 400, 0.5, 250, 0.25, 8),
        unit_network("d", "isp", 0.8, 200, 0.9, 500, 0.5, 4),
    ]


def test_hand_computed_scores_and_ranks():
    """Every component is 100 x yours / best (best / yours for speed and value), and the
    composite uses the balanced weights (22/14/10/12/8/10/10/14):
    bbb = 0.22x80 + 0.14x50 + 0.10x90 + 0.12x50 + 0.08x50 + 0.10x50 + 0.10x50 + 0.14x50 = 60.6,
    cccc = 0.22x50 + 0.14x25 + 0.10x50 + (0.12 + 0.08 + 0.10 + 0.10 + 0.14)x25 = 33.0."""
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
        "bbb": ([80, 50, 90, 50, 50, 50, 50, 50], 60.6, "C+", 2),
        "cccc": ([50, 25, 50, 25, 25, 25, 25, 25], 33.0, "D", 3),
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
    one, one of another day or after the cutoff, or a changed set of networks is counted."""
    counted = {"shared_share": 0.5, "as_of": "2026-11-05T00:00:00+00:00"}

    def previous(*ids, at=counted["as_of"]):
        overlap = counted | {"as_of": at}
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


async def run(h, provider, back, requests, *, split=False):
    """A daily US run of `requests` successes, settled `back` before end (History.settle())."""
    directory = await measurement(
        h.tmp_path, requests=requests, provider=provider, requested_country="us"
    )
    return h.settle(ingest(directory, h.database)["run_key"], back, split=split)


def place(conn, run_key, observed):
    """Replace a run's observations: observed maps a bucket start to its IP ids."""
    conn.execute("DELETE FROM ip_observations WHERE run_key=%s", (run_key,))
    for at, ip_ids in observed.items():
        partition(conn, "ip_observations", at)
        for ip_id in ip_ids:
            conn.execute(
                "INSERT INTO ip_observations VALUES (%s,%s,%s,1,%s,%s)",
                (at, run_key, ip_id, at, at + BUCKET / 5),
            )


def expected_overlap(sets, products, as_of):
    """The full-mode set rule (calculator.build()'s, per network): shares against the union of
    the other networks with IPs, and one row per network sharing an IP, counted at as_of."""
    found = {}
    for name, own in sets.items():
        peers = sorted(q for q in sets if q != name and sets[q])
        if not own:
            found[name] = {"exclusive_share": None, "shared_share": None}
            found[name] |= {"compared_products": 0, "peers": [], "as_of": as_of}
            continue
        shared = set().union(*(sets[q] for q in peers)) & own
        rows = [
            {"provider": q, "product_id": products[q], "shared_ips": len(own & sets[q])}
            | {"ips_ours": len(own), "ips_theirs": len(sets[q])}
            for q in peers
            if own & sets[q]
        ]
        found[name] = {
            "exclusive_share": 1 - len(shared) / len(own),
            "shared_share": len(shared) / len(own),
            "compared_products": len(peers),
            "peers": rows,
            "as_of": as_of,
        }
    return found


def assert_set_rule(database, nets, labels, as_of):
    """Each labelled window's overlap equals the set rule over the database's IPs, counted at
    as_of, and ips_ours (and ips_theirs) the window's pool.unique_ips."""
    products = {p: n["product_id"] for p, n in nets.items()}
    with connect(database) as conn:
        for label in labels:
            period = nets["alpha"]["windows"][label]["period"]
            bounds = [datetime.fromisoformat(period[k]) for k in ("start", "end")]
            sets = {p: set() for p in nets}
            for provider, ip_id in conn.execute(IPS, bounds).fetchall():
                sets[provider].add(ip_id)
            expected = expected_overlap(sets, products, as_of)
            for provider, n in nets.items():
                w = n["windows"][label]
                assert w["overlap"] == expected[provider], (label, provider)
                assert w["pool"]["unique_ips"] == len(sets[provider])
                for peer in w["overlap"]["peers"]:
                    assert peer["ips_ours"] == w["pool"]["unique_ips"]
                    theirs = nets[peer["provider"]]["windows"][label]["pool"]["unique_ips"]
                    assert peer["ips_theirs"] == theirs


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


def swapped(score, group):
    """The network's balanced composite with its group's diversity component."""
    components = score["components"] | {"diversity": group["components"]["diversity"]}
    return composite(components, score["weights"])


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


def coverages(net, groups, label):
    """generic_http's time coverage of a network window and of each of its groups'."""
    found = net["windows"][label]["targets"]["generic_http"]["metrics"]["coverage"]
    return found, [
        g["windows"][label]["targets"]["generic_http"]["metrics"]["coverage"] for g in groups
    ]


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
