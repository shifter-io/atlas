"""Calculated v3 over census and pulse batches: availability history, breakdowns, uptime,
census summary and snapshot v2."""

from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer
from census_pulse_cases import HOSTING, MUNICH, outage, scenario
from test_calculated import lenient_policy, payload_mismatches
from test_storage_v6 import BERLIN, NETWORK

from proxy_index.calculated import store
from proxy_index.calculated.aggregate import (
    breakdowns,
    downtime,
    outage_list,
    outage_uptime,
    pool_lists,
    series,
    uptime,
)
from proxy_index.calculated.api import app
from proxy_index.calculated.calculator import calculate
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]


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
    points = series([], [], start, start + 3 * day, 72, tested=tested, outages=outages)
    assert [p["at"] for p in points] == [start + i * day for i in range(3)]
    assert [p["uptime"] for p in points] == [None, 1 - 3 / 48, 0]
    # Availability remains measurable even when request-history buckets are missing.
    assert all(p["success_rate"] is None for p in points)


def test_availability_history_uses_half_open_five_minute_buckets():
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def m(minutes):
        return start + timedelta(minutes=minutes)

    points = series(
        [],
        [],
        start,
        m(15),
        24,
        tested=[{"country": "DE", "started_at": start, "last": m(5)}],
        outages=[{"country": "DE", "started_at": m(5), "ended_at": m(10)}],
    )
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


# --- snapshot v2 -------------------------------------------------------------------------------


async def test_snapshot_v2_breakdowns_uptime_census_and_traffic(database, tmp_path):
    as_of, ids = await scenario(database, tmp_path)
    # The calculation's cutoff: closed 5-minute buckets.
    as_of = datetime.fromtimestamp(as_of.timestamp() // 300 * 300, UTC)
    with connect(database) as conn:
        # Overlapping outages in DE: [-6h, -3h] and [-4h, open] are 6 hours down, once.
        outage(conn, ids["alpha"], as_of - timedelta(hours=6), as_of - timedelta(hours=3))
        outage(conn, ids["alpha"], as_of - timedelta(hours=4), source="pulse")
        # Before the 30-day window: neither counted nor listed.
        outage(conn, ids["alpha"], as_of - timedelta(days=40), as_of - timedelta(days=35))
    policy = lenient_policy(tmp_path)
    policy.write_text(policy.read_text().replace("min_batches = 10", "min_batches = 1"))
    payload = calculate(database, policy, as_of=as_of, publish=True)
    assert payload["schema_version"] == 4
    assert payload_mismatches(database, int(payload["snapshot_id"])) == []
    entries = [p for p in payload["products"] if p["provider"] == "alpha"]
    # One scheduled cohort for census and pulse across targets, plus the ad-hoc run's cohort.
    scheduled = next(p for p in entries if p["criteria"].get("schedule") == "census_pulse")
    assert len(entries) == 2 and scheduled["criteria"]["country"] is None
    w = scheduled["windows"]["24h"]
    assert w["roles"]["pool_sampling"] == 4 and w["roles"]["monitor"] == 4
    m = w["metrics"]
    assert (m["completed"], m["successes"]) == (8, 6)
    assert (m["challenge_rate"], m["block_rate"]) == (0, 0)
    assert (m["bytes_up"], m["bytes_down"]) == (600, 1800)
    # Uptime is outage time over the countries tested (DE): 6 of 24 hours down. The pulse-slot
    # counts stay for reference (one up per target: http 1 of 2, https 1 of 2).
    assert w["uptime"] == {
        "batches": 4,
        "up": 2,
        "value": 0.75,
        "downtime_s": 6 * 3600.0,
        "countries": 1,
        "basis": "outage time",
    }
    assert m["uptime"] == 0.75
    assert scheduled["windows"]["7d"]["uptime"]["value"] == pytest.approx(1 - 6 / 168)
    # Daily availability uses the actual monitoring/outage day, with older days N/A.
    assert [point["uptime"] for point in scheduled["windows"]["7d"]["series"]] == [
        None,
        None,
        None,
        None,
        None,
        None,
        0.75,
    ]
    with connect(database) as conn:
        stored = conn.execute(
            "SELECT s.uptime FROM calculated_series s JOIN calculated_windows w USING(result_id) "
            "WHERE w.build_id=%s AND w.product_id=%s AND w.cohort=%s AND w.target='' "
            "AND w.window_hours=168 ORDER BY s.bucket_start",
            (int(payload["snapshot_id"]), scheduled["product_id"], scheduled["cohort"]),
        ).fetchall()
        assert [r[0] for r in stored] == [
            point["uptime"] for point in scheduled["windows"]["7d"]["series"]
        ]
    assert w["targets"]["generic_http"]["metrics"]["uptime"] == 0.5
    uptime_component = w["scores"]["balanced"]["components"]["uptime"]
    # alpha is the only product of its kind with ready uptime: the leader, at 100.
    assert uptime_component["inputs"] == {"value": 75, "best": 75, "peers": 1}
    assert uptime_component["state"] == "ready" and uptime_component["value"] == 100
    assert scheduled["outages"] == [
        {
            "country": "DE",
            "region": "fra1",
            "source": "pulse",
            "started_at": (as_of - timedelta(hours=4)).isoformat(),
            "ended_at": None,
            "duration_s": 4 * 3600.0,
        },
        {
            "country": "DE",
            "region": "fra1",
            "source": "census",
            "started_at": (as_of - timedelta(hours=6)).isoformat(),
            "ended_at": (as_of - timedelta(hours=3)).isoformat(),
            "duration_s": 3 * 3600.0,
        },
    ]
    assert "uptime_pct" not in w["unavailable"] and "captcha_rate" not in w["unavailable"]
    b = w["breakdowns"]
    assert [(r["country_code"], r["completed"], r["successes"]) for r in b["country"]] == [
        ("DE", 8, 6)
    ]
    assert b["country"][0]["ips"] == 1 and b["country"][0]["success_rate"] == 0.75
    assert {r["region"]: r["completed"] for r in b["region"]} == {"Berlin": 3, "Bavaria": 3}
    assert {r["asn"]: r["usage_type"] for r in b["asn"]} == {3320: "residential", 24940: "hosting"}
    # Breakdown latency is light-load latency: the pulse's city has one, the census's none.
    cities = {r["city"]: r for r in b["city"]}
    assert len(b["country_asn"]) == 2 and cities["Munich"]["p50"] == 11
    assert cities["Berlin"]["completed"] == 3 and cities["Berlin"]["p50"] is None
    # Both alpha runs saw the same address (FakeTransport): one IP, six observations.
    assert w["pool"]["countries_breakdown"] == [
        {"country_code": "DE", "country": "Germany", "ips": 1, "observations": 6}
    ]
    assert scheduled["census"] == [
        {
            "country": "DE",
            "region": "fra1",
            "target": "generic_http",
            "finished_at": scheduled["census"][0]["finished_at"],
            "requests": 4,
            "successes": 3,
            "unique_ips": 1,
            "stop_reason": "unique_ratio",
            "cities": 1,
            "asns": 1,
            "duration_s": 60.0,
        }
    ]
    adhoc = next(p for p in entries if p is not scheduled)
    assert (
        adhoc["census"] == scheduled["census"] and adhoc["windows"]["24h"]["uptime"]["batches"] == 0
    )
    # Batch runs are summarized, not listed; the ad-hoc run is.
    assert [r["provider"] for r in payload["runs"]] == ["alpha"]
    # Traffic prefers the ingested buckets (300/900) over live counters (999) of linked runs.
    assert payload["traffic"]["by_test"]["census"] == {
        "runs": 2,
        "requests": 8,
        "bytes_up": 600,
        "bytes_down": 1800,
    }
    assert payload["traffic"]["by_test"]["pulse"] == {
        "runs": 4,
        "requests": 304,
        "bytes_up": 3300,
        "bytes_down": 12900,
    }
    assert payload["events"] == []
    with connect(database) as conn:
        rows = conn.execute(
            "SELECT dimension,count(*),bool_and(completed IS NOT NULL) "
            "FROM calculated_breakdown_details GROUP BY 1 ORDER BY 1"
        ).fetchall()
        windows = conn.execute(
            "SELECT uptime,bytes_up,challenge_rate FROM calculated_windows w "
            "JOIN cohorts USING(cohort) WHERE target='' AND window_hours=24 "
            "AND extra->>'schedule'='census_pulse' AND product_id=%s",
            (ids["alpha"],),
        ).fetchone()
    assert {r[0] for r in rows} == {
        "asn",
        "country",
        "country_asn",
        "geography",
        "region",
        "city_asn",
    }
    assert all(r[2] for r in rows) and windows == (0.75, 600, 0.0)
    # The API projects v2 dimensions next to the v1 pool, and lists products with census.
    async with TestClient(TestServer(app(database))) as client:
        body = await (await client.get("/v1/breakdowns?window=24h")).json()
        assert any("dimensions" in d and d["dimensions"]["country"] for d in body["data"])
        products = await (await client.get("/v1/products?window=24h")).json()
        assert all("census" in p for p in products["data"])


async def test_measured_uptime_needs_enough_batches_and_evidence_falls_back(database, tmp_path):
    as_of, ids = await scenario(database, tmp_path)
    payload = calculate(database, lenient_policy(tmp_path), as_of=as_of)  # min_batches = 10
    scheduled = next(
        p for p in payload["products"] if p["criteria"].get("schedule") == "census_pulse"
    )
    component = scheduled["windows"]["24h"]["scores"]["balanced"]["components"]["uptime"]
    assert component["state"] == "incomplete" and "Fewer" in component["reason"]
    store.evidence(
        database,
        {
            "product_id": ids["alpha"],
            "cohort": scheduled["cohort"],
            "metric": "uptime_pct",
            "value": 99.0,
            "period_start": (as_of - timedelta(days=40)).isoformat(),
            "period_end": (as_of + timedelta(days=1)).isoformat(),
            "valid_until": (as_of + timedelta(days=2)).isoformat(),
            "source": "fixture",
        },
    )
    payload = calculate(database, lenient_policy(tmp_path), as_of=as_of)
    scheduled = next(p for p in payload["products"] if p["cohort"] == scheduled["cohort"])
    component = scheduled["windows"]["24h"]["scores"]["balanced"]["components"]["uptime"]
    assert component["state"] == "ready" and component["value"] == 100
    assert component["inputs"] == {"value": 99.0, "best": 99.0, "peers": 1}
