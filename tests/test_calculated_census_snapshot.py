"""Calculated v3 snapshot v2 over census and pulse batches: breakdowns, uptime, census summary
and traffic, and the uptime score's batch minimum with its evidence fallback."""

from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer
from calculated_cases import lenient_policy, payload_mismatches
from census_pulse_cases import outage, scenario

from proxy_index.calculated import store
from proxy_index.calculated.api import app
from proxy_index.calculated.calculator import calculate
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]


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
    # The "window" basis: this test checks the outage arithmetic against the window length (the
    # measured-time basis is test_outage_scenario.py's and test_uptime_measured.py's).
    policy.write_text(
        policy.read_text()
        .replace("min_batches = 10", "min_batches = 1")
        .replace('basis = "measured_time"', 'basis = "window"')
    )
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
