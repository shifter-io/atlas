"""storage_read's checks of a run directory: every malformed input is refused before a write."""

import gzip
import hashlib
import json
from datetime import UTC, datetime

import pytest

from atlas import storage_read
from atlas.storage_read import (
    _bucket,
    _check_bucket_histograms,
    _dimension_item,
    _geo_rows,
    _histogram,
    _read_buckets,
    _read_run,
    _reconcile_counters,
    _scope,
    _time,
)

START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
HISTOGRAM = {"format": "ceil_ms_v1", "bins": [[10, 1]]}


def counters(**extra):
    """A pre-v6 bucket (no outcome counters): two answers, one success at 10 ms."""
    return {
        "bucket_start": START.isoformat(timespec="microseconds"),
        "completed": 2,
        "successes": 1,
        "response_body_bytes": 7,
        "errors": {"timeout": 1},
        "status_codes": {"200": 1},
        "latency_histogram": HISTOGRAM,
    } | extra


def summary(**extra):
    """Run counters that reconcile: one request, one success."""
    return {
        "requested": 1,
        "started": 1,
        "completed": 1,
        "successes": 1,
        "not_started": 0,
        "cancelled": 0,
        "total_errors": 0,
        "errors": {},
        "latency_histogram": HISTOGRAM,
    } | extra


def test_connect_refuses_unknown_connection_fields(tmp_path):
    path = tmp_path / "db.toml"
    path.write_text('[postgres]\nhost = "127.0.0.1"\nport = "5432"\noptions = "-c x=y"\n')
    path.chmod(0o600)
    with pytest.raises(ValueError, match="Unsupported database connection field"):
        storage_read.connect(path)


def test_time_requires_a_timezone():
    assert _time("2026-10-01T14:00:00+02:00") == START
    with pytest.raises(ValueError, match="require a timezone"):
        _time("2026-10-01T12:00:00")


@pytest.mark.parametrize(
    "bins",
    [[[-1, 1]], [[10, 0]], [[10.5, 1]], [[10, True]], [[2**31, 1]]],
)
def test_histogram_refuses_invalid_bins(bins):
    with pytest.raises(ValueError, match="Invalid latency histogram"):
        _histogram({"format": "ceil_ms_v1", "bins": bins})


def test_histogram_refuses_other_formats_and_sums_repeated_bins():
    with pytest.raises(ValueError, match="Unsupported latency histogram"):
        _histogram({"format": "floor_ms", "bins": []})
    assert _histogram({"format": "ceil_ms_v1", "bins": [[10, 1], [10, 2]]}).counts == {10: 3}


def test_bucket_keeps_unknown_histogram_keys_and_untyped_outcomes_in_extra():
    histogram = HISTOGRAM | {"note": "x"}
    row = _bucket(START, counters(latency_histogram=histogram, challenges=1))
    # Not every outcome counter is present: typed is false, the partial one stays in extra.
    assert row[-7] is False and row[-6:] == (0,) * 6
    assert row[7].obj == {"latency_histogram": histogram, "challenges": 1}
    assert row[:3] == (2, 1, 7) and row[5:7] == ([10], [1])


def test_bucket_refuses_negative_counters():
    with pytest.raises(ValueError, match="Invalid bucket counters"):
        _bucket(START, counters(successes=-1))


@pytest.mark.parametrize(
    ("item", "kind", "value"),
    [(3, "double precision", 3.0), (2**40, "bigint", 2**40), ("Berlin", "text", "Berlin")],
)
def test_dimension_item_types_each_column(item, kind, value):
    assert _dimension_item(item, kind) == value
    assert type(_dimension_item(item, kind)) is type(value)


@pytest.mark.parametrize(
    ("item", "kind"),
    [(float("nan"), "double precision"), (2**63, "bigint"), (True, "bigint"), (1, "text")],
)
def test_dimension_item_refuses_values_outside_the_column(item, kind):
    with pytest.raises(ValueError, match="Invalid geo dimension"):
        _dimension_item(item, kind)


def geo_file(directory, rows):
    path = directory / "geo.jsonl.gz"
    with gzip.open(path, "wt") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in rows)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"artifacts": {"geo": {"file": path.name, "sha256": digest, "rows": len(rows)}}}


def geo_row(**extra):
    return {
        "bucket_start": START.isoformat(),
        "location": None,
        "network": {"asn": 3320, "asn_organization": "Deutsche Telekom AG"},
        "latency_histogram": HISTOGRAM,
        "completed": 2,
        "successes": 1,
        "challenges": 5,
        "blocks": 0,
        "timeouts": 1,
        "bytes_up": 0,
        "bytes_down": 0,
    } | extra


def test_geo_rows_of_a_pre_v6_bucket_reconcile_only_answers_and_successes(tmp_path):
    buckets = {START: counters()}
    rows = _geo_rows(
        tmp_path, geo_file(tmp_path, [geo_row()]), buckets, {START: _bucket(START, buckets[START])}
    )
    # The geo challenges (5) are not compared: the bucket has no typed outcome counters.
    assert len(rows) == 1
    assert rows[0][1:8] == (None,) * 7 and rows[0][10] == 3320 and rows[0][14:16] == (2, 1)


def test_geo_rows_of_a_typed_bucket_reconcile_the_outcomes(tmp_path):
    outcomes = dict(challenges=0, blocks=0, timeouts=1, proxy_errors=0, bytes_up=0, bytes_down=0)
    buckets = {START: counters(**outcomes)}
    with pytest.raises(ValueError, match="do not reconcile with run buckets"):
        _geo_rows(
            tmp_path,
            geo_file(tmp_path, [geo_row()]),
            buckets,
            {START: _bucket(START, buckets[START])},
        )


def test_bucket_histograms_must_count_each_buckets_successes():
    with pytest.raises(ValueError, match="Bucket histogram does not reconcile"):
        _check_bucket_histograms({START: counters(successes=2)}, _histogram(HISTOGRAM))


def test_reconcile_counters_requires_the_histogram_to_count_the_successes():
    attempted, in_country, histogram = _reconcile_counters(summary())
    assert (attempted, in_country, histogram.counts) == (1, 1, {10: 1})
    with pytest.raises(ValueError, match="Latency histogram does not reconcile"):
        _reconcile_counters(summary(latency_histogram={"format": "ceil_ms_v1", "bins": []}))


def test_scope_requires_provider_and_product_labels():
    with pytest.raises(ValueError, match="requires provider and product labels"):
        _scope({"target": "echo", "config": {"provider": "alpha", "product": ""}})
    provider, product, scope, scope_hash = _scope(
        {"target": "echo", "config": {"provider": "alpha", "product": "res", "requests": 5}}
    )
    # Run-length settings stay out of the scope, so the hash ignores them.
    assert (provider, product) == ("alpha", "res")
    assert scope == {"target": "echo", "config": {"provider": "alpha", "product": "res"}}
    assert len(scope_hash) == 64


def test_read_buckets_refuses_two_buckets_with_one_start():
    bucket = counters(completed=1, successes=1, errors={})
    with pytest.raises(ValueError, match="Bucket counters do not reconcile"):
        _read_buckets(summary(buckets=[bucket, bucket]), _histogram(HISTOGRAM))


def test_read_run_refuses_other_schema_versions(tmp_path):
    (tmp_path / "summary.json").write_text(json.dumps({"schema_version": "2"}))
    with pytest.raises(ValueError, match="Unsupported measurement schema"):
        _read_run(tmp_path)
