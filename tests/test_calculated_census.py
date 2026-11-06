"""Calculated v3 aggregation over census and pulse data: availability history, breakdowns and
uptime. The snapshot v2 built from them is in test_calculated_census_snapshot.py."""

from datetime import UTC, datetime, timedelta

import pytest
from census_pulse_cases import HOSTING, MUNICH
from test_storage_v6 import BERLIN, NETWORK

from proxy_index.calculated.aggregate import (
    SeriesSpec,
    breakdowns,
    downtime,
    outage_list,
    outage_uptime,
    pool_lists,
    series,
    uptime,
)


def test_availability_history_clips_and_unions_outages_without_inventing_healthy_days():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    day = timedelta(days=1)

    def h(hours):
        return start + timedelta(hours=hours)

    tested = [{"country": c, "started_at": h(25), "last": h(26)} for c in ("DE", "US")]
    outages = [
        {"country": "DE", "started_at": h(34), "ended_at": h(36)},
        {"country": "DE", "started_at": h(35), "ended_at": h(37)},
        {"country": "DE", "started_at": h(48), "ended_at": None},
    ]
    points = series([], [], SeriesSpec(start, start + 3 * day, 72, tested=tested, outages=outages))
    assert [p["at"] for p in points] == [start + i * day for i in range(3)]
    assert [p["uptime"] for p in points] == [None, 1 - 3 / 48, 0]
    # Availability remains measurable even when request-history buckets are missing.
    assert all(p["success_rate"] is None for p in points)


def test_availability_history_uses_half_open_five_minute_buckets():
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def m(minutes):
        return start + timedelta(minutes=minutes)

    spec = SeriesSpec(
        start,
        m(15),
        24,
        tested=[{"country": "DE", "started_at": start, "last": m(5)}],
        outages=[{"country": "DE", "started_at": m(5), "ended_at": m(10)}],
    )
    points = series([], [], spec)
    assert [p["uptime"] for p in points] == [1, 0, None]


# --- pure aggregation -------------------------------------------------------------------------


def geo_row(location, network, completed, successes, bins, *, fallback=None, **outcomes):
    return {
        "location": None if location is None else tuple(location.get(f) for f in PLACE),
        "network": None
        if network is None
        else (network.get("asn"), network.get("asn_organization"), network.get("usage_type")),
        "fallback": fallback,
        "completed": completed,
        "successes": successes,
        "challenges": outcomes.get("challenges", 0),
        "blocks": outcomes.get("blocks", 0),
        "histogram": bins,
    }


PLACE = ("country_code", "country", "region_code", "region", "city")


def test_breakdowns_per_dimension_from_geo_buckets():
    unknown = {k: None for k in PLACE}
    rows = [
        geo_row(BERLIN, NETWORK, 10, 8, [[100, 4], [300, 4]], challenges=2),
        geo_row(MUNICH, NETWORK, 4, 4, [[50, 4]]),
        geo_row(MUNICH, HOSTING, 6, 5, [[1000, 5]], blocks=1),
        # No exit address: counts towards the requested country only.
        geo_row(unknown, {"asn": None}, 5, 0, [], fallback=("DE", "Deutschland"), blocks=5),
        geo_row(unknown, {"asn": None}, 2, 0, [], fallback=("FR", "France")),
    ]
    ips = {
        1: {
            "count": 3,
            "row": BERLIN
            | {
                "asn": 3320,
                "asn_organization": NETWORK["asn_organization"],
                "usage_type": "residential",
            },
        },
    }
    result = breakdowns(ips, rows)
    assert list(result) == ["country", "region", "city", "asn", "country_asn", "city_asn"]
    de, fr = result["country"]
    # The DB-IP name wins over the requested-country fallback name.
    assert (de["country_code"], de["country"], fr["country"]) == ("DE", "Germany", "France")
    assert (de["completed"], de["successes"], de["challenges"], de["blocks"]) == (25, 17, 2, 6)
    assert de["success_rate"] == 17 / 25 and fr["success_rate"] == 0 and fr["p50"] is None
    assert (de["ips"], de["observations"]) == (1, 3)
    # Merged histograms, never averaged medians: 17 samples, 9th is 300 ms.
    assert (de["p50"], de["p90"], de["p99"]) == (300, 1000, 1000)
    regions = {r["region"]: r for r in result["region"]}
    assert regions["Bavaria"]["completed"] == 10 and regions["Bavaria"]["successes"] == 9
    assert regions["Berlin"]["p50"] == 100 and set(regions["Berlin"]) == {
        "country_code",
        "country",
        "region",
        "region_code",
        "ips",
        "observations",
        "completed",
        "successes",
        "challenges",
        "blocks",
        "success_rate",
        "p50",
        "p90",
        "p99",
    }
    # Ordered by requests, then addresses: ten requests each, only Berlin has an address.
    assert [r["city"] for r in result["city"]] == ["Berlin", "Munich"]
    asns = {r["asn"]: r for r in result["asn"]}
    assert asns[24940]["usage_type"] == "hosting" and asns[3320]["completed"] == 14
    assert {(r["country_code"], r["asn"]) for r in result["country_asn"]} == {
        ("DE", 3320),
        ("DE", 24940),
    }
    pairs = {(r["city"], r["asn"]): r for r in result["city_asn"]}
    assert set(pairs) == {("Berlin", 3320), ("Munich", 3320), ("Munich", 24940)}
    assert (pairs["Munich", 3320]["p50"], pairs["Munich", 24940]["p50"]) == (50, 1000)
    assert pairs["Berlin", 3320]["ips"] == 1
    # The v1 pool lists keep only rows with addresses.
    lists = pool_lists(result)
    assert lists["countries_breakdown"] == [
        {"country_code": "DE", "country": "Germany", "ips": 1, "observations": 3}
    ]
    assert [g["city"] for g in lists["geo"]] == ["Berlin"]
    assert lists["asns_breakdown"][0]["asn"] == 3320


def test_city_network_evidence_retains_unclassified_exits_and_does_not_copy_city_latency():
    ips = {
        1: {
            "count": 3,
            "row": BERLIN
            | {
                "asn": 3320,
                "asn_organization": "Telekom",
                "usage_type": "residential",
            },
        },
        2: {
            "count": 2,
            "row": BERLIN
            | {
                "asn": None,
                "asn_organization": None,
                "usage_type": None,
            },
        },
    }
    rows = [geo_row(BERLIN, NETWORK, 3, 3, [[50, 3]])]
    result = breakdowns(ips, rows)["city_asn"]
    unknown = next(r for r in result if r["asn"] is None)
    assert (unknown["ips"], unknown["observations"]) == (1, 2)
    assert unknown["name"] is None and unknown["usage_type"] is None
    assert unknown["p50"] is None
    assert sum(r["ips"] for r in result) == 2


def test_uptime_share_of_pulse_batches():
    t = datetime(2026, 10, 1, tzinfo=UTC)
    batches = [
        {"scheduled_for": t + timedelta(minutes=30 * i), "successes": n, "status": "done"}
        for i, n in enumerate([5, 0, 1, 2])
    ]
    assert uptime(batches, t, t + timedelta(hours=2), 1) == {"batches": 4, "up": 3, "value": 0.75}
    assert uptime(batches, t, t + timedelta(hours=2), 2)["value"] == 0.5
    assert uptime(batches, t + timedelta(hours=1), t + timedelta(hours=2), 1)["batches"] == 2
    assert uptime([], t, t, 1) == {"batches": 0, "up": 0, "value": None}


def test_uptime_is_outage_time_over_the_countries_tested():
    end = datetime(2026, 10, 2, tzinfo=UTC)
    since = end - timedelta(hours=24)

    def h(hours):
        return end - timedelta(hours=hours)

    def down(country, start, stop=None, outage_id=0):
        return {
            "outage_id": outage_id,
            "country": country,
            "region": "fra1",
            "source": "census",
            "started_at": start,
            "ended_at": stop,
        }

    rows = [
        down("DE", h(10), h(8), 1),
        down("DE", h(9), h(7), 2),  # overlaps the first: 3 h in all, not 4
        down("DE", h(30), h(23), 3),  # clipped at the window start: 1 h
        down("DE", h(1), None, 4),  # still open: clipped at the cutoff, 1 h
        down("DE", h(40), h(24), 5),  # ends at the window start: nothing
        down("FR", h(2), h(1), 6),  # FR was not tested in the window
    ]
    tested = {"DE": h(0.5), "US": end, "GB": h(25), "FR": h(30)}
    assert downtime(rows, ["DE"], since, end) == 5 * 3600
    assert outage_uptime(rows, tested, since, end) == {
        "value": pytest.approx(1 - 5 / 48),
        "downtime_s": 5 * 3600.0,
        "countries": 2,
        "basis": "outage time",
    }
    assert outage_uptime(rows, {}, since, end)["value"] is None
    assert outage_uptime([], {"US": end}, since, end)["value"] == 1.0
    # The newest first; ongoing at the cutoff has no end; durations are not clipped.
    listed = outage_list(rows, since, end)
    assert [o["started_at"] for o in listed] == [h(1), h(2), h(9), h(10), h(30)]
    assert listed[0]["ended_at"] is None and listed[0]["duration_s"] == 3600
    assert listed[-1]["duration_s"] == 7 * 3600
    # An outage that ended after the cutoff was still open as of it.
    assert (
        outage_list([down("DE", h(2), end + timedelta(hours=1))], since, end)[0]["ended_at"] is None
    )
    assert len(outage_list([down("DE", h(1 + i / 10), h(1)) for i in range(60)], since, end)) == 50
