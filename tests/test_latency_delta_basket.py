"""LATENCY-DELTA-1 (#1386): the 24h and 7d latency changes compare each period's basket p50, the
headline's rule (LATENCY-BASKET-1): its own basket countries' p50 and the slowest measured
network's of the same kind and window in the others, so the change follows latencyP50."""

import pytest
from scoring_cases import row
from test_latency_days import AS_OF, COUNTRIES, HOUR, day, nov

from atlas.calculated import latency_basket as lb
from atlas.calculated.latency_days import NEEDS_TWO_DAYS
from atlas.calculated.timeout_latency import with_timeouts

BASKET = list(COUNTRIES)
CONFIG = {"latency": {"basket": BASKET, "aggregate": "mean", "penalty": "slowest_network"}}
WIDE = ["US", "DE", "FR", "GB", "JP", "IN", "BR", "CA", "AU", "NL"]


def entry(provider, rows, label="24h", days=1, basket=BASKET, held=False):
    own = lb.periods(rows, "generic_http", 30, set(basket), AS_OF, days)
    window = {"_delta": own} | ({"_held": None} if held else {})
    return {"provider": provider, "kind": "residential", "windows": {label: window}}


def delta(network, label="24h"):
    return network["windows"][label]["latency_day_delta"]


def p50s(found):
    return [d["p50"] for d in found["days"]]


def headline(networks, start):
    """The basket p50 of each network's rows of the day from start, as latency_basket.apply()
    makes a window's headline."""
    owns = [
        lb.countries([r for r in rows if start <= r["bucket_start"] < start + 24 * HOUR], *own)
        for rows, own in networks
    ]
    penalty = lb.slowest(owns, BASKET)
    return [lb.basket_latency(o, penalty, "mean")[0]["p50"] for o in owns]


def test_the_change_compares_the_basket_p50_with_the_slowest_networks_penalty():
    # a serves every basket country, 1000 then 1100 ms; b only US and DE, 500 ms on both days.
    a_rows = day(nov(14), 1000) + day(nov(15), 1100)
    b_rows = day(nov(14), 500, COUNTRIES[:2]) + day(nov(15), 500, COUNTRIES[:2])
    a, b = entry("a", a_rows), entry("b", b_rows)
    lb.deltas([a, b], CONFIG)
    assert "_delta" not in a["windows"]["24h"]
    assert delta(a)["pct"] == pytest.approx(10.0) and p50s(delta(a)) == [1000, 1100]
    # b's own latency did not move, but its three missing countries count at a's latency, as
    # its headline does: (500 + 500 + 3 × 1000) / 5 = 800, then 860.
    assert delta(b)["pct"] == pytest.approx(7.5) and p50s(delta(b)) == [800, 860]
    own = ("generic_http", 30, set(BASKET))
    for n, start in enumerate((nov(14), nov(15))):
        expected = headline([(a_rows, own), (b_rows, own)], start)
        assert expected == [p50s(delta(a))[n], p50s(delta(b))[n]]
    assert [d["countries"] for d in delta(b)["days"]] == [2, 2]


def test_a_timeout_counts_in_the_change_at_its_runs_limit():
    # TIMEOUT-LATENCY-1: on 15 Nov 60 of US's 100 requests time out at 10 s, 40 succeed at 500 ms.
    late = row(nov(15), 500, successes=40, country="US")
    late["counters"] = with_timeouts(late["counters"] | {"errors": {"timeout": 60}}, 10_000)
    rows = day(nov(14), 500, ["US"]) + [late]
    found = entry("a", rows, basket=["US"])
    lb.deltas([found], {"latency": CONFIG["latency"] | {"basket": ["US"]}})
    assert p50s(delta(found)) == [500, 10_000]
    assert lb.countries([late], "generic_http", 30, {"US"})["US"]["p50"] == 10_000


def test_a_pair_that_lost_countries_compares_the_common_ones_the_others_at_their_penalty():
    # a lost AU and NL on 15 Nov: 8 of 10 countries fail the 90% rule, so it compares the 8
    # countries of both days; AU and NL count at b's 2000 ms on both.
    a_rows = day(nov(14), 1000, WIDE) + day(nov(15), 1100, WIDE[:8])
    b_rows = day(nov(14), 2000, WIDE) + day(nov(15), 2000, WIDE)
    a, b = (entry(n, rows, basket=WIDE) for n, rows in (("a", a_rows), ("b", b_rows)))
    lb.deltas([a, b], {"latency": CONFIG["latency"] | {"basket": WIDE}})
    assert delta(a)["scope"] == "common" and p50s(delta(a)) == [1200, 1280]
    assert delta(a)["pct"] == pytest.approx(80 / 12)
    assert delta(b)["pct"] == pytest.approx(0.0) and delta(b)["scope"] == "all"


def test_a_held_window_takes_no_part_in_the_measured_ones_penalties():
    # h (held) alone has JP: for a, JP is a country no measured network has, so it is left out.
    a = entry("a", day(nov(14), 1000, COUNTRIES[:4]) + day(nov(15), 1100, COUNTRIES[:4]))
    h = entry("h", day(nov(14), 3000, ["JP"]) + day(nov(15), 3000, ["JP"]), held=True)
    lb.deltas([a, h], CONFIG)
    assert p50s(delta(a)) == [1000, 1100]
    # h's own JP counts in its own penalties: (4 × 1000 + 3000) / 5, then (4 × 1100 + 3000) / 5.
    assert p50s(delta(h)) == [1400, 1480]


def test_the_7d_change_falls_back_to_the_day_and_a_network_without_a_basket_country_has_none():
    rows = day(nov(14), 1000) + day(nov(15), 1100)
    week = entry("a", rows, label="7d", days=7)
    elsewhere = entry("x", day(nov(14), 900, ["XX"]) + day(nov(15), 900, ["XX"]), "7d", 7)
    lb.deltas([week, elsewhere], CONFIG)
    found = delta(week, "7d")
    assert found["pct"] == pytest.approx(10.0)
    assert [d["end"] - d["start"] for d in found["days"]] == [24 * HOUR] * 2
    assert delta(elsewhere, "7d")["pct"] is None
    assert delta(elsewhere, "7d")["reason"] == NEEDS_TWO_DAYS


def test_without_a_basket_nothing_is_compared():
    plain = {"provider": "a", "kind": "residential", "windows": {"24h": {}}}
    lb.deltas([plain], {})
    assert plain["windows"]["24h"] == {}
