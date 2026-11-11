"""ASN-COUNT-1: a network window's place counts are distinct places, never label rows.

A network list's rows are labels: an ASN once per usage type and name, a country once per name.
pool.countries, regions, cities and asns of a network window (and its pool.daily) count the
distinct places of those rows as poolfig.pool_figures() counts a group's. Hand-written group lists
check the merge against pool_figures() over the same IP labels; the 3-day fixture (one ASN under
two usage types, cities without a name, an empty country code) checks every group's and every
network window's counts against a direct distinct count over its observations' enrichment.
"""

from datetime import UTC, datetime

import pytest
from calculated_cases import lenient_policy
from test_calculated_hourly_reads import HOUR, floor_hour
from test_calculated_incremental import history
from test_calculated_netlists import group, group_list, product

from proxy_index.calculated import incremental, netlists, network, store
from proxy_index.calculated.aggregate import BREAKDOWNS, NETWORK, PLACE, _memberships
from proxy_index.calculated.calculator import calculate
from proxy_index.calculated.poolfig import EPOCH, pool_figures
from proxy_index.calculated.sqlwindow import Combo, WindowInputs
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

US = "United States"
# The IPs of two groups of one network: (place, network) labels.
FIRST = [
    (("US", US, "CA", "California", "San Jose"), (64500, "Example Net", "isp")),
    (("US", US, "CA", "California", None), (64500, "Example Net", "hosting")),
    (("US", US, "", "Texas", "Austin"), (64501, "Other Net", None)),
    (("", "Nowhere", None, None, "Ghost"), (None, None, "datacenter")),
]
SECOND = [
    (("US", "United States of America", "CA", "Calif.", "San Jose"), (64500, "Example", "isp")),
    (("US", US, "", None, "Reno"), (64500, "Example Net", "residential")),
    (("US", US, None, "", "Reno"), (64502, "Third", "isp")),
    (("DE", "Germany", None, None, None), (64501, "Other Net", "isp")),
    ((None, None, None, None, None), (None, None, None)),
]
COUNTS = ("countries", "regions", "cities", "asns")


@pytest.fixture
def calculated(database):
    """A database with the calculated schema, for hand-written lists."""
    store.initialize(database)
    return database


def list_rows(ips):
    """A group list's rows of these IP labels, as aggregate.breakdowns() and list_write.py give
    them: one per dimension and label (no country x ASN rows, which no network list has)."""
    found = set()
    for place, net in ips:
        for name, _, label in _memberships(place, net):
            code = BREAKDOWNS[name]
            if code in "crga":
                where = None if code == "a" else tuple(label.get(f) for f in PLACE)
                found.add((code, where, tuple(label[f] for f in NETWORK) if code == "a" else None))
    return [(code, where, net, 1, 1, 1, 100) for code, where, net in sorted(found, key=str)]


def figures(ips):
    """poolfig.pool_figures()'s place counts of these IP labels: a group's formula."""
    combos = tuple(
        Combo(place, net[:2], net[2], ips=1, observations=1, located=0, geo_matched=0)
        for place, net in ips
    )
    inputs = WindowInputs(combos, unique_ips=len(ips), observations=len(ips))
    return {k: pool_figures(inputs, EPOCH, 24)[k] for k in COUNTS}


def test_a_network_counts_distinct_places_of_its_groups_rows(calculated):
    with connect(calculated) as conn:
        p1, p2 = product(conn, "alpha", "residential"), product(conn, "alpha", "mobile")
        members = {
            ("g1", "24h"): group_list(conn, p1, list_rows(FIRST)),
            ("g2", "24h"): group_list(conn, p2, list_rows(SECOND)),
        }
        # Two networks of one group each: a network of one group counts the group's places.
        apart = [group("g1", "alpha", "residential", p1, 2), group("g2", "alpha", "x", p2, 1)]
        written = netlists.write_network_lists(conn, 1, {"products": apart}, members)
        _, entries, places = written["alpha:residential:network", "24h"]
        # ASN 64500 under two usage types: three ASN rows, two ASNs.
        assert entries["asn"] == 3 and places["asns"] == 2
        assert places == figures(FIRST)
        assert written["alpha:x:network", "24h"][2] == figures(SECOND)
        # One network of both: a place two groups name differently (a country, a region, an ASN
        # under other names and usage types) is one place.
        for g in apart:
            g["kind"] = "residential"
        written = netlists.write_network_lists(conn, 2, {"products": apart}, members)
        _, entries, places = written["alpha:residential:network", "24h"]
        assert places == figures(FIRST + SECOND)
        assert places == {"countries": 2, "regions": 2, "cities": 4, "asns": 3}
        assert (entries["country"], entries["region"], entries["city"], entries["asn"]) == (
            4,
            5,
            8,
            7,
        )


def test_a_window_without_a_network_list_has_no_place_counts():
    pool = {"new_ips": 0, "daily": {"days": 0, "unique_ips": None}}
    window = {"pool": pool, "_runs": [], "period": {"hours": 24}}
    done = network.complete(window, 0, [], None, None)
    assert {k: done["pool"][k] for k in COUNTS} == dict.fromkeys(COUNTS)
    found = (7, {"asn": 2}, {"countries": 1, "regions": 0, "cities": 0, "asns": 1})
    done = network.complete(window, 1, [], found, None)
    assert {k: done["pool"][k] for k in COUNTS} == found[2]
    assert {k: done["pool"]["daily"][k] for k in COUNTS} == found[2]
    assert done["pool"]["list_entries"] == done["breakdown_entries"] == {"asn": 2}


def direct(rows):
    """Distinct places of IP labels counted directly: each IP once, labelled by its first
    observation's enrichment; an empty country code, region or city names no place."""
    first = {}
    for o in rows:
        first.setdefault(o["ip_id"], o)
    located = [o for o in first.values() if o["country_code"]]

    def region(o):
        return o["region_code"] or o["region"]

    return {
        "countries": len({o["country_code"] for o in located}),
        "regions": len({(o["country_code"], region(o)) for o in located if region(o)}),
        "cities": len({(o["country_code"], region(o), o["city"]) for o in located if o["city"]}),
        "asns": len({o["asn"] for o in first.values() if o["asn"] is not None}),
    }


@pytest.mark.asyncio
async def test_every_place_count_is_a_distinct_count_over_the_enrichment(
    database, tmp_path, monkeypatch
):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    seen, original = {}, incremental.Full.window

    def window(self, key, rows, since, hours, *args):
        seen[key, hours] = list(rows)
        return original(self, key, rows, since, hours, *args)

    monkeypatch.setattr(incremental.Full, "window", window)
    policy = lenient_policy(tmp_path)
    for mode in ("full", "incremental"):  # the incremental build reads no observation
        payload = calculate(database, policy, as_of=end, mode=mode)
        by_network = {}
        for g in payload["products"]:
            for label, w in g["windows"].items():
                rows = seen[(g["product_id"], g["cohort"]), w["period"]["hours"]]
                assert {k: w["pool"][k] for k in COUNTS} == direct(rows), (mode, g["id"], label)
                by_network.setdefault((netlists.network_id(g), label), []).extend(
                    {**o, "ip_id": (g["id"], o["ip_id"])} for o in rows
                )
        asns = []
        for entry in payload["networks"]:
            for label, w in entry["windows"].items():
                expected = direct(by_network[entry["id"], label])
                assert {k: w["pool"][k] for k in COUNTS} == expected, (mode, entry["id"], label)
                assert {k: w["pool"]["daily"][k] for k in COUNTS} == expected
                asns.append((w["pool"]["asns"], w["pool"]["list_entries"]["asn"]))
        # The fixture's ASN 64500 has two usage types: some list has more ASN rows than ASNs.
        assert any(n < rows for n, rows in asns)
