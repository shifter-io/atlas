"""LATENCY-BASKET-1 (#1385): the network headline latency over a fixed basket of countries, each
weighing the same, with the slowest network's latency for a basket country a network lacks."""

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scoring_cases import row

from atlas.calculated import latency_basket as lb
from atlas.calculated.latency import KEYS, LATENCY
from atlas.calculated.policy import latency_basket, load, validate
from atlas.calculated.scores import target_score

T0 = datetime(2026, 11, 18, tzinfo=UTC)
HOUR = timedelta(hours=1)
BASKET = ["US", "DE", "JP", "IN"]
SETTINGS = {"basket": BASKET, "aggregate": "mean", "penalty": "slowest_network"}


@pytest.fixture(scope="module")
def shipped():
    return load(Path("configs/methodology.toml"))[0]


def pulses(country, ms, hours=3, **kw):
    """One pulse an hour in country, every success at ms."""
    return [row(T0 + h * HOUR, ms, country=country, **kw) for h in range(hours)]


def latency(p50):
    return dict.fromkeys(KEYS, p50) | {"p50": p50}


def network(provider, served, kind="residential", labels=("24h",), points=()):
    """A network entry whose windows' "_basket" has served ({country: p50}) as its countries'
    latency, and a series point per time of points (each {at: {country: p50}})."""
    windows = {}
    for label in labels:
        own = {c: latency(ms) for c, ms in served.items()}
        at = {t: {c: latency(ms) for c, ms in found.items()} for t, found in points}
        metrics = {"latency_ms": latency(999), "latency_reason": None, "success_rate": 1.0}
        windows[label] = {
            "targets": {"generic_http": {"metrics": metrics}},
            "series": [{"at": t, "latency_ms": dict.fromkeys(LATENCY, 999)} for t, _ in points],
            "_basket": {"target": "generic_http", "window": own, "points": at},
        }
    return {"provider": provider, "kind": kind, "windows": windows}


def headline(entry, label="24h"):
    return entry["windows"][label]["targets"]["generic_http"]["metrics"]


def test_the_shipped_basket_is_the_owners_38_countries_mean_and_slowest_penalty(shipped):
    found = latency_basket(shipped)
    assert len(found["basket"]) == 38 and found["basket"][:3] == ["CN", "US", "IN"]
    assert found["basket"][-3:] == ["DZ", "BE", "PE"]
    assert (found["aggregate"], found["penalty"]) == ("mean", "slowest_network")


@pytest.mark.parametrize(
    "value",
    [
        ["US"],
        {"basket": [], "aggregate": "mean", "penalty": "slowest_network"},
        {"basket": ["US", "US"], "aggregate": "mean", "penalty": "slowest_network"},
        {"basket": ["us"], "aggregate": "mean", "penalty": "slowest_network"},
        {"basket": ["USA"], "aggregate": "mean", "penalty": "slowest_network"},
        {"basket": "US", "aggregate": "mean", "penalty": "slowest_network"},
        {"basket": ["US"], "aggregate": "mode", "penalty": "slowest_network"},
        {"basket": ["US"], "aggregate": "mean", "penalty": "field_median"},
        {"basket": ["US"], "aggregate": "mean"},
        {"basket": ["US"], "aggregate": "mean", "penalty": "slowest_network", "weights": {}},
    ],
)
def test_an_invalid_latency_table_is_refused(shipped, value):
    with pytest.raises(ValueError, match="Invalid latency settings"):
        validate(copy.deepcopy(shipped) | {"latency": value})


def test_without_the_table_there_is_no_basket(shipped):
    config = {k: v for k, v in shipped.items() if k != "latency"}
    assert latency_basket(validate(config)) is None


def test_countries_are_the_basket_ones_with_enough_successes_each_slot_weighing_the_same():
    rows = pulses("US", 200) + pulses("DE", 100, hours=1) + pulses("BR", 50)
    rows += pulses("JP", 300, successes=29)  # under min_latency_successes in every slot
    rows += pulses("IN", 400, target="generic_https")  # not the latency target
    rows += pulses("IN", 700, test=None)  # a daily test: under load
    found = lb.countries(rows, "generic_http", 30, set(BASKET))
    assert sorted(found) == ["DE", "US"]  # BR is not in the basket
    assert found["US"]["p50"] == 200 and found["DE"]["p99"] == 100
    # Slots weigh the same, not requests: two slots at 100 and 300 ms, one with 10x the pulses.
    mixed = [row(T0, 100, n=1000, country="US"), row(T0 + HOUR, 300, country="US")]
    assert lb.countries(mixed, "generic_http", 30, {"US"})["US"]["mean"] == 200


def test_measure_gives_the_window_and_each_series_point():
    rows = pulses("US", 200, hours=2) + [row(T0 + HOUR, 500, country="DE")]
    times = [T0, T0 + HOUR, T0 + 2 * HOUR]
    found = lb.measure(rows, "generic_http", 30, set(BASKET), ((T0, HOUR), times))
    assert found["target"] == "generic_http" and sorted(found["window"]) == ["DE", "US"]
    assert {t: sorted(c) for t, c in found["points"].items()} == {
        T0: ["US"],
        T0 + HOUR: ["DE", "US"],
        T0 + 2 * HOUR: [],
    }


def test_an_unserved_country_counts_as_the_slowest_networks_and_coverage_is_published():
    fast = network("fast", {"US": 100, "DE": 200, "JP": 300})
    slow = network("slow", {"US": 400, "DE": 600})
    mid = network("mid", {"US": 300, "JP": 900})
    lb.apply([fast, slow, mid], {"latency": SETTINGS})
    # IN: no network has it, left out for all; slow's JP is mid's 900 and mid's DE slow's 600.
    assert headline(fast)["latency_ms"]["p50"] == 200  # (100 + 200 + 300) / 3
    # (400 + 600 + 900) / 3: the percentiles in whole ms, the mean to 0.1 ms.
    assert headline(slow)["latency_ms"]["p50"] == 633
    assert headline(slow)["latency_ms"]["mean"] == 633.3
    assert headline(mid)["latency_ms"]["p50"] == 600.0  # (300 + 600 + 900) / 3
    assert set(headline(fast)["latency_ms"]) == set(KEYS)
    assert headline(slow)["latency_basket"] == {
        "size": 4,
        "covered": 2,
        "counted": 3,
        "unserved": ["JP"],
        "unmeasured": ["IN"],
        "aggregate": "mean",
        "penalty": "slowest_network",
        "countries": {"US": 400, "DE": 600, "JP": 900},
        "pooled_ms": latency(999),
    }
    assert "_basket" not in fast["windows"]["24h"]


def test_a_network_serving_every_country_gets_the_mean_of_its_own_countries():
    # Other networks change only their own figures and the penalties they set elsewhere.
    full = {"US": 100, "DE": 200, "JP": 300, "IN": 400}
    for others in ([], [{"US": 900}], [{"US": 50, "DE": 5000}]):
        entries = [network("full", full)] + [network(f"n{i}", o) for i, o in enumerate(others)]
        lb.apply(entries, {"latency": SETTINGS})
        assert headline(entries[0])["latency_ms"]["p50"] == 250.0
        assert headline(entries[0])["latency_basket"]["covered"] == 4


def test_the_median_of_the_basket_values():
    entries = [network("a", {"US": 100, "DE": 200, "JP": 300, "IN": 4000})]
    entries.append(network("b", {"US": 100, "DE": 200, "JP": 300}))
    lb.apply(entries, {"latency": SETTINGS | {"aggregate": "median"}})
    # An even count gives the mean of the middle two; the outlier moves the median not at all.
    assert headline(entries[0])["latency_ms"]["p50"] == 250.0
    assert headline(entries[1])["latency_ms"]["p50"] == 250.0
    alone = network("a", {"US": 100, "DE": 200, "JP": 300, "IN": 4000})
    lb.apply([alone], {"latency": SETTINGS})
    assert headline(alone)["latency_ms"]["p50"] == 1150.0  # the mean feels the outlier


def test_a_network_without_a_basket_country_has_no_latency():
    entries = [network("a", {"US": 100}), network("none", {})]
    lb.apply(entries, {"latency": SETTINGS})
    m = headline(entries[1])
    assert m["latency_ms"] == dict.fromkeys(KEYS) and m["latency_reason"] == lb.NO_BASKET
    assert m["latency_basket"]["covered"] == 0 and m["latency_basket"]["unserved"] == ["US"]
    speed = target_score(m | {"completed": 0, "coverage": 0}, minimal_config())
    assert speed["components"]["speed"]["value"] is None
    assert speed["components"]["speed"]["reason"] == lb.NO_BASKET
    # A window without a pooled latency keeps its own reason.
    quiet = network("quiet", {})
    headline(quiet)["latency_ms"], headline(quiet)["latency_reason"] = dict.fromkeys(KEYS), "x"
    lb.apply([quiet], {"latency": SETTINGS})
    assert headline(quiet)["latency_reason"] == "x"


def test_speed_reads_the_basket_p50():
    entries = [network("a", {"US": 100}), network("b", {"US": 300, "DE": 300})]
    lb.apply(entries, {"latency": SETTINGS})
    m = headline(entries[0]) | {"completed": 0, "coverage": 0}
    assert target_score(m, minimal_config())["components"]["speed"]["value"] == 200.0


def test_kinds_and_windows_have_their_own_penalties():
    a = network("a", {"US": 100}, labels=("24h", "7d"))
    b = network("b", {"US": 100, "DE": 500}, labels=("24h",))
    c = network("c", {"US": 100, "DE": 900}, kind="mobile", labels=("24h", "7d"))
    lb.apply([a, b, c], {"latency": SETTINGS})
    assert headline(a)["latency_ms"]["p50"] == 300.0  # 24h: DE is b's, not c's (mobile)
    assert headline(a, "7d")["latency_ms"]["p50"] == 100.0  # 7d: nobody of its kind has DE
    assert headline(a, "7d")["latency_basket"]["unmeasured"] == ["DE", "JP", "IN"]


def test_series_points_follow_the_rule_at_each_time():
    t1, t2 = T0, T0 + HOUR
    a = network("a", {"US": 100}, points=[(t1, {"US": 100}), (t2, {})])
    b = network("b", {"US": 300, "DE": 500}, points=[(t1, {"US": 300, "DE": 500}), (t2, {})])
    lb.apply([a, b], {"latency": SETTINGS})
    first, second = a["windows"]["24h"]["series"]
    assert first["latency_ms"] == dict.fromkeys(LATENCY, 300.0)  # (100 + 500) / 2
    assert second["latency_ms"] == dict.fromkeys(LATENCY)  # no country of its own: a gap
    assert b["windows"]["24h"]["series"][0]["latency_ms"]["p50"] == 400.0


def test_without_the_table_nothing_changes_but_the_private_key_goes():
    entry = network("a", {"US": 100})
    before = copy.deepcopy(headline(entry))
    lb.apply([entry], {})
    assert headline(entry) == before and "_basket" not in entry["windows"]["24h"]


def minimal_config():
    return {
        "minimum_requests": 1,
        "minimum_coverage": 0,
        "target_weights": {"reliability": 80, "speed": 20},
        "speed_latency": "light_load",
    }


def test_a_window_without_the_latency_target_is_left_as_it_is():
    entry, other = network("a", {"US": 100}), network("b", {"US": 300})
    entry["windows"]["24h"]["targets"] = {}
    lb.apply([entry, other], {"latency": SETTINGS})
    assert entry["windows"]["24h"]["targets"] == {}
    assert headline(other)["latency_ms"]["p50"] == 300
