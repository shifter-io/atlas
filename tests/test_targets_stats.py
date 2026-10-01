import math

import pytest

from proxy_framework import ProxyConfig, RunConfig
from proxy_framework.stats import Histogram, bucket_of
from proxy_framework.targets import EchoIpTarget, TargetRegistry, registry


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
    assert not registry.get("generic_https").pool_eligible


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
