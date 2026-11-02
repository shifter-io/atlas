"""Tape event kinds of the daily test, the hour estimates and the pulse: payload contract of the
emit helpers in orchestrator/events.py and their live tape category (detect.category and the
SQL the events API filters with)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from test_census_pulse import product

from proxy_index.detect import category, list_events
from proxy_index.orchestrator import events
from proxy_index.orchestrator.db import Database
from proxy_index.regions import REGION_IDS
from proxy_index.storage import connect, partition, seed

pytest_plugins = ["test_storage"]

AT = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)
COLUMNS = (
    "at",
    "kind",
    "batch_id",
    "product_id",
    "country_code",
    "target_id",
    "region_id",
    "signal",
    "severity",
    "data",
)
KINDS = {
    "daily_test_started": "census",
    "daily_test_finished": "census",
    "estimate_hour_released": "census",
    "pulse_down": "pulse",
    "pulse_recovered": "pulse",
    "latency_rise": "incident",
}


class Recorder:
    """An AsyncConnection stand-in that keeps the inserted row."""

    def __init__(self):
        self.rows = []

    async def execute(self, query, params):
        assert query.startswith("INSERT INTO test_events(")
        self.rows.append(dict(zip(COLUMNS, params, strict=True)) | {"data": params[-1].obj})
        return self

    async def fetchone(self):
        return {"event_id": len(self.rows)}


def daily(product_id=7, region_id=3, countries=("DE", "FR")):
    return events.DailyTest(
        product_id=product_id, region_id=region_id, region="fra1", countries=countries
    )


async def emit_all(conn, product_id, region_id, at=AT):
    test = daily(product_id, region_id)
    await events.emit_daily_test_started(conn, at, test)
    await events.emit_daily_test_finished(
        conn,
        at,
        test,
        events.DailyTestResult(status="done", requests=120000, unique_ips=41000, duration_s=1800.5),
    )
    await events.emit_estimate_hour_released(
        conn,
        at,
        events.HourEstimate(
            product_id=product_id, day=date(2026, 11, 2), hour=13, formula_version="f1"
        ),
    )
    await events.emit_pulse_down(conn, at, window(product_id), outage_ids=(11, 12))
    await events.emit_pulse_recovered(
        conn, at, product_id=product_id, down_since=at - timedelta(minutes=25), outage_ids=(11, 12)
    )
    await events.emit_latency_rise(
        conn,
        at,
        events.LatencyRise(product_id=product_id, country_code="DE", p50=1840.0, baseline=400.0),
    )


def window(product_id=7, **fields):
    return events.PulseWindow(
        **{
            "product_id": product_id,
            "window_start": AT - timedelta(minutes=5),
            "window_minutes": 5,
            "errors": 640,
            "requests": 800,
            "regions": 8,
            "threshold": 0.5,
        }
        | fields
    )


def test_new_kinds_have_their_tape_category():
    assert events.DAILY_TEST_STATUSES == ("done", "failed")
    assert {kind: category(kind) for kind in KINDS} == KINDS


async def test_helpers_write_the_payload_contract():
    conn = Recorder()
    await emit_all(conn, 7, 3)
    started, finished, estimate, down, recovered, rise = conn.rows
    assert [r["kind"] for r in conn.rows] == list(KINDS)
    assert started | {"at": None} == {
        "at": None,
        "kind": "daily_test_started",
        "batch_id": None,
        "product_id": 7,
        "country_code": None,
        "target_id": None,
        "region_id": 3,
        "signal": 0,
        "severity": "info",
        "data": {"region": "fra1", "countries": ["DE", "FR"]},
    }
    assert (finished["signal"], finished["severity"], finished["data"]) == (
        1,
        "info",
        {
            "region": "fra1",
            "countries": ["DE", "FR"],
            "status": "done",
            "requests": 120000,
            "unique_ips": 41000,
            "duration_s": 1800.5,
        },
    )
    assert (estimate["region_id"], estimate["signal"], estimate["data"]) == (
        None,
        0,
        {"day": "2026-11-02", "hour": 13, "formula_version": "f1"},
    )
    assert (down["signal"], down["severity"], down["region_id"], down["data"]) == (
        -1,
        "major",
        None,
        {
            "window_start": "2026-11-02T13:55:00+00:00",
            "window_minutes": 5,
            "errors": 640,
            "requests": 800,
            "regions": 8,
            "threshold": 0.5,
            "outage_ids": [11, 12],
        },
    )
    assert (recovered["signal"], recovered["data"]) == (
        1,
        {"down_since": "2026-11-02T13:35:00+00:00", "duration_s": 1500.0, "outage_ids": [11, 12]},
    )
    assert (rise["country_code"], rise["signal"], rise["severity"], rise["data"]) == (
        "DE",
        -1,
        "minor",
        {"p50": 1840.0, "baseline": 400.0, "baseline_source": "daily"},
    )


async def test_failed_daily_test_is_bad_news_and_unknown_counters_are_left_out():
    conn = Recorder()
    test = events.DailyTest(product_id=1, region_id=2, region="sgp1", countries=["SG"])
    await events.emit_daily_test_finished(conn, AT, test, events.DailyTestResult(status="failed"))
    row = conn.rows[0]
    assert (row["signal"], row["severity"], row["data"]) == (
        -1,
        "minor",
        {"region": "sgp1", "countries": ["SG"], "status": "failed"},
    )


def hour(**fields):
    return events.HourEstimate(
        **{"product_id": 1, "day": date(2026, 11, 2), "hour": 13, "formula_version": "f1"} | fields
    )


def rise(**fields):
    return events.LatencyRise(
        **{"product_id": 1, "country_code": "DE", "p50": 500.0, "baseline": 400.0} | fields
    )


BROKEN = {
    "no countries": lambda: daily(1, 1, []),
    "lower-case country": lambda: daily(1, 1, ["de"]),
    "alpha-3 country": lambda: daily(1, 1, ["DEU"]),
    "unknown status": lambda: events.DailyTestResult(status="stalled"),
    "negative requests": lambda: events.DailyTestResult(status="done", requests=-1),
    "boolean counter": lambda: events.DailyTestResult(status="done", unique_ips=True),
    "hour 24": lambda: hour(hour=24),
    "hour -1": lambda: hour(hour=-1),
    "boolean hour": lambda: hour(hour=True),
    "no formula": lambda: hour(formula_version=""),
    "negative estimate": lambda: hour(unique_ips=-5),
    "threshold above 1": lambda: window(threshold=1.5),
    "negative errors": lambda: window(errors=-3),
    "more errors than requests": lambda: window(errors=801),
    "no requests": lambda: window(errors=0, requests=0),
    "zero-minute window": lambda: window(window_minutes=0),
    "no region": lambda: window(regions=0),
    "below the threshold": lambda: window(errors=399),
    "NaN threshold": lambda: window(threshold=float("nan")),
    "NaN counter": lambda: window(requests=float("nan")),
    "infinite p50": lambda: rise(p50=float("inf")),
    "NaN baseline": lambda: rise(baseline=float("nan")),
    "infinite duration": lambda: events.DailyTestResult(status="done", duration_s=float("inf")),
    "no rise": lambda: rise(p50=300.0),
    "bad country": lambda: rise(country_code="xx"),
    "unknown baseline source": lambda: rise(baseline_source="pulse"),
}


@pytest.mark.parametrize("build", BROKEN.values(), ids=BROKEN.keys())
def test_payloads_refuse_broken_fields(build):
    with pytest.raises(ValueError):
        build()


async def test_recovery_before_the_outage_is_refused():
    conn = Recorder()
    with pytest.raises(ValueError):
        await events.emit_pulse_recovered(
            conn, AT, product_id=1, down_since=AT + timedelta(seconds=1)
        )
    assert conn.rows == []


async def test_events_api_lists_the_new_kinds_under_their_category(database):
    seed(database)
    with connect(database) as conn:
        alpha = product(conn, "alpha")
        partition(conn, "test_events", AT)
    db = Database(database)
    try:
        async with db.transaction() as conn:
            await emit_all(conn, alpha, REGION_IDS["fra1"])
    finally:
        await db.close()
    with connect(database) as conn:
        listed = list_events(conn)
        assert {e["kind"]: e["category"] for e in listed} == KINDS
        # The SQL the API filters a tab with agrees with category() for every new kind.
        for tab in ("census", "pulse", "incident"):
            assert {e["kind"] for e in list_events(conn, category=tab)} == {
                kind for kind, c in KINDS.items() if c == tab
            }
    started = next(e for e in listed if e["kind"] == "daily_test_started")
    assert (started["product"], started["region"], started["country"]) == (
        "alpha/residential",
        "fra1",
        None,
    )
