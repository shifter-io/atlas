"""Calculated v3 over census and pulse batches: breakdowns, uptime, census summary, snapshot v2,
event detection, the events and traffic API and the traffic report."""

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from helpers import FakeTransport
from psycopg.rows import dict_row
from test_calculated import OtherIP, lenient_policy, payload_mismatches
from test_storage import enrichment, measurement
from test_storage_v6 import BERLIN, NETWORK, census

from proxy_index import traffic
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
from proxy_index.cli import main
from proxy_index.detect import WATERMARK, category, detect, list_events, thresholds
from proxy_index.storage import connect, ingest, seed

pytest_plugins = ["test_storage"]

POLICY = Path("configs/methodology.toml")
MUNICH = BERLIN | {"region_code": "BY", "region": "Bavaria", "city": "Munich"}
HOSTING = {
    "isp": "Hetzner",
    "organization": "Hetzner",
    "asn": 24940,
    "asn_organization": "Hetzner Online GmbH",
    "connection_type": "Corporate",
    "usage_type": "hosting",
}


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


def batch(conn, kind, at, *, country="DE", target="generic_http", status="done", minutes=1):
    return conn.execute(
        "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,start_at,"
        "started_at,finished_at,status) SELECT %s,%s,region_id,%s,%s,%s,%s,%s,%s FROM countries "
        "WHERE country_code=%s RETURNING batch_id",
        (kind, country, target, at, at, at, at + timedelta(minutes=minutes), status, country),
    ).fetchone()[0]


def product(conn, provider="alpha", name="residential"):
    conn.execute("INSERT INTO providers VALUES (%s) ON CONFLICT DO NOTHING", (provider,))
    conn.execute(
        "INSERT INTO products(provider,name) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        (provider, name),
    )
    return conn.execute(
        "SELECT product_id FROM products WHERE provider=%s AND name=%s", (provider, name)
    ).fetchone()[0]


def batch_run(conn, batch_id, product_id, finished, *, run_key=None, **counters):
    values = {
        "requests": 100,
        "successes": 100,
        "unique_ips": 0,
        "challenges": 0,
        "blocks": 0,
        "bytes_up": 0,
        "bytes_down": 0,
        "stop_reason": None,
    } | counters
    conn.execute(
        "INSERT INTO batch_runs(batch_id,product_id,status,started_at,finished_at,run_key,"
        + ",".join(values)
        + ") VALUES (%s,%s,'done',%s,%s,%s,"
        + ",".join(["%s"] * len(values))
        + ")",
        (
            batch_id,
            product_id,
            finished - timedelta(minutes=1),
            finished,
            run_key,
            *values.values(),
        ),
    )


def run_key(database, directory):
    return ingest(directory, database)["run_key"]


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


def test_event_categories():
    assert [category(k) for k in ("census_started", "census_finished", "census_skipped")] == [
        "census"
    ] * 3
    assert category("pulse_finished") == "pulse"
    assert {category(k) for k in ("down", "recovered", "node_offline", "no_node")} == {"incident"}
    assert category("new_hosting_asn") == category("ip_overlap") == "audit"


# --- snapshot v2 -------------------------------------------------------------------------------


async def scenario(database, tmp_path):
    """alpha: a census (Berlin) and a pulse run (Munich, hosting), pulse batches for uptime and
    an ad-hoc run; beta: one census. Returns the cutoff and ids."""
    store.initialize(database)
    seed(database)
    census_dir, census_summary = await census(tmp_path, provider="alpha")
    enrichment(census_dir, attributes=BERLIN | NETWORK)
    pulse_dir, _ = await census(tmp_path, provider="alpha", location=MUNICH, network=HOSTING)
    beta_dir, _ = await census(tmp_path, provider="beta")
    started = datetime.fromisoformat(census_summary["started_at"])
    slot = started - timedelta(minutes=1)
    with connect(database) as conn:
        census_batch = batch(conn, "census", slot)
        pulse_batch = batch(conn, "pulse", slot)
    keys = {
        "census": ingest(census_dir, database, batch_id=census_batch)["run_key"],
        "pulse": ingest(pulse_dir, database, batch_id=pulse_batch)["run_key"],
        "beta": ingest(beta_dir, database, batch_id=census_batch)["run_key"],
    }
    adhoc = await measurement(tmp_path, provider="alpha")
    ingest(adhoc, database)
    finished = started + timedelta(minutes=2)
    with connect(database) as conn:
        alpha, beta = product(conn, "alpha"), product(conn, "beta")
        batch_run(
            conn,
            census_batch,
            alpha,
            finished,
            run_key=keys["census"],
            requests=4,
            successes=3,
            unique_ips=1,
            bytes_up=999,
            bytes_down=999,
            stop_reason="unique_ratio",
        )
        batch_run(conn, census_batch, beta, finished, run_key=keys["beta"], requests=4)
        batch_run(
            conn, pulse_batch, alpha, finished, run_key=keys["pulse"], requests=4, successes=3
        )
        # Three more pulses without artifacts: two https (one down), one http down.
        for minutes, target, successes in (
            (-30, "generic_https", 50),
            (-60, "generic_https", 0),
            (-90, "generic_http", 0),
        ):
            moment = slot + timedelta(minutes=minutes)
            batch_run(
                conn,
                batch(conn, "pulse", moment, target=target),
                alpha,
                moment + timedelta(minutes=1),
                successes=successes,
                bytes_up=1000,
                bytes_down=4000,
            )
    as_of = finished + timedelta(minutes=10)
    return as_of, {"alpha": alpha, "beta": beta, "census_batch": census_batch}


def outage(conn, product_id, start, end=None, *, country="DE", source="census"):
    conn.execute(
        "INSERT INTO outages(product_id,country_code,region_id,source,started_at,ended_at) "
        "SELECT %s,%s,region_id,%s,%s,%s FROM countries WHERE country_code=%s",
        (product_id, country, source, start, end, country),
    )


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
    assert payload["schema_version"] == 3
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


# --- detection ---------------------------------------------------------------------------------

T0 = datetime(2026, 10, 1, tzinfo=UTC)
SETTINGS = thresholds() | {"initial_lookback_minutes": 60 * 24 * 30}


def pulses(conn, product_id, results, *, start=0, run_keys=None, **options):
    """One finished pulse batch per entry (successes or a dict of counters), 30 minutes apart."""
    for i, result in enumerate(results, start):
        at = T0 + timedelta(minutes=30 * i)
        counters = result if isinstance(result, dict) else {"successes": result}
        b = batch(conn, "pulse", at, **options)
        batch_run(
            conn,
            b,
            product_id,
            at + timedelta(minutes=1),
            run_key=(run_keys or {}).get(i),
            **counters,
        )


def after(i):
    """A detection time that covers pulse i (settled)."""
    return T0 + timedelta(minutes=30 * i + 10)


def kinds(conn, since_id=0):
    return [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT kind,signal FROM test_events WHERE event_id>%s ORDER BY event_id", (since_id,)
        )
    ]


def test_detect_drop_down_recovered_block_spike_and_pulse_changes(database):
    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        pulses(conn, alpha, [95] * 6)
        # No events while the baseline forms and nothing changes.
        assert detect(conn, after(5), config=SETTINGS) == 0
        assert conn.execute("SELECT value FROM settings WHERE key=%s", (WATERMARK,)).fetchone()
        pulses(conn, alpha, [70], start=6)
        assert detect(conn, after(6), config=SETTINGS) == 1
        drop = (
            conn.cursor(row_factory=dict_row)
            .execute("SELECT * FROM test_events ORDER BY event_id DESC LIMIT 1")
            .fetchone()
        )
        assert (drop["kind"], drop["signal"], drop["severity"]) == ("success_drop", -1, "minor")
        assert drop["data"]["baseline"] == 0.95 and drop["data"]["value"] == 0.7
        assert drop["data"]["region"] == "fra1" and drop["country_code"] == "DE"
        assert drop["target_id"] == "generic_http" and drop["region_id"] is not None
        pulses(conn, alpha, [0, 0, 96, 95], start=7)
        assert detect(conn, after(10), config=SETTINGS) == 2
        assert kinds(conn, drop["event_id"]) == [("down", -1), ("recovered", 1)]
        recovered = conn.execute("SELECT data FROM test_events WHERE kind='recovered'").fetchone()[
            0
        ]
        # Healthy again against the baseline the incident was judged by (which held the 70).
        assert recovered["incident"] == "down"
        assert recovered["baseline"] == pytest.approx((6 * 95 + 70) / 700)
        last = conn.execute("SELECT max(event_id) FROM test_events").fetchone()[0]
        pulses(
            conn,
            alpha,
            [{"successes": 75, "challenges": 15, "blocks": 5}] * 2 + [85],
            start=11,
        )
        assert detect(conn, after(13), config=SETTINGS) == 2
        # The spike is reported once while it lasts; the next clean pulse is a change.
        assert kinds(conn, last) == [("block_spike", -1), ("pulse_finished", 1)]
        spike, change = conn.execute(
            "SELECT data FROM test_events WHERE event_id>%s ORDER BY event_id", (last,)
        ).fetchall()
        assert (spike[0]["challenges"], spike[0]["blocks"], spike[0]["value"]) == (15, 5, 0.2)
        assert change[0]["previous"]["challenges"] == 15 and change[0]["success_rate"] == 0.85
        total = conn.execute("SELECT count(*) FROM test_events").fetchone()[0]
        # Idempotent: the same moment, an earlier one, and a reset watermark add nothing.
        assert detect(conn, after(13), config=SETTINGS) == 0
        assert detect(conn, after(3), config=SETTINGS) == 0
        conn.execute("DELETE FROM settings WHERE key=%s", (WATERMARK,))
        assert detect(conn, after(14), config=SETTINGS) == 0
        assert conn.execute("SELECT count(*) FROM test_events").fetchone()[0] == total


def outages(conn):
    return conn.execute(
        "SELECT source,started_at,ended_at FROM outages ORDER BY outage_id"
    ).fetchall()


def test_pulse_down_opens_an_outage_and_recovered_closes_it(database):
    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        # Both targets go down at pulse 6; http recovers at 8, https only at 9.
        pulses(conn, alpha, [95] * 6 + [0, 0, 96, 95])
        pulses(conn, alpha, [95] * 6 + [0, 0, 0, 96], target="generic_https")
        detect(conn, after(9), config=SETTINGS)
        events = conn.execute(
            "SELECT e.kind,e.target_id,e.data FROM test_events e "
            "WHERE e.kind IN ('down','recovered') ORDER BY e.event_id"
        ).fetchall()
        assert [(e[0], e[1]) for e in events] == [
            ("down", "generic_http"),
            ("down", "generic_https"),
            ("recovered", "generic_http"),
            ("recovered", "generic_https"),
        ]
        # One outage for the product and country: from the last healthy pulse (5, finished a
        # minute after it started) to the start of the pulse after which no target was down.
        last_healthy = T0 + timedelta(minutes=30 * 5 + 1)
        assert outages(conn) == [("pulse", last_healthy, T0 + timedelta(minutes=30 * 9))]
        assert events[0][2]["since"] == last_healthy.isoformat(timespec="microseconds")
        assert events[0][2]["source"] == "pulse"
        # Judging the same batches again opens and closes nothing.
        total = conn.execute("SELECT count(*) FROM test_events").fetchone()[0]
        conn.execute("DELETE FROM settings WHERE key=%s", (WATERMARK,))
        assert detect(conn, after(10), config=SETTINGS) == 0
        assert conn.execute("SELECT count(*) FROM test_events").fetchone()[0] == total
        assert len(outages(conn)) == 1


def test_healthy_pulse_closes_a_census_outage_once(database):
    from proxy_index.storage import partition

    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        # A census on the pulse's own target stalled at T0+100 min: an open census outage and
        # its 'down' event, which the pulse incident state must not take for its own.
        census_batch = batch(conn, "census", T0 + timedelta(minutes=60))
        partition(conn, "test_events", T0)
        conn.execute(
            "INSERT INTO test_events(at,kind,batch_id,product_id,country_code,target_id,signal,"
            "data) VALUES (%s,'down',%s,%s,'DE','generic_http',-1,'{\"source\":\"census\"}')",
            (T0 + timedelta(minutes=115), census_batch, alpha),
        )
        conn.execute(
            "INSERT INTO outages(product_id,country_code,source,batch_id,started_at) "
            "VALUES (%s,'DE','census',%s,%s)",
            (alpha, census_batch, T0 + timedelta(minutes=100)),
        )
        last = conn.execute("SELECT max(event_id) FROM test_events").fetchone()[0]
        # Pulse 3 (T0+90) started before the outage; pulse 4 (T0+120) is the first after it.
        pulses(conn, alpha, [95] * 8)
        detect(conn, after(7), config=SETTINGS)
        assert outages(conn) == [
            ("census", T0 + timedelta(minutes=100), T0 + timedelta(minutes=120))
        ]
        assert kinds(conn, last) == [("recovered", 1)]
        data = conn.execute("SELECT data FROM test_events WHERE event_id>%s", (last,)).fetchone()[0]
        assert data["source"] == "census" and data["incident"] == "down"
        assert data["duration_s"] == 20 * 60
        conn.execute("DELETE FROM settings WHERE key=%s", (WATERMARK,))
        assert detect(conn, after(8), config=SETTINGS) == 0


def test_detect_waits_for_settled_batches_and_filters_small_pulses(database):
    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        pulses(conn, alpha, [95] * 6 + [{"requests": 10, "successes": 0}])
        settle = timedelta(seconds=SETTINGS["settle_seconds"])
        finished = T0 + timedelta(minutes=30 * 6 + 1)
        # Finished less than settle_seconds ago: not yet.
        assert detect(conn, finished + settle - timedelta(seconds=1), config=SETTINGS) == 0
        # Ten requests are below min_requests: not judged as down, only reported as a change.
        assert detect(conn, finished + settle, config=SETTINGS) == 1
        assert kinds(conn) == [("pulse_finished", -1)]


async def test_detect_latency_spike_from_run_histograms(database, tmp_path):
    seed(database)
    keys = {}
    for i in range(7):
        transport = OtherIP if i == 6 else FakeTransport
        keys[i] = run_key(database, await measurement(tmp_path, transport_factory=transport))
    with connect(database) as conn:
        alpha = product(conn, "fixture")
        pulses(conn, alpha, [100] * 7, run_keys=keys)
        assert detect(conn, after(6), config=SETTINGS | {"min_latency_samples": 3}) == 1
        event = conn.execute("SELECT kind,data FROM test_events").fetchone()
    assert event[0] == "latency_spike"
    assert (event[1]["p50"], event[1]["baseline"]) == (31, 11)


async def test_detect_new_hosting_asn_and_census_overlap(database, tmp_path):
    seed(database)
    a, _ = await census(tmp_path, provider="alpha", network=HOSTING)
    b, _ = await census(tmp_path, provider="beta")
    again, _ = await census(tmp_path, provider="alpha", network=HOSTING)
    with connect(database) as conn:
        first, second = batch(conn, "census", T0), batch(conn, "pulse", T0 + timedelta(hours=1))
    keys = [
        ingest(a, database, batch_id=first)["run_key"],
        ingest(b, database, batch_id=first)["run_key"],
        ingest(again, database, batch_id=second)["run_key"],
    ]
    with connect(database) as conn:
        alpha, beta = product(conn, "alpha"), product(conn, "beta")
        batch_run(conn, first, alpha, T0 + timedelta(minutes=1), run_key=keys[0])
        batch_run(conn, first, beta, T0 + timedelta(minutes=1), run_key=keys[1])
        batch_run(conn, second, alpha, T0 + timedelta(hours=1, minutes=1), run_key=keys[2])
        assert detect(conn, T0 + timedelta(hours=2), config=SETTINGS) == 3
        events = (
            conn.cursor(row_factory=dict_row)
            .execute(
                "SELECT e.kind,p.provider,e.batch_id,e.signal,e.data FROM test_events e "
                "JOIN products p USING(product_id) ORDER BY e.event_id"
            )
            .fetchall()
        )
    overlap = {e["provider"]: e for e in events if e["kind"] == "ip_overlap"}
    assert set(overlap) == {"alpha", "beta"}
    assert overlap["alpha"]["data"] | {"region": None} == {
        "region": None,
        "value": 1.0,
        "shared_ips": 1,
        "unique_ips": 1,
        "peers": {"beta": 1},
    }
    hosting = [e for e in events if e["kind"] == "new_hosting_asn"]
    # Only once per product and ASN, although the pulse landed in it again.
    assert len(hosting) == 1 and hosting[0]["batch_id"] == first
    assert (
        hosting[0]["data"]["asn"] == 24940 and hosting[0]["data"]["name"] == "Hetzner Online GmbH"
    )


# --- events and traffic API, traffic report ----------------------------------------------------


def events_fixture(database):
    seed(database)
    with connect(database) as conn:
        alpha, beta = product(conn, "alpha"), product(conn, "beta", "datacenter")
        pulses(conn, alpha, [95] * 6 + [0])
        pulses(conn, beta, [95] * 6 + [60], target="generic_https")
        detect(conn, after(6), config=SETTINGS)
        conn.execute(
            "INSERT INTO test_events(at,kind,country_code,region_id,signal,data) SELECT %s,"
            "'census_started','DE',region_id,0,'{\"requests\":0}' FROM countries "
            "WHERE country_code='DE'",
            (after(7),),
        )
        # As the orchestrator writes them: per product, with the batch's country and target.
        conn.execute(
            "INSERT INTO test_events(at,kind,product_id,country_code,target_id,region_id,"
            "signal,data) SELECT %s,'census_finished',%s,'DE','generic_http',region_id,1,"
            "'{\"requests\":10,\"unique_ips\":4}' FROM countries WHERE country_code='DE'",
            (after(8), alpha),
        )
        return [r[0] for r in conn.execute("SELECT at FROM test_events ORDER BY at,event_id")]


async def test_events_api_filters_and_shape(database):
    times = events_fixture(database)
    async with TestClient(TestServer(app(database))) as client:

        async def get(query=""):
            response = await client.get("/v1/events" + query)
            assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
            return (await response.json())["events"]

        everything = await get()
        assert [e["kind"] for e in everything] == [
            "census_finished",
            "census_started",
            "success_drop",
            "down",
        ]
        down = everything[3]
        assert set(down) == {
            "id",
            "at",
            "kind",
            "category",
            "product",
            "provider",
            "country",
            "region",
            "target",
            "signal",
            "severity",
            "data",
        }
        assert (down["product"], down["provider"], down["category"]) == (
            "alpha/residential",
            "alpha",
            "incident",
        )
        assert (down["country"], down["region"], down["target"]) == ("DE", "fra1", "generic_http")
        assert everything[1]["product"] == "" and everything[1]["category"] == "census"
        census = everything[0]
        assert (census["product"], census["provider"], census["category"]) == (
            "alpha/residential",
            "alpha",
            "census",
        )
        assert (census["country"], census["region"], census["data"]["unique_ips"]) == (
            "DE",
            "fra1",
            4,
        )
        assert [e["kind"] for e in await get("?product=alpha")] == ["census_finished", "down"]
        assert await get("?product=alpha/residential&category=census") == [census]
        assert [e["kind"] for e in await get("?product=beta/datacenter")] == ["success_drop"]
        assert await get("?product=beta/residential") == []
        assert [e["kind"] for e in await get("?kind=down,census_started")] == [
            "census_started",
            "down",
        ]
        assert [e["kind"] for e in await get("?category=census")] == [
            "census_finished",
            "census_started",
        ]
        assert len(await get("?category=incident&limit=1")) == 1
        # since is exclusive and empty means none (the site's first poll).
        newest = everything[0]["at"]
        assert await get("?since=" + newest.replace("+", "%2B")) == []
        assert len(await get("?since=")) == 4
        assert len(await get("?since=" + times[0].isoformat().replace("+", "%2B"))) == 2
        for bad in ("?category=other", "?limit=0", "?limit=501", "?since=yesterday", "?x=1"):
            assert (await client.get("/v1/events" + bad)).status == 400
    with connect(database) as conn:
        assert list_events(conn, before=times[-1]) == everything[1:]


async def test_traffic_api_cli_and_sums(database, tmp_path, capsys):
    as_of, _ = await scenario(database, tmp_path)
    with connect(database) as conn:
        report = traffic.summary(conn)
        pulse_only = traffic.summary(conn, test="pulse", provider="alpha/residential")
        none = traffic.summary(conn, country="FR")
        windowed = traffic.summary(conn, end=as_of - timedelta(minutes=60))
    assert report["total"]["requests"] == 312 and report["total"]["bytes_total"] == 18600
    assert {r["provider"]: r["requests"] for r in report["by_provider"]} == {
        "alpha": 308,
        "beta": 4,
    }
    alpha_census = next(
        r for r in report["rows"] if (r["test"], r["provider"]) == ("census", "alpha")
    )
    assert alpha_census["artifact_runs"] == 1 and alpha_census["bytes_up"] == 300
    assert alpha_census["gb_total"] == 1200 / 10**9
    assert alpha_census["avg_bytes_per_request"] == 300
    assert pulse_only["total"]["runs"] == 4 and pulse_only["by_test"]["census"]["runs"] == 0
    assert none["total"] == traffic.totals([]) and none["total"]["avg_bytes_per_request"] is None
    assert windowed["total"]["runs"] == 2  # only the two pulses scheduled an hour earlier
    async with TestClient(TestServer(app(database))) as client:
        response = await client.get("/v1/traffic?test=census&provider=beta")
        body = await response.json()
        assert body["total"]["requests"] == 4 and body["filters"]["provider"] == "beta"
        assert (await client.get("/v1/traffic?test=other")).status == 400
        assert (await client.get("/v1/traffic?from=never")).status == 400
    args = ["report", "traffic", "--database-file", str(database), "--test", "census"]
    assert main(args) == 0
    table = capsys.readouterr().out.splitlines()
    assert table[0].split() == [
        "TEST",
        "PROVIDER",
        "PRODUCT",
        "RUNS",
        "REQUESTS",
        "GB",
        "UP",
        "GB",
        "DOWN",
        "GB",
        "TOTAL",
        "AVG",
        "B/REQ",
    ]
    assert table[1].split()[:5] == ["census", "alpha", "residential", "1", "4"]
    assert table[-1].split()[:5] == ["all", "all", "2", "8", "0.000"]
    assert main([*args, "--json", "--region", "fra1", "--from", "2000-01-01"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["total"]["bytes_up"] == 600 and printed["period"]["from"].startswith("2000")
    assert Counter(r["test"] for r in printed["rows"]) == {"census": 2}


def coverage_policy(tmp_path):
    """The shipped methodology (minimum_coverage 0.90) with a one-request minimum."""
    policy = tmp_path / "methodology.toml"
    policy.write_text(POLICY.read_text().replace("minimum_requests = 4000", "minimum_requests = 1"))
    return policy


async def test_scheduled_coverage_is_the_share_of_pulse_slots(database, tmp_path):
    """Operator skips/kills and failed runs (a run is only ever failed by our side) are not
    slots and never count against the provider. A provider that is down (a done run whose
    requests all failed) fills its slot: it lowers success and uptime, never coverage. 40 of 48
    slots filled (8 done without a request) is below minimum_coverage 0.90."""
    store.initialize(database)
    seed(database)
    directory, summary = await census(tmp_path, provider="alpha")
    as_of = datetime.fromisoformat(summary["finished_at"]) + timedelta(minutes=10)
    slot = [as_of - timedelta(minutes=30 * (i + 1)) for i in range(48)]
    with connect(database) as conn:
        first = batch(conn, "pulse", slot[0])
    linked = ingest(directory, database, batch_id=first)["run_key"]
    with connect(database) as conn:
        alpha = product(conn)
        batch_run(conn, first, alpha, slot[0] + timedelta(minutes=1), run_key=linked, requests=4)
        for moment in slot[1:47]:
            batch_run(conn, batch(conn, "pulse", moment), alpha, moment + timedelta(minutes=1))
        # The provider is down: every request of the slot's run failed.
        down = batch(conn, "pulse", slot[47])
        batch_run(conn, down, alpha, slot[47] + timedelta(minutes=1), requests=100, successes=0)
        # Not slots: runs our side failed (whatever they counted) and an operator-killed batch.
        for minute, error in (
            (3, "node error (OSError)"),
            (4, "node_shutdown"),
            (5, "no_node"),
            (6, "not_started"),
            (7, "stale"),
        ):
            b = batch(conn, "pulse", slot[10] + timedelta(minutes=minute))
            batch_run(conn, b, alpha, slot[10] + timedelta(minutes=minute + 1))
            conn.execute(
                "UPDATE batch_runs SET status='failed',error=%s WHERE batch_id=%s", (error, b)
            )
        killed = batch(conn, "pulse", slot[10] + timedelta(minutes=8), status="killed")
        batch_run(conn, killed, alpha, slot[10] + timedelta(minutes=9), requests=0)
    policy = coverage_policy(tmp_path)

    def window(as_of):
        payload = calculate(database, policy, as_of=as_of)
        scheduled = next(
            p for p in payload["products"] if p["criteria"].get("schedule") == "census_pulse"
        )
        return scheduled["windows"]["24h"]

    w = window(as_of)
    target = w["targets"]["generic_http"]
    assert target["metrics"]["coverage_slots"] == {"due": 48, "filled": 48, "down": 1}
    assert target["metrics"]["coverage"] == 1
    assert w["metrics"]["coverage_basis"] == "pulse slots"
    assert w["metrics"]["coverage"] == 1
    assert target["metrics"]["uptime"] == pytest.approx(47 / 48)
    # Every slot weighs the same: the ingested run (3 of 4), 46 healthy pulses and the down
    # slot, which votes 0. Failed runs add nothing, even the requests they counted.
    assert target["metrics"]["success_rate"] == pytest.approx((0.75 + 46) / 48)
    assert target["metrics"]["time_slots"]["down"] == 1
    assert target["metrics"]["time_slots"]["success"] == 48
    assert "Insufficient time coverage" not in target["score"]["reasons"]
    # Seven more slots with the provider down: still covered, but down.
    with connect(database) as conn:
        conn.execute(
            "UPDATE batch_runs SET successes=0 WHERE batch_id IN "
            "(SELECT batch_id FROM test_batches WHERE scheduled_for=ANY(%s))",
            (slot[1:8],),
        )
    target = window(as_of)["targets"]["generic_http"]  # new inputs, new build
    assert target["metrics"]["coverage_slots"] == {"due": 48, "filled": 48, "down": 8}
    assert "Insufficient time coverage" not in target["score"]["reasons"]
    assert target["metrics"]["uptime"] == pytest.approx(40 / 48)
    # Eight slots done without a single request are gaps.
    with connect(database) as conn:
        conn.execute(
            "UPDATE batch_runs SET status='done',error=NULL,requests=0 WHERE batch_id IN "
            "(SELECT batch_id FROM test_batches WHERE scheduled_for=ANY(%s))",
            (slot[1:9],),
        )
    target = window(as_of)["targets"]["generic_http"]
    assert target["metrics"]["coverage_slots"] == {"due": 48, "filled": 40, "down": 1}
    assert "Insufficient time coverage" in target["score"]["reasons"]
    assert target["score"]["state"] == "incomplete"
