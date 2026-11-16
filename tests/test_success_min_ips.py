"""SUCCESS-MIN-IPS-1 (#1214, the owner's decision of 2026-11-16): the requests of a country whose
verdict found fewer than the methodology's success_min_country_ips unique IPs do not vote on the
time-slot success rate; they still count as requests."""

from datetime import UTC, datetime, timedelta

import pytest
from calculated_cases import POLICY, lenient_policy
from scoring_cases import T, row
from test_calculated_country_verdicts import verdict
from test_calculated_hourly_reads import HOUR, floor_hour
from test_calculated_incremental import assert_equal, history

from atlas import verdicts
from atlas.calculated import policy, store, target_scores
from atlas.calculated.aggregate import SeriesSpec, series, timed
from atlas.calculated.calculator import _run_fields, calculate
from atlas.calculated.fleet import merged
from atlas.calculated.network import slot_runs

pytest_plugins = ["test_storage"]

SLOT = timedelta(hours=1)


def small(r, ips=499, minimum=500):
    """Row or pulse r of a country whose verdict found `ips` unique IPs."""
    return r | {"few_ips": verdicts.few_ips(ips, minimum)}


def day(minimum=500, small_ips=499):
    """24 hourly slots of a big country at 99%, each with 100 failed requests of a small one."""
    rows = [row(T + n * SLOT, 100, successes=99) for n in range(24)]
    rows += [small(row(T + n * SLOT, 100, successes=0), small_ips, minimum) for n in range(24)]
    return rows


def test_a_country_under_the_minimum_does_not_vote_but_its_requests_count():
    m = timed(day(), 24, 1, slot_minutes=60)
    assert m["success_rate"] == pytest.approx(0.99)
    assert m["completed"] == 24 * 200 and m["request_success_rate"] == pytest.approx(0.495)
    assert m["time_slots"]["few_ips"] == 24 * 100 and m["time_slots"]["success"] == 24
    assert m["time_slots"]["down"] == 0


def test_off_or_at_the_minimum_every_country_votes():
    before = timed([r | {"few_ips": False} for r in day()], 24, 1, slot_minutes=60)
    for rows in (day(minimum=0), day(small_ips=500)):  # off, and a country with exactly 500
        m = timed(rows, 24, 1, slot_minutes=60)
        assert m == before and m["success_rate"] == pytest.approx(0.495)
        assert m["time_slots"]["few_ips"] == 0
    assert not verdicts.few_ips(None, 500)  # no verdict: country_verdicts decides
    assert verdicts.few_ips(499, 500) and not verdicts.few_ips(0, 0)


def test_a_slot_of_only_small_countries_does_not_vote_not_even_a_zero():
    rows = [row(T, 100, successes=99), small(row(T + SLOT, 100, successes=0))]
    pulse = {"slot": T + 2 * SLOT, "requests": 100, "successes": 0, "counted": False}
    m = timed(rows, 24, 1, [small(pulse)], 60)
    assert m["success_rate"] == pytest.approx(0.99)
    assert m["time_slots"]["success"] == 1 and m["time_slots"]["down"] == 0
    assert m["time_slots"]["few_ips"] == 200
    only = timed([small(r) for r in rows], 24, 1, slot_minutes=60)
    assert only["success_rate"] is None and only["completed"] == 200
    assert only["latency_ms"]["p50"] == 100  # latency is not changed here


def test_the_series_leaves_them_out_of_each_points_success_only():
    spec = SeriesSpec(T, T + 2 * SLOT, 24)
    points = series([row(T, 100, successes=99), small(row(T, 100, successes=0))], (), spec)
    assert points[0]["success_rate"] == pytest.approx(0.99)
    assert points[0]["completed"] == 200 and points[0]["successes"] == 99
    assert points[1]["success_rate"] is None


def test_the_fleet_keeps_small_countries_apart_when_it_merges_rows():
    rows = [row(T, 100, successes=99), small(row(T, 100, successes=0))]
    together = merged(rows)
    assert sorted(r["few_ips"] for r in together) == [False, True]
    assert timed(together, 24, 1, slot_minutes=60) == timed(rows, 24, 1, slot_minutes=60)


def test_runs_and_pulses_carry_the_flag_from_their_verdict():
    run = {"scope": {"target": {"id": "generic_http"}, "config": {}}, "role": "monitor"}
    run |= {"light": True, "finished_at": T, "verdict_ips": 499, "batch_id": 1}
    assert _run_fields(run, [], 3600, 500).fields["few_ips"] is True
    assert _run_fields(run, [], 3600).fields["few_ips"] is False  # the setting off
    batch = {"scheduled_for": T, "target_id": "generic_http", "requests": 1, "successes": 1}
    batch |= {"product_id": 1, "few_ips": True}
    assert slot_runs([batch], 3600, {})[0]["few_ips"] is True


def windows(rate, few=0):
    """A product with one 24h window whose generic_http has `rate` over 1,000 requests."""
    m = {"completed": 1000, "success_rate": rate, "challenge_rate": 0.0, "block_rate": 0.0}
    m["time_slots"] = {"few_ips": few}
    return {"windows": {"24h": {"targets": {"generic_http": {"metrics": m}}}}}


def test_a_network_with_only_small_countries_has_no_success_score_not_a_zero():
    products = [windows(0.99), windows(0.9), windows(0.8), windows(None, 1000), windows(None)]
    target_scores.score(products, {}, {})
    found = [p["windows"]["24h"] for p in products]
    success = [w["targets"]["generic_http"]["scores"]["success"] for w in found]
    assert [s["status"] for s in success] == ["ranked"] * 3 + ["too_few_ips", "not_enough_data"]
    assert success[3]["score"] is None and success[3]["rank"] is None
    assert found[3]["success_overall"]["status"] == target_scores.NO_RANKED_TARGETS
    assert found[3]["success_overall"]["score"] is None
    assert [w["success_overall"]["rank"] for w in found[:3]] == [1, 2, 3]


def test_the_setting_is_validated_and_set_for_production():
    config, _ = policy.load(POLICY)
    assert policy.success_min_country_ips(config) == 500
    assert policy.success_min_country_ips({}) == 0
    for value in (-1, 1.5, "500", True):
        with pytest.raises(ValueError, match="Invalid success_min_country_ips"):
            policy.validate(config | {"success_min_country_ips": value})
    with pytest.raises(ValueError, match="Invalid success_min_country_ips"):
        policy.validate(config | {"country_verdicts": False})
    assert policy.validate(config | {"country_verdicts": False, "success_min_country_ips": 0})


def daily_metrics(payload, provider, country, label="7d"):
    """The window metrics of a provider's daily-test group of `country`."""
    for p in payload["products"]:
        if p["provider"] == provider and (p["criteria"].get("country") or "").upper() == country:
            return p["windows"][label]["metrics"]
    raise KeyError((provider, country))


@pytest.mark.country_verdicts
@pytest.mark.asyncio
async def test_the_built_success_leaves_out_a_country_under_the_minimum(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    verdict(database, "alpha", "US", end - 71 * HOUR, 600, -1)  # votes
    verdict(database, "alpha", "DE", end - 60 * HOUR, 499, -2)  # counts, does not vote
    methodology = lenient_policy(tmp_path)
    payload = assert_equal(database, methodology, end)  # the incremental build equals the full
    us, de = (daily_metrics(payload, "alpha", c) for c in ("US", "DE"))
    assert us["success_rate"] is not None and us["time_slots"]["few_ips"] == 0
    assert de["completed"] == 3 * 40 and de["time_slots"]["few_ips"] == 3 * 40
    assert de["success_rate"] is None
    off = tmp_path / "off.toml"
    text = methodology.read_text()
    off.write_text(text.replace("success_min_country_ips = 500", "success_min_country_ips = 0"))
    build = calculate(database, off, as_of=end, mode="full")
    de = daily_metrics(store.read(database, build["snapshot_id"]), "alpha", "DE")
    assert de["success_rate"] is not None and de["time_slots"]["few_ips"] == 0
