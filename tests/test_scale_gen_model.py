"""SCALE-GEN-1a: the synthetic data model and its row plan (no database)."""

import importlib
import ipaddress
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from proxy_index.estimates.compute import load_formula
from proxy_index.regions import load_countries

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scale"))
synthetic = importlib.import_module("synthetic")

FORMULAS = Path(__file__).resolve().parents[1] / "formulas"
NETWORKS, COUNTRIES = ("decodo", "iproyal"), ("AQ", "DE", "US")  # AQ: in no formula


def model(seed=7, days=3, scale=0.004, countries=COUNTRIES):
    formulas = {name: load_formula(FORMULAS, name) for name in NETWORKS}
    settings = synthetic.Settings(seed, NETWORKS, countries, days, date(2026, 10, 4), scale)
    return synthetic.Model(settings, formulas, load_countries())


def test_addresses_are_distinct_and_only_synthetic():
    seen = {synthetic.address(n) for n in range(4000)}
    assert len(seen) == 4000
    assert ipaddress.ip_address(synthetic.address(0)) == ipaddress.ip_address(0xF0000000)
    assert synthetic.address(3).startswith("2001:db8::")
    synthetic_space = (ipaddress.ip_network((0xF0000000, 4)), ipaddress.ip_network("2001:db8::/32"))
    assert all(any(ipaddress.ip_address(a) in net for net in synthetic_space) for a in seen)
    # Past the IPv4 space every index is IPv6, and still distinct.
    late = 4 * synthetic.V4_SPACE // 3 + 8
    assert synthetic.address(late).startswith("2001:db8::")
    assert synthetic.address(late) != synthetic.address(late + 1)


def test_apportion_settings_and_days():
    assert synthetic.apportion(10, [1, 1, 1]) == [4, 3, 3]
    assert synthetic.apportion(0, [2, 1]) == [0, 0]
    assert sum(synthetic.apportion(97, synthetic.WEIGHTS)) == 97
    settings = synthetic.Settings(3, ("a",), ("DE",), 5, date(2026, 10, 3), 0.5)
    assert synthetic.Settings.from_json(json.loads(json.dumps(settings.to_json()))) == settings
    assert settings.day_start(5) == datetime(2026, 10, 2, tzinfo=UTC)
    assert settings.day_start(1) == datetime(2026, 9, 28, tzinfo=UTC)
    with pytest.raises(ValueError, match="another generator version"):
        synthetic.Settings.from_json(settings.to_json() | {"version": 0})


def test_each_day_reuses_its_share_of_the_day_befores_ips():
    m = model(days=5)
    for lane in m.lanes:
        assert 0.3 <= lane.reuse <= 0.6
        previous = None
        for day in range(1, 6):
            start, size = m.window(lane, day)
            if previous:
                assert previous[0] + previous[1] - start == round(lane.reuse * size)
            previous = (start, size)
        # New IPs of the days are disjoint and together cover every position used.
        spans = [m.new(lane, day) for day in range(1, 6)]
        assert all(a[1] == b[0] for a, b in zip(spans, spans[1:], strict=False))
        assert spans[-1][1] == max(sum(m.window(lane, d)) for d in range(1, 6))


def test_lanes_own_disjoint_ip_blocks_and_borrow_from_their_neighbour():
    m = model()
    ends = [lane.offset + max(sum(m.window(lane, d)) for d in (1, 2, 3)) for lane in m.lanes]
    assert all(end <= lane.offset for end, lane in zip(ends, m.lanes[1:], strict=False))
    assert m.ip_space == ends[-1]
    us = m.lane(1, 2)  # iproyal US: large enough to borrow
    test = m.test(us, 2)
    other = m.neighbour(us)
    assert (other.network, other.country) == (1, 0)
    first, size = m.window(other, 2)
    assert test.borrowed == min(round(synthetic.SHARED * test.size), size) > 0
    indexes = list(test.indexes())
    assert len(indexes) == len(set(indexes)) == test.ips
    assert indexes[test.size] == other.offset + first
    assert test.rows == test.ips + (test.ips + 2) // 3
    assert test.successes >= test.rows and test.errors == test.successes // 50
    assert 2 <= test.buckets <= 4 and test.started_at.hour == us.hour
    assert model(countries=("DE",)).neighbour(model(countries=("DE",)).lanes[0]) is None


def test_a_country_without_a_formula_uses_the_network_curve():
    m = model()
    aq, de = m.lane(0, 0), m.lane(0, 1)
    assert aq.entry["curve"] == "network" and de.entry["curve"] != "network"
    assert aq.entry["hour_factors"][aq.hour]
    rows = m.estimates(m.test(aq, 1))
    assert rows and all(row["estimated"] for row in rows)
    assert len(m.estimates(m.test(de, 1))) == 24
    assert synthetic._fallback({"schedule": {"DE": 3}, "countries": {}}) == (100, 200.0)


def test_enrichment_places_ips_in_their_country_and_leaves_a_few_unmatched():
    m = model()
    places = m.places[2]
    found = [m.enrichment(n, places) for n in range(20000)]
    unmatched = sum(1 for _, _, matched in found if not matched)
    assert 40 <= unmatched <= 160  # about 5 per 1000
    for location, network, matched in found:
        if matched:
            assert places.location <= location < places.location + places.cities
            assert places.network <= network < places.network + places.asns
        else:
            assert (location, network) == (1, 1)


def test_a_pulse_has_47_to_92_distinct_rows_that_add_up_to_100_requests():
    m = model()  # scale 0.004: every country's tests are tiny, its lists at their floor
    assert all(p.cities >= 31 and p.asns >= 5 for p in m.places)
    for lane in m.lanes:
        for hour in range(24):
            errors, rows = m.pulse(lane, 1, hour)
            assert 0 <= errors <= 3
            assert sum(row[2] for row in rows) == synthetic.PULSE_REQUESTS
            assert sum(row[3] for row in rows) == synthetic.PULSE_REQUESTS - errors
            assert len({row[:2] for row in rows}) == len(rows)
            assert synthetic.GEO_ROWS[0] <= len(rows) <= synthetic.GEO_ROWS[1]
    assert m.latency(0) == model().latency(0) and m.latency(0) != model(seed=8).latency(0)


def test_the_model_is_deterministic_for_a_seed():
    a, b, c = model(), model(), model(seed=8)
    assert [a.test(lane, 2) for lane in a.lanes] == [b.test(lane, 2) for lane in b.lanes]
    assert a.pulse(a.lanes[3], 2, 5) == b.pulse(b.lanes[3], 2, 5)
    assert [lane.reuse for lane in a.lanes] != [lane.reuse for lane in c.lanes]


def test_the_plan_counts_rows_per_table():
    m = model()
    plan = synthetic.plan(m, [1, 2])
    lanes = len(m.lanes)
    assert plan["test_runs"] == lanes * 25 * 2 and plan["batch_runs"] == lanes * 24 * 2
    assert plan["test_batches"] == 3 * 24 * 2 and plan["dbip_releases"] == 2
    assert (
        plan["ip_addresses"]
        == plan["product_ips"]
        == sum(m.new(lane, 2)[1] - m.new(lane, 1)[0] for lane in m.lanes)
    )
    assert plan["ip_observations"] == sum(m.test(lane, d).rows for lane in m.lanes for d in (1, 2))
    assert plan["place_names"] == 1 + sum(p.cities for p in m.places)
    assert plan["pool_ips"] >= plan["ip_addresses"]


def test_borrowed_ips_are_counted_once_across_days():
    class Fake:
        def __init__(self, spans):
            self.spans = spans

        def test(self, lane, day):
            first, after = self.spans[day - 1]
            return synthetic.Test(lane, day, 0, 0, 1, after - first, first, 2, None, 1)

    assert synthetic._borrowed_union(Fake([(0, 5), (3, 8), (10, 12)]), None, [1, 2, 3]) == 10
    assert synthetic._borrowed_union(Fake([(0, 5), (1, 3), (5, 5)]), None, [1, 2, 3]) == 5
