import math

import pytest

from milf_framework import ProxyConfig, RunConfig
from milf_framework.models import Outcome
from milf_framework.stats import LOCATION_FIELDS, NETWORK_FIELDS, Counters, Histogram, bucket_of
from milf_framework.targets import EchoIpTarget, TargetRegistry, registry


@pytest.mark.parametrize("body,expected", [
    (b"8.8.8.8\n", "8.8.8.8"), (b" 2001:4860:4860::8888 \r\n", "2001:4860:4860::8888"),
    (b"", None), (b"1.2.3.999", None), (b"001.2.3.4", None), (b"fe80::1%eth0", None),
    (b"8.8.8.8\n1.1.1.1", None), (b"<html>8.8.8.8</html>", None), (b"\xff", None),
])
def test_ip_parser(body, expected):
    result = registry.get("generic_http").assess(200, body)
    assert result.ip == expected
    assert result.error == (None if expected else "invalid_body")


@pytest.mark.parametrize("status", [201, 301, 403, 429, 500, 502])
def test_status(status):
    assert registry.get("generic_http").assess(status, b"8.8.8.8").error == "http_status"


def test_registry_extension():
    targets = TargetRegistry()
    target = EchoIpTarget("custom", "http://localhost/")
    targets.register(target)
    assert targets.get("custom") is target
    with pytest.raises(ValueError):
        targets.register(target)
    with pytest.raises(ValueError):
        targets.get("missing")
    assert registry.get("generic_http").pool_eligible
    assert registry.get("generic_https").pool_eligible


def test_histogram_merge_and_nulls():
    left, right, together = Histogram(), Histogram(), Histogram()
    assert left.percentiles() == dict.fromkeys(("p50", "p90", "p95", "p99"))
    for value in range(1, 101):
        (left if value <= 30 else right).add(value - .2)
        together.add(value - .2)
    left.merge(right)
    assert left.export() == together.export()
    assert left.percentiles() == {"p50": 50, "p90": 90, "p95": 95, "p99": 99}
    assert bucket_of(601.1) == 600


@pytest.mark.parametrize("key,value", [
    ("requests", 0), ("requests", True), ("concurrency", -2),
    ("timeout_seconds", 0), ("timeout_seconds", math.inf), ("timeout_seconds", None),
    ("requests_per_second", math.nan),
])
def test_config_validation(key, value):
    with pytest.raises(ValueError):
        RunConfig("generic_http", ProxyConfig("http", "localhost", 8080), **{key: value})


def test_credentials_are_not_in_repr_or_public_config():
    proxy = ProxyConfig.from_url("socks5h://test:p%40ss@localhost:1080")
    assert proxy.password == "p@ss"
    config = RunConfig("generic_http", proxy)
    assert "p@ss" not in repr(proxy) + repr(config) + str(config.public())
    with pytest.raises(ValueError) as exc:
        ProxyConfig.from_url("http://test:secret@localhost:not-a-port")
    assert "secret" not in str(exc.value)
    with pytest.raises(ValueError):
        ProxyConfig.from_url("http://localhost:0")


@pytest.mark.parametrize("status,verdict", [
    (201, "error"), (301, "error"), (403, "block"), (429, "block"), (500, "error"),
    (502, "proxy_error"),
])
def test_status_verdict(status, verdict):
    assert registry.get("generic_http").assess(status, b"8.8.8.8").verdict == verdict


def test_counters_track_verdicts_and_bytes():
    counters = Counters()
    for outcome in (
        Outcome(4.2, ip="8.8.8.8", status=200, bytes_up=100, bytes_down=300, bytes_exact=True),
        Outcome(9, error="challenge", status=200, bytes_up=100, bytes_down=4000),
        Outcome(9, error="http_status", status=403, verdict="block", bytes_up=90, bytes_down=50),
        Outcome(15000, error="timeout"),
        Outcome(3, error="proxy_connect"),
        Outcome(3, error="invalid_body", status=200),
    ):
        counters.add(outcome)
    exported = counters.export()
    assert exported["completed"] == 6 and exported["successes"] == 1
    assert {k: exported[k] for k in ("challenges", "blocks", "timeouts", "proxy_errors")} == {
        "challenges": 1, "blocks": 1, "timeouts": 1, "proxy_errors": 1,
    }
    assert (exported["bytes_up"], exported["bytes_down"]) == (290, 4350)
    assert exported["errors"] == {"challenge": 1, "http_status": 1, "timeout": 1,
                                  "proxy_connect": 1, "invalid_body": 1}
    # Only successes enter the latency histogram.
    assert exported["latency_histogram"]["bins"] == [(5, 1)]
    assert set(counters.export_geo()) == {
        "completed", "successes", "challenges", "blocks", "timeouts", "bytes_up",
        "bytes_down", "latency_histogram",
    }


def test_geo_fields_match_the_enrichment_lookup():
    from milf_framework.storage import ENRICHMENT_FIELDS

    assert set(LOCATION_FIELDS) | set(NETWORK_FIELDS) | {"matched"} == set(ENRICHMENT_FIELDS)
    assert not set(LOCATION_FIELDS) & set(NETWORK_FIELDS)
