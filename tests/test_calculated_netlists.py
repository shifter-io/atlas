"""NET-LISTS-1: one place list per network window (contract K10), merged in the database.

Hand-written group lists check the merge itself (sums, the percentile row and its ties, entry
counts, positions, network isolation) against the K10 rule and an independent Python merge. The
3-day fixtures check a real build: one list per network window, the payload, group lists,
typed windows and serve answers unchanged against a build without network lists, the incremental
build's network lists equal to the full one's, and retention (prune, discard, the estimate).
"""

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime

import pytest
from test_calculated import lenient_policy, payload_mismatches
from test_calculated_hourly_reads import HOUR, daily_history, floor_hour
from test_calculated_incremental import history
from test_calculated_listref import served, without

from proxy_index.calculated import netlists, store
from proxy_index.calculated.calculator import calculate
from proxy_index.names import labels
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

US = ("US", "United States", None, None, None)
DE = ("DE", "Germany", None, None, None)
FR = ("FR", "France", None, None, None)
IT = ("IT", "Italy", None, None, None)
CITY = ("US", "United States", "CA", "California", "San Jose")
ASN = (64500, "Example Net", "isp")
WINDOWS = (("24h", 24), ("7d", 168), ("30d", 720))
ROWS = (
    "SELECT dimension,position,place_id,asn_label_id,ips,observations,completed,successes,"
    "challenges,blocks,p50,p90,p99 FROM calculated_list_rows WHERE list_id=%s "
    "ORDER BY dimension,position"
)
NETWORK_LISTS = (
    "SELECT created_build,product_id,window_hours,content_hash,entries,signature,figures,list_id "
    "FROM calculated_lists WHERE cohort='network' ORDER BY created_build,list_id"
)


@pytest.fixture
def calculated(database):
    """A database with the calculated schema, for hand-written lists."""
    store.initialize(database)
    return database


def product(conn, provider, name):
    conn.execute("INSERT INTO providers VALUES (%s) ON CONFLICT DO NOTHING", (provider,))
    return conn.execute(
        "INSERT INTO products(provider,name) VALUES (%s,%s) RETURNING product_id", (provider, name)
    ).fetchone()[0]


def group_list(conn, product_id, rows):
    """A 24h group list of rows (dimension, place, network, ips, completed, successes, p50)."""
    places, networks = labels(conn, {r[1] for r in rows if r[1]}, {r[2] for r in rows if r[2]})
    list_id = conn.execute(
        "INSERT INTO calculated_lists(product_id,cohort,window_hours,content_hash,entries,"
        "figures,created_build) VALUES (%s,'group',24,'h','{}','{}',0) RETURNING list_id",
        (product_id,),
    ).fetchone()[0]
    positions = Counter()
    for dimension, place, network, ips, completed, successes, p50 in rows:
        conn.execute(
            "INSERT INTO calculated_list_rows VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,2,%s,%s,%s)",
            (list_id, dimension, positions[dimension], places.get(place), networks.get(network))
            + (ips, 2 * ips, completed, successes, p50, p50 and p50 + 1, p50 and p50 + 2),
        )
        positions[dimension] += 1
    return list_id


def group(gid, provider, kind, product_id, completed):
    """A payload product group with every window and its 30d completed requests."""
    return {
        "id": gid,
        "provider": provider,
        "kind": kind,
        "product_id": product_id,
        "windows": {
            label: {"period": {"hours": hours}, "metrics": {"completed": completed}}
            for label, hours in WINDOWS
        },
    }


def rows(conn, list_id):
    return conn.execute(ROWS, (list_id,)).fetchall()


def oracle(conn, members):
    """The K10 merge of the member lists' rows, computed in Python from the stored rows."""
    merged, top = defaultdict(lambda: [None] * 6), {}
    for list_id in members:
        for r in rows(conn, list_id):
            if r[0] == "x":
                continue
            key = (r[0], r[2], r[3])
            sums = merged[key]
            for i, value in enumerate(r[4:10]):  # SQL sum(): NULLs left out, all NULL is NULL
                if value is not None:
                    sums[i] = (sums[i] or 0) + value
            best = (-1 if r[7] is None else r[7], -list_id, -r[1])
            if key not in top or best > top[key][0]:
                top[key] = (best, r[10:])
    last = float("inf")
    order = sorted(
        merged,
        key=lambda k: (
            k[0],
            -(merged[k][2] if merged[k][2] is not None else -last),
            -merged[k][0],
            last if k[1] is None else k[1],
            last if k[2] is None else k[2],
        ),
    )
    positions = Counter()
    result = []
    for key in order:
        result.append((key[0], positions[key[0]], key[1], key[2], *merged[key], *top[key][1]))
        positions[key[0]] += 1
    return result


def test_a_network_list_sums_its_groups_and_takes_the_busiest_rows_percentiles(calculated):
    with connect(calculated) as conn:
        p1, p2 = product(conn, "alpha", "residential"), product(conn, "alpha", "mobile")
        l1 = group_list(
            conn,
            p1,
            [
                ("c", US, None, 5, 10, 8, 100),
                ("g", CITY, None, 3, 6, 5, 110),
                ("a", None, ASN, 4, 8, 7, 120),
                ("y", CITY, ASN, 2, 4, 3, 130),
                ("x", US, ASN, 9, 9, 9, 140),  # country x ASN: not in a network list
            ],
        )
        l2 = group_list(
            conn,
            p1,
            [
                ("c", US, None, 2, 4, 9, 200),
                ("g", CITY, None, 1, 2, 5, 210),  # ties l1's 5 successes: l1, the lower id, wins
                ("a", None, ASN, 1, 1, 1, 220),
                ("y", CITY, ASN, 1, 2, 4, 230),
            ],
        )
        l3 = group_list(
            conn,
            p2,
            [
                ("c", DE, None, 7, 30, 20, 300),
                ("g", CITY, None, 1, 1, 1, 310),
                ("a", None, ASN, 1, 1, 9, 320),
                ("y", CITY, ASN, 1, 1, 1, 330),
            ],
        )
        groups = [
            group("g1", "alpha", "residential", p1, 10),
            group("g2", "alpha", "residential", p1, 15),  # p1: 25 30d requests, the headline
            group("g3", "alpha", "residential", p2, 20),
        ]
        members = {("g1", "24h"): l1, ("g2", "24h"): l2, ("g3", "24h"): l3}
        written = netlists.write_network_lists(conn, 7, {"products": groups}, members)
        # Only the 24h window has member lists: the other windows get no network list.
        assert list(written) == [("alpha:residential:network", "24h")]
        list_id, entries = written["alpha:residential:network", "24h"]
        assert entries == {"country": 2, "region": 0, "city": 1, "asn": 1, "city_asn": 1}
        stored = conn.execute(
            "SELECT product_id,cohort,window_hours,signature,entries,figures,created_build,"
            "length(content_hash) FROM calculated_lists WHERE list_id=%s",
            (list_id,),
        ).fetchone()
        assert stored == (p1, "network", 24, None, entries, {}, 7, 64)
        merged = rows(conn, list_id)
        assert merged == oracle(conn, [l1, l2, l3])
        by_label = {(r[0], r[1]): (r[4], r[6], r[10]) for r in merged}  # ips, completed, p50
        assert by_label == {
            ("c", 0): (7, 30, 300),  # Germany: most completed requests
            ("c", 1): (7, 14, 200),  # the US: 5 + 2 IPs, p50 of l2's row (9 successes)
            ("g", 0): (5, 9, 110),
            ("a", 0): (6, 10, 320),
            ("y", 0): (4, 7, 230),
        }
        # Every summed counter is the sum; the percentiles are one member row's, never mixed.
        us = next(r for r in merged if r[:2] == ("c", 1))
        assert us[4:] == (7, 14, 14, 17, 2, 4, 200, 201, 202)


def test_the_headline_product_has_the_most_30d_requests_ties_to_the_lowest_id():
    tie = [group("a", "p", "k", 9, 5), group("b", "p", "k", 3, 5), group("c", "p", "k", 4, 2)]
    assert netlists.headline(tie) == 3
    no_30d = group("d", "p", "k", 1, 0)
    del no_30d["windows"]["30d"]
    assert netlists.headline([no_30d, group("e", "p", "k", 2, 1)]) == 2


def test_positions_are_deterministic_and_follow_the_k10_order(calculated):
    with connect(calculated) as conn:
        p = product(conn, "alpha", "residential")
        data = [
            ("c", FR, None, 3, 5, 5, 100),
            ("c", IT, None, 4, 5, 5, 100),
            ("c", DE, None, 4, 5, 5, 100),
            ("c", US, None, 9, None, None, None),  # a pre-v3 row: no requests, sorts last
            ("a", None, ASN, 1, 1, 1, 100),
        ]
        members = {("g", "24h"): group_list(conn, p, data)}
        payload = {"products": [group("g", "alpha", "residential", p, 1)]}
        first = netlists.write_network_lists(conn, 1, payload, members)
        second = netlists.write_network_lists(conn, 2, payload, members)
        [(a, _)], [(b, _)] = first.values(), second.values()
        assert a != b and rows(conn, a) == rows(conn, b) == oracle(conn, [*members.values()])
        hashes = "SELECT content_hash FROM calculated_lists WHERE list_id=ANY(%s)"
        assert len({h for (h,) in conn.execute(hashes, ([a, b],))}) == 1
        ids = labels(conn, {FR, IT, DE, US}, set())[0]
        countries = [r[2] for r in rows(conn, a) if r[0] == "c"]
        # 5 requests each: most IPs first (IT and DE by place id), then France; the US last.
        assert countries == [*sorted([ids[IT], ids[DE]]), ids[FR], ids[US]]
        assert [r[6] for r in rows(conn, a) if r[0] == "c"] == [5, 5, 5, None]


def test_each_network_is_one_provider_and_kind(calculated):
    with connect(calculated) as conn:
        networks = [
            ("alpha", "residential", 1),
            ("alpha", "datacenter", 10),
            ("beta", "residential", 100),
        ]
        groups, members = [], {}
        for provider, kind, ips in networks:
            p = product(conn, provider, kind)
            gid = f"{p}:daily"
            groups.append(group(gid, provider, kind, p, ips))
            members[gid, "7d"] = group_list(conn, p, [("c", US, None, ips, ips, ips, ips)])
        written = netlists.write_network_lists(conn, 3, {"products": groups}, members)
        assert sorted(written) == sorted(
            (f"{provider}:{kind}:network", "7d") for provider, kind, _ in networks
        )
        for provider, kind, ips in networks:
            list_id, entries = written[f"{provider}:{kind}:network", "7d"]
            assert [r[4] for r in rows(conn, list_id)] == [ips] and entries["country"] == 1
        hours = "SELECT DISTINCT window_hours FROM calculated_lists WHERE cohort='network'"
        assert conn.execute(hours).fetchall() == [(168,)]


def network_lists(database, build):
    """Each network list of a build, with its rows, by product and window."""
    with connect(database) as conn:
        found = conn.execute(
            "SELECT product_id,window_hours,content_hash,entries,list_id FROM calculated_lists "
            "WHERE cohort='network' AND created_build=%s ORDER BY 1,2",
            (int(build),),
        ).fetchall()
        return {r[:2]: (*r[2:4], rows(conn, r[4])) for r in found}


async def capture(database, build):
    """What a build publishes apart from network lists: its payload, typed windows, group lists
    and serve answers, without build ids, list ids and generated_at."""
    with connect(database) as conn:
        windows = conn.execute(
            "SELECT to_jsonb(w)-'result_id'-'build_id'-'list_id',l.signature,l.content_hash,"
            "l.entries,l.figures,l.list_id FROM calculated_windows w "
            "LEFT JOIN calculated_lists l USING(list_id) WHERE w.build_id=%s "
            "ORDER BY w.product_id,w.cohort,w.target,w.window_hours",
            (int(build),),
        ).fetchall()
        windows = [(*w[:5], w[5] and rows(conn, w[5])) for w in windows]
    payload = json.dumps(without(store.read(database, str(build))))
    return payload, windows, await served(database, [build])


@pytest.mark.asyncio
async def test_a_build_writes_one_list_per_network_window_and_changes_nothing_else(
    database, tmp_path, monkeypatch
):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)  # daily, census-pulse and browser groups
    policy = lenient_policy(tmp_path)
    # Before NET-LISTS-1: the same build without network lists.
    monkeypatch.setattr(netlists, "write_network_lists", lambda *args: {})
    before = calculate(database, policy, as_of=end, mode="incremental")
    assert network_lists(database, before["snapshot_id"]) == {}
    old = await capture(database, before["snapshot_id"])
    with connect(database) as conn:
        store.discard(conn, int(before["snapshot_id"]))
    monkeypatch.undo()
    new = calculate(database, policy, as_of=end, mode="incremental")
    assert await capture(database, new["snapshot_id"]) == old
    assert payload_mismatches(database, int(new["snapshot_id"])) == []
    full = calculate(database, policy, as_of=end, mode="full")
    lists = network_lists(database, new["snapshot_id"])
    # The incremental build's network lists equal the full build's (CALC-INC-1a rule).
    assert lists == network_lists(database, full["snapshot_id"])
    # alpha (US, DE and census-pulse groups), beta and gamma: one list per network window.
    networks = {(p["provider"], p["kind"]) for p in new["products"]}
    assert len(networks) == 3 and len(lists) == len(networks) * len(WINDOWS)
    with connect(database) as conn:
        members = defaultdict(list)
        for product_id, hours, list_id in conn.execute(
            "SELECT product_id,window_hours,list_id FROM calculated_windows "
            "WHERE build_id=%s AND target=''",
            (int(new["snapshot_id"]),),
        ):
            members[product_id, hours].append(list_id)
        for net in networks:
            network = [g for g in new["products"] if (g["provider"], g["kind"]) == net]
            for _, hours in WINDOWS:
                ids = sorted({i for g in network for i in members[g["product_id"], hours]})
                key = (netlists.headline(network), hours)
                assert lists[key][2] == oracle(conn, ids)
                assert lists[key][1] == {
                    name: sum(1 for r in lists[key][2] if r[0] == code)
                    for name, code in netlists.DIMENSIONS.items()
                }


@pytest.mark.asyncio
async def test_network_lists_live_while_their_build_has_windows(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await daily_history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    builds = [
        int(calculate(database, policy, as_of=end - k * HOUR, mode="incremental")["snapshot_id"])
        for k in (2, 1, 0)
    ]

    def by_build(conn):
        return Counter(r[0] for r in conn.execute(NETWORK_LISTS))

    def counts(conn):
        query = "SELECT (SELECT count(*) FROM calculated_lists),(SELECT count(*) FROM "
        return conn.execute(query + "calculated_list_rows)").fetchone()

    with connect(database) as conn:
        # calculate()'s prune() dropped the oldest draft's payload; its windows stay.
        payloads = "SELECT build_id FROM calculated_snapshots ORDER BY 1"
        assert conn.execute(payloads).fetchall() == [(b,) for b in builds[1:]]
        assert store.collect(conn) == 0
        assert by_build(conn) == {b: 2 * len(WINDOWS) for b in builds}
        assert conn.execute(
            "SELECT bool_and(signature IS NULL AND figures='{}') FROM calculated_lists "
            "WHERE cohort='network'"
        ).fetchone() == (True,)
        store.discard(conn, builds[1])
        assert by_build(conn) == {builds[0]: 6, builds[2]: 6}
        # The estimate counts what discarding the pruned build frees: its network lists too.
        estimate, before = store.estimate(conn, [builds[0]])["rows"], counts(conn)
        store.discard(conn, builds[0])
        freed = tuple(a - b for a, b in zip(before, counts(conn), strict=True))
        assert freed == (estimate["calculated_lists"], estimate["calculated_list_rows"])
        assert freed[0] >= 6 and by_build(conn) == {builds[2]: 6}
        # Group lists keep their rule: every one left is referenced by a window.
        assert conn.execute(
            "SELECT count(*) FROM calculated_lists l WHERE cohort<>'network' AND NOT EXISTS "
            "(SELECT FROM calculated_windows w WHERE w.list_id=l.list_id)"
        ).fetchone() == (0,)
