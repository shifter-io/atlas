"""Outcome classification: status codes, body sniffing, verdicts from errors, the echo target's
assessment, and the transport verdicts that reach the run summary."""

import pytest
from helpers import Gateway, server

from atlas import JsonSink, ProxyConfig, RunConfig, bytes_io, run
from atlas.models import Outcome
from atlas.outcome import (
    CHALLENGE_MARKERS,
    GatewayCode,
    Verdict,
    classify_status,
    failed_response,
    from_error,
    gateway_code,
    sniff_body,
)
from atlas.targets import EchoIpTarget, TargetRegistry, registry
from atlas.transport import HttpTransport

CLOUDFLARE = b"<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"
BLOCKED = b"<html><body><h1>Access Denied</h1>You don't have permission.</body></html>"


@pytest.mark.parametrize(
    "status,verdict",
    [
        (200, Verdict.OK),
        (204, Verdict.OK),
        (301, Verdict.OK),
        # A block is the target refusing us.
        (403, Verdict.BLOCK),
        (429, Verdict.BLOCK),
        (451, Verdict.BLOCK),
        # The proxy side failing is never a block.
        (407, Verdict.PROXY_ERROR),
        (502, Verdict.PROXY_ERROR),
        (503, Verdict.PROXY_ERROR),
        (504, Verdict.PROXY_ERROR),
        (509, Verdict.PROXY_ERROR),
        # A target that is broken, or answers something else, did not refuse us either.
        (500, Verdict.ERROR),
        (404, Verdict.ERROR),
        (401, Verdict.ERROR),
        (418, Verdict.ERROR),
        (100, Verdict.ERROR),
    ],
)
def test_classify_status(status, verdict):
    assert classify_status(status) is verdict


@pytest.mark.parametrize(
    "body,verdict",
    [
        (CLOUDFLARE, Verdict.CHALLENGE),
        ("Please verify you are human", Verdict.CHALLENGE),
        (b"<div class='g-recaptcha'>", Verdict.CHALLENGE),
        (BLOCKED, Verdict.BLOCK),
        ("You have been BLOCKED", Verdict.BLOCK),
        (b"203.0.113.8\n", None),
        (b"", None),
        # Markers past the first 4 KB are not looked at.
        (b" " * 5000 + b"captcha", None),
        (b"\xff\xfe captcha", Verdict.CHALLENGE),
    ],
)
def test_sniff_body(body, verdict):
    assert sniff_body(body) == verdict


def test_every_challenge_marker_is_lower_case():
    assert all(marker == marker.lower() for marker in CHALLENGE_MARKERS)


@pytest.mark.parametrize(
    "status,body,verdict",
    [
        (403, CLOUDFLARE, Verdict.CHALLENGE),
        (503, CLOUDFLARE, Verdict.CHALLENGE),
        (403, b"nope", Verdict.BLOCK),
        (200, BLOCKED, Verdict.BLOCK),
        (200, b"garbage", Verdict.ERROR),
        (201, b"203.0.113.8", Verdict.ERROR),
        (302, b"", Verdict.ERROR),
        (502, CLOUDFLARE, Verdict.PROXY_ERROR),
        (503, b"", Verdict.PROXY_ERROR),
        (504, b"Gateway Timeout", Verdict.PROXY_ERROR),
        (503, BLOCKED, Verdict.BLOCK),
        (500, b"oops", Verdict.ERROR),
        (404, b"", Verdict.ERROR),
    ],
)
def test_failed_response(status, body, verdict):
    assert failed_response(status, body) is verdict


@pytest.mark.parametrize(
    "error,verdict",
    [
        (None, "ok"),
        ("timeout", "timeout"),
        ("proxy_auth", "proxy_error"),
        ("proxy_connect", "proxy_error"),
        ("challenge", "challenge"),
        ("blocked", "block"),
        ("tls", "error"),
        ("dns", "proxy_error"),
        ("connection", "proxy_error"),
        ("http_status", "error"),
    ],
)
def test_default_verdict_from_error_code(error, verdict):
    assert from_error(error) == verdict
    assert Outcome(1, error=error, ip=None if error else "203.0.113.8").verdict == verdict


def test_outcome_rejects_inconsistent_verdicts():
    with pytest.raises(ValueError):
        Outcome(1, ip="203.0.113.8", verdict="block")
    with pytest.raises(ValueError):
        Outcome(1, error="http_status", verdict="ok")
    with pytest.raises(ValueError):
        Outcome(1, error="http_status", verdict="maybe")
    outcome = Outcome(1, error="http_status", verdict=Verdict.BLOCK)
    assert outcome.verdict == "block" and type(outcome.verdict) is str
    assert (outcome.bytes_up, outcome.bytes_down, outcome.bytes_exact) == (0, 0, False)


def test_gateway_code():
    assert gateway_code(407, geo_requested=True) is GatewayCode.UNSUPPORTED_GEO
    assert gateway_code(407, geo_requested=False) is GatewayCode.BAD_CREDENTIALS
    assert gateway_code(509, geo_requested=False) is GatewayCode.BANDWIDTH_EXHAUSTED
    assert gateway_code(500, geo_requested=True) is GatewayCode.GATEWAY_ERROR


@pytest.mark.parametrize(
    "status,body,error,verdict",
    [
        (200, b"203.0.113.8\n", None, "ok"),
        (200, CLOUDFLARE, "challenge", "challenge"),
        (200, BLOCKED, "blocked", "block"),
        (200, b"<html>203.0.113.8</html>", "invalid_body", "error"),
        (403, b"", "http_status", "block"),
        (403, CLOUDFLARE, "http_status", "challenge"),
        (407, b"", "proxy_auth", "proxy_error"),
        (502, b"", "http_status", "proxy_error"),
        (503, b"", "http_status", "proxy_error"),
        (500, b"", "http_status", "error"),
        (301, b"", "http_status", "error"),
    ],
)
def test_echo_target_requires_a_bare_ip(status, body, error, verdict):
    result = registry.get("generic_http").assess(status, body)
    assert (result.error, result.verdict) == (error, verdict)
    assert (result.ip is not None) == (verdict == "ok")


def config(port, **kwargs):
    return RunConfig("generic_http", ProxyConfig("http", "127.0.0.1", port), **kwargs)


@pytest.mark.parametrize(
    "status,body,error,counter",
    [
        (200, CLOUDFLARE, "challenge", "challenges"),
        # A challenge page larger than the read limit is still recognised.
        (200, CLOUDFLARE + b" " * 1000, "challenge", "challenges"),
        (200, BLOCKED, "blocked", "blocks"),
        (403, b"denied", "http_status", "blocks"),
        (429, b"x" * 1000, "body_too_large", "blocks"),
        (407, b"auth", "proxy_auth", "proxy_errors"),
        (502, b"", "http_status", "proxy_errors"),
        (504, b"", "http_status", "proxy_errors"),
    ],
)
async def test_transport_verdicts_reach_the_summary(tmp_path, status, body, error, counter):
    gateway = Gateway(status=status, body=body)
    async with server(gateway) as port:
        result = await run(config(port, requests=3), JsonSink(tmp_path))
    assert result.errors == {error: 3} and result.successes == 0
    assert getattr(result, counter) == 3
    others = {"challenges", "blocks", "timeouts", "proxy_errors"} - {counter}
    assert all(getattr(result, name) == 0 for name in others)
    assert result.buckets[0][counter] == 3
    assert result.latency_ms["p50"] is None


async def test_timeout_verdict(tmp_path):
    answering = Gateway()
    async with server(answering) as port:
        await run(config(port, requests=1), JsonSink(tmp_path / "sized"))
    request = answering.requests[0]
    gateway = Gateway(delay=0.2)
    async with server(gateway) as port:
        result = await run(config(port, requests=2, timeout_seconds=0.02), JsonSink(tmp_path))
    assert result.errors == {"timeout": 2} and result.timeouts == 2
    # BYTES-TRUTH-1: the requests crossed before the timeout; Linux counts them (no CONNECT on
    # plain HTTP, so the estimate elsewhere claims nothing). Nothing came back.
    assert result.bytes_down == 0
    assert result.bytes_up == (2 * len(request) if bytes_io.LINUX else 0)


async def test_connect_rejection_is_a_proxy_error():
    gateway = Gateway(auth=True)
    async with server(gateway) as port:
        targets = TargetRegistry()
        target = EchoIpTarget("generic_https", "https://localhost:1/")
        targets.register(target)
        proxy = ProxyConfig("http", "127.0.0.1", port, "test", "wrong")
        async with HttpTransport(RunConfig("generic_https", proxy)) as transport:
            outcome = await transport.execute(target)
    assert (outcome.error, outcome.verdict, outcome.status) == ("proxy_auth", "proxy_error", 407)
