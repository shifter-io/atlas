"""Uptime over the measured time ([uptime] basis = "measured_time", UPTIME-MEASURED-1) against
the "window" basis: outage_time.outage_uptime() with tested runs, and the score component."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from atlas.calculated import policy
from atlas.calculated.outage_time import measured_spans, outage_uptime
from atlas.calculated.scores import UPTIME_UNMEASURED, _uptime

SHIPPED = policy.load(Path(__file__).parents[1] / "configs/methodology.toml")[0]
END = datetime(2026, 11, 10, tzinfo=UTC)
DAY = timedelta(days=1)


def ago(days):
    return END - days * DAY


def run(country, start, last):
    return {"country": country, "started_at": start, "last": last}


def down(country, start, minutes):
    stop = start + timedelta(minutes=minutes)
    return {"country": country, "started_at": start, "ended_at": stop}


def latest(runs):
    """The tested map (country -> end of its latest run) of tested_by_product()."""
    out = {}
    for r in runs:
        out[r["country"]] = max(out.get(r["country"], r["last"]), r["last"])
    return out


def both(outages, runs, since, end=END):
    return (
        outage_uptime(outages, latest(runs), since, end),
        outage_uptime(outages, latest(runs), since, end, runs),
    )


def test_a_country_measured_two_of_seven_days_divides_by_its_two_days():
    # Shifter's case: 13.2 min down, pulses every 30 min over the last 2 days of a 7d window.
    starts = [ago(2) + i * timedelta(minutes=30) for i in range(96)]
    runs = [run("US", t, t + timedelta(seconds=50)) for t in starts]
    runs[-1]["last"] = END
    outages = [down("US", ago(1), 13.2)]
    window, measured = both(outages, runs, ago(7))
    assert measured["value"] == pytest.approx(1 - 13.2 / 2880)
    assert window["value"] == pytest.approx(1 - 13.2 / 10080)
    # downtime_s, countries and basis are the same on both bases.
    assert {k: v for k, v in measured.items() if k != "value"} == {
        "downtime_s": pytest.approx(13.2 * 60),
        "countries": 1,
        "basis": "outage time",
    }
    assert {k: v for k, v in window.items() if k != "value"} == {
        k: v for k, v in measured.items() if k != "value"
    }


def test_several_countries_sum_their_own_spans_and_clip_downtime_to_them():
    since = ago(7)
    runs = [
        run("US", ago(2), ago(1.5)),
        run("US", ago(1), END),  # US: 2 days measured (first start to last end)
        run("DE", ago(9), ago(6)),  # DE: started before the window, clipped: 1 day
        run("FR", ago(8), ago(7.5)),  # FR: ended before the window, not tested in it
    ]
    outages = [
        down("US", ago(1.2), 60),  # inside the US span
        down("DE", ago(6.5), 30),  # inside the DE span
        down("DE", ago(3), 120),  # after DE's last run: outside its span, not in the value
        down("FR", ago(3), 600),  # FR was not tested in the window
    ]
    window, measured = both(outages, runs, since)
    assert measured["value"] == pytest.approx(1 - (60 + 30) / (3 * 1440))
    assert window["value"] == pytest.approx(1 - (60 + 30 + 120) / (2 * 7 * 1440))
    assert measured["countries"] == window["countries"] == 2
    # downtime_s stays the window's whole downtime of the countries tested.
    assert measured["downtime_s"] == pytest.approx((60 + 30 + 120) * 60)
    assert measured_spans(runs, since, END) == {"US": (ago(2), END), "DE": (since, ago(6))}


def test_a_window_without_measured_time_has_no_uptime_and_says_why():
    empty = {"value": None, "downtime_s": 0.0, "countries": 0, "basis": "outage time"}
    assert both([], [], ago(1)) == (empty, empty)
    # A run that ends exactly at the window start is tested there but measures nothing.
    edge = [run("US", ago(2), ago(1))]
    assert both([], edge, ago(1))[1]["value"] is None
    assert both([], edge, ago(1))[0]["value"] == 1.0
    measured = {"batches": 4, "up": 4, "value": None}
    assert _uptime(SHIPPED, measured, {}) == {
        "value": None,
        "state": "unavailable",
        "reason": UPTIME_UNMEASURED,
        "inputs": None,
    }


def test_a_fully_measured_24h_window_is_unchanged():
    since = ago(1)
    first = (since - timedelta(minutes=5), since + timedelta(minutes=1))
    runs = [run(c, *first) for c in ("US", "DE")]
    runs += [run(c, END - timedelta(minutes=2), END) for c in ("US", "DE")]
    outages = [down("US", ago(0.5), 15), down("DE", since - timedelta(minutes=10), 20)]
    window, measured = both(outages, runs, since)
    assert measured == window
    assert measured["value"] == pytest.approx(1 - (15 + 10) / (2 * 1440))


def test_the_basis_setting_is_validated_and_defaults_to_the_window():
    assert policy.uptime_settings({})["basis"] == "window"
    assert policy.uptime_settings(SHIPPED)["basis"] == "measured_time"
    for bad in ("hours", 1, None):
        with pytest.raises(ValueError, match="Invalid uptime settings"):
            policy.validate(policy.merge(SHIPPED, {"uptime": {"basis": bad}}))
