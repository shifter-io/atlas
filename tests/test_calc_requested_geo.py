"""MEASURED-FIELDS-1: a daily test's requests reach its requested country's place row.

A daily test is ingested from its hour files with IP observations only (daily.ingest): no geo rows
record its requests per exit place. Its runs are placeless, and inputs.requested_geo() turns their
bucket rows into geo rows without a location, so aggregate.breakdowns() counts their requests
and successes towards the requested country's row, the row its IPs label too (one row per
country). Their latency is under load and stays out of every row's latency, the pulses' only
(HEADLINE-PULSE-1). Region and city rows get requests only from geo rows with a place (pulses).
"""

from datetime import UTC, datetime, timedelta

from atlas.calculated.aggregate import breakdowns
from atlas.calculated.calculator import _run_fields
from atlas.calculated.inputs import BucketRow, requested_geo
from atlas.names import PLACE

AS_OF = datetime(2026, 11, 1, tzinfo=UTC)
NAMES = {"US": "United States", "DE": "Germany"}
BERLIN = ("DE", "Germany", "BE", "Berlin", "Berlin")
BOSTON = ("US", "United States", "MA", "Massachusetts", "Boston")
NET = (64500, "Example Net", "isp")


def run(batch_id=None, artifacts=None, country="us"):
    """A stored run as incremental.RUNS reads it: a daily test without a geo artifact."""
    summary = {"artifacts": artifacts or {"ips": {}}, "test_kind": "daily"}
    scope = {"target": {"id": "generic_http"}, "config": {"requested_country": country}}
    return {
        "run_key": 7,
        "batch_id": batch_id,
        "summary": summary,
        "scope": scope,
        "role": "exploratory",
        "light": True,
        "loaded": True,  # a daily test of the latency target (inputs.under_load_run())
        "finished_at": AS_OF,
    }


def bucket(hours_ago, completed, successes, bins, fields):
    counters = {
        "completed": completed,
        "successes": successes,
        "challenges": 1,
        "blocks": 0,
        "latency_histogram": {"bins": bins},
    }
    at = AS_OF - timedelta(hours=hours_ago)
    return BucketRow({"bucket_start": at, "run_key": 7, "counters": counters}, fields)


def daily_rows():
    fields = _run_fields(run(), [], 1800)
    return [
        bucket(2, 100, 90, [[200, 60], [400, 30]], fields),
        bucket(30, 50, 40, [[300, 40]], fields),
        bucket(800, 9, 9, [[100, 9]], fields),  # older than the 30-day window
    ]


def test_run_fields_mark_runs_without_geo_rows():
    assert _run_fields(run(), [], 1800).fields["placeless"] is True
    with_geo = run(artifacts={"ips": {}, "geo": {"file": "geo.jsonl.gz"}})
    assert _run_fields(with_geo, [], 1800).fields["placeless"] is False
    batched = run(batch_id=3) | {"summary": None, "test": "pulse"}
    assert _run_fields(batched, [], 1800).fields["placeless"] is False


def test_requested_geo_sums_placeless_buckets_per_window_and_country():
    geo = requested_geo(daily_rows(), AS_OF, NAMES)
    assert [(g["hours"], g["fallback"], g["completed"], g["successes"]) for g in geo] == [
        (24, ("US", "United States"), 100, 90),
        (168, ("US", "United States"), 50, 40),
    ]
    assert geo[0]["location"] is None and geo[0]["network"] is None
    # A daily test's latency is under load: its requests count, its latency does not.
    assert geo[0]["challenges"] == 1 and geo[0]["histogram"] == []


def test_requested_geo_leaves_out_runs_with_geo_rows_and_heavy_latency():
    placed = _run_fields(run(artifacts={"geo": {}}), [], 1800)
    heavy = _run_fields(run() | {"light": False, "loaded": False}, [], 1800)
    light = _run_fields(run() | {"loaded": False}, [], 1800)  # a light-load run outside a batch
    rows = [bucket(1, 10, 10, [[50, 10]], placed), bucket(1, 20, 20, [[70, 20]], heavy)]
    rows.append(bucket(1, 5, 5, [[90, 5]], light))
    geo = requested_geo(rows, AS_OF, NAMES)
    assert [(g["completed"], g["histogram"]) for g in geo] == [(25, [(90, 5)])]


def ip(place):
    row = dict(zip(PLACE, place, strict=True))
    row |= dict(zip(("asn", "asn_organization", "usage_type"), NET, strict=True))
    return {"row": row, "count": 1}


def test_daily_requests_fill_the_country_row_and_pulses_the_city_row():
    ips = {1: ip(BOSTON), 2: ip(BOSTON), 3: ip(BERLIN)}
    pulse = {
        "location": BERLIN,
        "network": NET,
        "fallback": None,
        "completed": 8,
        "successes": 6,
        "challenges": 0,
        "blocks": 0,
        "histogram": [(150, 6)],
    }
    week = [g for g in requested_geo(daily_rows(), AS_OF, NAMES) if g["hours"] <= 168]
    rows = breakdowns(ips, [*week, pulse])
    us = [r for r in rows["country"] if r["country_code"] == "US"]
    assert len(us) == 1  # the IPs' row and the requests' row are one row
    assert (us[0]["ips"], us[0]["completed"], us[0]["successes"]) == (2, 150, 130)
    assert us[0]["success_rate"] == 130 / 150 and us[0]["p50"] is None  # no pulse exits there
    boston = next(r for r in rows["city"] if r["city"] == "Boston")
    assert (boston["ips"], boston["completed"], boston["p50"]) == (2, 0, None)
    berlin = next(r for r in rows["city"] if r["city"] == "Berlin")
    assert (berlin["completed"], berlin["successes"], berlin["p50"]) == (8, 6, 150)
