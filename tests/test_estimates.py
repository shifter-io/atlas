"""ESTIMATE-1: the estimated hours of a daily test, their hourly release and the windows.

The arithmetic (measured x factor) against the committed formulas and the windows' figures. The
stored rows and their release are in test_estimates_release.py, the ingest hook and the hourly
calculation trigger in test_estimates_jobs.py. No database outside the test is touched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from estimates_cases import T1, T2, formula, product_window

from atlas.estimates import compute, window

FORMULAS = Path("formulas")


# --- arithmetic ----------------------------------------------------------------------------


@pytest.mark.parametrize("network", sorted(p.stem for p in FORMULAS.glob("*.json")))
def test_every_committed_formula_gives_measured_times_factor_at_its_test_hour(network):
    f = compute.load_formula(FORMULAS, network)
    for code, entry in f["countries"].items():
        if entry["daily_factor"] is None or entry["hour_factors"][entry["test_hour"]] != 1.0:
            continue  # no estimate possible, or a test hour off the peak: covered below
        at = T1.replace(hour=entry["test_hour"], minute=7)
        rows = compute.hours(f, code, at, 200, 1000)
        assert rows[0]["hour"] == entry["test_hour"] and rows[0]["unique_ips"] == 200
        for row in rows:
            factor = entry["hour_factors"][row["hour"]]
            assert row["estimated"] is (factor is not None) and row["curve"] == entry["curve"]
            if factor is None:  # no data: not estimated, never 0
                assert row["unique_ips"] is row["requests"] is None and row["reason"]
                continue
            assert row["unique_ips"] > 0
            assert row["unique_ips"] == pytest.approx(200 * factor)
            assert row["day_unique_ips"] == pytest.approx(200 * entry["daily_factor"])
        network = entry["curve"] == "network"
        curve = f["network_curve"]["requests"] if network else entry["requests"]
        last = [row for row in rows if row["estimated"]][-1]
        if entry["hour_basis"][last["hour"]] in compute.FALLBACK_HOURS:
            assert last["requests"] == pytest.approx(1000 * last["hour_factor"])
        elif curve[entry["test_hour"]]:  # else requests follow the IP factors
            expected = 1000 * curve[last["hour"]] / curve[entry["test_hour"]]
            assert last["requests"] == pytest.approx(expected)


def test_hours_roll_into_the_next_day_and_skip_hours_without_a_factor():
    f = formula(DE={}, FR={"curve": "network"})
    rows = compute.hours(f, "DE", datetime(2026, 10, 1, 21, 59, tzinfo=UTC), 30, 600)
    assert [(r["day"].day, r["hour"]) for r in rows[:4]] == [(1, 21), (1, 22), (1, 23), (2, 0)]
    assert len(rows) == 24 and [r["hour"] for r in rows if not r["estimated"]] == [3]
    unset = next(r for r in rows if r["hour"] == 3)  # no factor: not estimated, not 0
    assert unset["unique_ips"] is unset["requests"] is unset["hour_factor"] is None
    assert unset["reason"].startswith("no data")
    assert rows[0]["hour_factor"] == 1.0 and rows[0]["requests"] == 600
    assert rows[1]["unique_ips"] == pytest.approx(30 * 23 / 22)
    assert rows[1]["requests"] == pytest.approx(600 * 2300 / 2200)
    assert rows[0]["day_unique_ips"] == pytest.approx(30 * 3.0 / (22 / 24))
    network = compute.hours(f, "FR", datetime(2026, 10, 1, 21, tzinfo=UTC), 30, 600)
    assert {r["requests"] for r in network} == {600, None} and network[0]["curve"] == "network"
    local = datetime(2026, 10, 2, 1, 30, tzinfo=UTC).astimezone(timezone(timedelta(hours=4)))
    assert compute.hours(f, "DE", local, 1, 1)[0]["hour"] == 1  # UTC hours, whatever the zone
    with pytest.raises(ValueError, match="hour 3 has no factor"):
        compute.hours(f, "DE", datetime(2026, 10, 1, 3, tzinfo=UTC), 1, 1)
    with pytest.raises(ValueError, match="not in the formula"):
        compute.hours(f, "XX", T1, 1, 1)


def test_a_test_counts_for_and_is_scaled_by_its_scheduled_hour():
    # ESTIMATE-SCHED-1 (#1152): IPRoyal's catch-up ran at 10:26 the countries due at 21:00.
    f = formula(DE={})
    late = datetime(2026, 10, 1, 10, 26, tzinfo=UTC)
    rows = compute.hours(f, "DE", late, 30, 600, scheduled=late.replace(hour=21, minute=0))
    assert (rows[0]["day"].day, rows[0]["hour"]) == (1, 21) and rows[0]["unique_ips"] == 30
    assert rows[0]["day_unique_ips"] == pytest.approx(30 * 3.0 / (22 / 24))
    # Without a scheduled hour the run's start counts, as before.
    assert compute.hours(f, "DE", late, 30, 600)[0]["hour"] == 10


def test_no_test_is_scaled_by_an_hour_factor_below_the_floor():
    f = formula(DE={})  # hour 1's factor is 2 / 24
    rows = {r["hour"]: r for r in compute.hours(f, "DE", T1.replace(hour=1), 30, 600)}
    assert compute.BASE_FLOOR == 0.5
    assert rows[1]["day_unique_ips"] == pytest.approx(30 * 3.0 / 0.5)  # not 30 x 3 x 12
    assert rows[1]["unique_ips"] == pytest.approx(30 * (2 / 24) / 0.5)
    assert rows[23]["unique_ips"] == pytest.approx(30 * 1.0 / 0.5)
    above = compute.hours(f, "DE", T1.replace(hour=21), 30, 600)[0]  # 22 / 24: no floor
    assert above["day_unique_ips"] == pytest.approx(30 * 3.0 / (22 / 24))


def test_an_hour_on_the_network_curve_scales_requests_by_its_factor():
    basis = ["country"] * 24
    basis[8] = "network_missing"
    entry = {"hour_basis": basis}
    entry["requests"] = [100] * 24
    entry["requests"][8] = 0
    f = formula(DE=entry)
    f["countries"]["DE"]["hour_factors"][8] = 0.5
    rows = {r["hour"]: r for r in compute.hours(f, "DE", T1.replace(hour=23), 30, 600)}
    base = 1.0  # factor at hour 23
    assert rows[8]["unique_ips"] == pytest.approx(30 * 0.5 / base)
    assert rows[8]["requests"] == pytest.approx(600 * 0.5)
    assert rows[9]["requests"] == pytest.approx(600)  # own request curve elsewhere
    # Measured in a fallback hour: every hour's requests follow the factors.
    measured = {r["hour"]: r for r in compute.hours(f, "DE", T1.replace(hour=8), 30, 600)}
    assert measured[23]["requests"] == pytest.approx(600 * 1.0 / 0.5)


def test_only_a_version_1_hour_formula_loads(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"schema": compute.SCHEMA, "schema_version": 2}))
    with pytest.raises(ValueError, match="not a version 1"):
        compute.load_formula(tmp_path, "a")


def rows_of(run_key, product=1, country="DE", measured=2, day_unique=10.0, version=1):
    return {
        "run_key": run_key,
        "product_id": product,
        "country_code": country,
        "measured_ips": measured,
        "day_unique_ips": day_unique,
        "unique_ips": measured / 2,
        "requests": 7.0,
        "formula_version": version,
        "hour_start": T1 + timedelta(hours=run_key),
    }


def test_window_unique_ips_count_an_ip_seen_on_several_days_once():
    # Two days of tests of the same 2 IPs: the sum counts them twice, distinct once.
    rows = [rows_of(1), rows_of(1), rows_of(2)]
    assert window.window_unique("sum", rows, 2) == 20.0
    assert window.window_unique("distinct", rows, 2) == 10.0
    assert window.window_unique("distinct", rows, 3) == 15.0  # one new IP on day 2
    assert window.window_unique("distinct", [rows_of(1, measured=0)], 0) == 0.0
    with pytest.raises(ValueError, match="window_unique"):
        window.window_unique("average", rows, 2)


def test_place_flags_every_estimated_figure_with_its_formula_version():
    groups = {
        (1, "c"): {
            "product_id": 1,
            "windows": {
                "24h": product_window(T1 + timedelta(hours=2), 24),
                "7d": product_window(T1),
            },
        }
    }
    hours = [rows_of(1, version=1), rows_of(2, version=2), rows_of(3, country="FR")]
    hours.append(rows_of(9, product=2))  # a run outside the calculation: ignored
    run_group = dict.fromkeys((1, 2, 3), (1, "c"))
    found = window.windows_with(groups, run_group, hours)
    assert [len(rows) for _, rows in found] == [2, 3]  # 24h, 7d
    window.place(found, [{}, {None: 2}], {"estimates": {"window_unique": "sum"}})
    week = groups[(1, "c")]["windows"]["7d"]["estimated"]
    day = groups[(1, "c")]["windows"]["24h"]["estimated"]
    assert week["estimated"] is True and week["formula_versions"] == [1, 2] and week["hours"] == 3
    assert week["unique_ips"] == {
        "value": 30.0,
        "estimated": True,
        "formula_version": 2,
        "method": "sum",
    }
    assert week["requests"]["estimated"] and week["requests"]["value"] == 21.0
    assert [(c["country_code"], c["formula_version"]) for c in week["countries"]] == [
        ("DE", 2),
        ("FR", 1),
    ]
    assert all(point["estimated"] for point in week["series"]) and len(week["series"]) == 3
    assert week["breakdown_scale"] == 30.0 / 4
    assert week["carried"] == {
        "shared_share": 0.5,
        "datacenter_asn_share": 0.25,
        "latency": "measured",
    }
    assert day["hours"] == 2  # the 24h window starts at the second hour
    groups[(1, "c")]["windows"]["24h"]["period"]["start"] = T2
    del groups[(1, "c")]["windows"]["24h"]["estimated"]
    found = window.windows_with(groups, run_group, hours)
    window.place(found, [{None: 2}], {})
    assert "estimated" not in groups[(1, "c")]["windows"]["24h"]
    assert groups[(1, "c")]["windows"]["7d"]["estimated"]["unique_ips"]["method"] == "distinct"
    assert window.windows_with(groups, run_group, []) == []  # nothing released


def hour_of(run_key, offset, **kw):
    """A released hour of test run_key, offset hours after T1."""
    return rows_of(run_key, **kw) | {"hour_start": T1 + timedelta(hours=offset)}


def test_a_24h_window_counts_each_countrys_most_recent_test_once(monkeypatch):
    # #1117: yesterday's DE test (run 1) still holds the window's first hours, today's (run 5)
    # the latest ones. A test's figure is a whole day's, so the 24h unique IPs count today's DE
    # test and FR's only; the 7d window counts every test; requests, hours and the hourly
    # series keep every released hour.
    groups = {(1, "c"): {"windows": {"24h": product_window(T1, 24), "7d": product_window(T1)}}}
    hours = [hour_of(1, h, measured=2, day_unique=10.0) for h in (0, 1, 2)]
    hours += [hour_of(5, h, measured=3, day_unique=12.0) for h in (3, 4)]
    hours.append(hour_of(7, 4, country="FR", measured=2, day_unique=8.0))
    found = window.windows_with(groups, dict.fromkeys((1, 5, 7), (1, "c")), hours)
    seen = []
    monkeypatch.setattr(window, "window_distinct", lambda _c, sets, _b: seen.extend(sets) or [])
    window.distinct(None, found)
    assert [sorted(s) for s in seen] == [[(5, "DE"), (7, "FR")], [(1, "DE"), (5, "DE"), (7, "FR")]]
    window.place(found, [{None: 5, "DE": 3, "FR": 2}, {None: 7, "DE": 5, "FR": 2}], {})
    day = groups[(1, "c")]["windows"]["24h"]["estimated"]
    assert day["unique_ips"]["value"] == pytest.approx(5 * (12 + 8) / (3 + 2))
    assert [(c["country_code"], c["unique_ips"]) for c in day["countries"]] == [
        ("DE", pytest.approx(12.0)),
        ("FR", pytest.approx(8.0)),
    ]
    assert day["hours"] == 6 and day["requests"]["value"] == pytest.approx(42.0)
    assert [p["start"] for p in day["series"]] == [T1 + timedelta(hours=h) for h in range(5)]
    assert day["breakdown_scale"] == pytest.approx(20.0 / 4)
    week = groups[(1, "c")]["windows"]["7d"]["estimated"]
    assert week["unique_ips"]["value"] == pytest.approx(7 * (10 + 12 + 8) / (2 + 3 + 2))
    # A tie on the latest hour goes to the larger run key; a window without hours keeps all.
    tie = [hour_of(2, 0), hour_of(3, 0)]
    assert [r["run_key"] for r in window.counted({"period": {"hours": 24}}, tie)] == [3]
    assert window.counted({"period": {}}, tie) == tie
