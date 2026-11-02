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


async def emit_all(conn, product_id, region_id, at=AT):
    await events.emit_daily_test_started(
        conn, at, product_id=product_id, region_id=region_id, region="fra1", countries=["DE", "FR"]
    )
    await events.emit_daily_test_finished(
        conn,
        at,
        product_id=product_id,
        region_id=region_id,
        region="fra1",
        countries=["DE", "FR"],
        status="done",
        requests=120000,
        unique_ips=41000,
        duration_s=1800.5,
    )
    await events.emit_estimate_hour_released(
        conn, at, product_id=product_id, day=date(2026, 11, 2), hour=13, formula_version="f1"
    )
    await events.emit_pulse_down(
        conn,
        at,
        product_id=product_id,
        window_start=at - timedelta(minutes=5),
        window_minutes=5,
        errors=640,
        requests=800,
        regions=8,
        threshold=0.5,
    )
    await events.emit_pulse_recovered(
        conn, at, product_id=product_id, down_since=at - timedelta(minutes=25)
    )
    await events.emit_latency_rise(
        conn, at, product_id=product_id, country_code="DE", p50=1840.0, baseline=400.0
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
        },
    )
    assert (recovered["signal"], recovered["data"]) == (
        1,
        {"down_since": "2026-11-02T13:35:00+00:00", "duration_s": 1500.0},
    )
    assert (rise["country_code"], rise["signal"], rise["severity"], rise["data"]) == (
        "DE",
        -1,
        "minor",
        {"p50": 1840.0, "baseline": 400.0},
    )


async def test_failed_daily_test_is_bad_news_and_unknown_counters_are_left_out():
    conn = Recorder()
    await events.emit_daily_test_finished(
        conn, AT, product_id=1, region_id=2, region="sgp1", countries=["SG"], status="failed"
    )
    row = conn.rows[0]
    assert (row["signal"], row["severity"], row["data"]) == (
        -1,
        "minor",
        {"region": "sgp1", "countries": ["SG"], "status": "failed"},
    )


@pytest.mark.parametrize(
    ("helper", "kwargs"),
    [
        (events.emit_daily_test_started, {"countries": []}),
        (events.emit_daily_test_started, {"countries": ["de"]}),
        (events.emit_daily_test_started, {"countries": ["DEU"]}),
        (events.emit_daily_test_finished, {"status": "stalled"}),
        (events.emit_daily_test_finished, {"status": "done", "requests": -1}),
        (events.emit_daily_test_finished, {"status": "done", "unique_ips": True}),
        (events.emit_estimate_hour_released, {"hour": 24}),
        (events.emit_estimate_hour_released, {"hour": -1}),
        (events.emit_estimate_hour_released, {"hour": 13, "formula_version": ""}),
        (events.emit_pulse_down, {"threshold": 1.5}),
        (events.emit_pulse_down, {"threshold": 0.5, "errors": -3}),
        (events.emit_pulse_recovered, {"down_since": AT + timedelta(seconds=1)}),
        (events.emit_latency_rise, {"p50": 300.0}),
        (events.emit_latency_rise, {"p50": 500.0, "country_code": "xx"}),
    ],
)
async def test_helpers_refuse_a_broken_payload(helper, kwargs):
    defaults = {
        events.emit_daily_test_started: {"region_id": 1, "region": "fra1", "countries": ["DE"]},
        events.emit_daily_test_finished: {"region_id": 1, "region": "fra1", "countries": ["DE"]},
        events.emit_estimate_hour_released: {"day": date(2026, 11, 2), "formula_version": "f1"},
        events.emit_pulse_down: {
            "window_start": AT,
            "window_minutes": 5,
            "errors": 1,
            "requests": 2,
            "regions": 8,
        },
        events.emit_pulse_recovered: {},
        events.emit_latency_rise: {"country_code": "DE", "baseline": 400.0},
    }[helper]
    conn = Recorder()
    with pytest.raises(ValueError):
        await helper(conn, AT, product_id=1, **(defaults | kwargs))
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
