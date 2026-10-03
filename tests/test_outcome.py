import asyncio
import ssl
import struct
from functools import partial

import pytest
import trustme
from helpers import Gateway, server

from milf_framework import JsonSink, ProxyConfig, RunConfig, bytes_io, run
from milf_framework.models import Outcome
from milf_framework.outcome import (
    CHALLENGE_MARKERS,
    GatewayCode,
    Verdict,
    classify_status,
    failed_response,
    from_error,
    gateway_code,
    sniff_body,
)
from milf_framework.targets import EchoIpTarget, TargetRegistry, registry
from milf_framework.transport import HttpTransport

CLOUDFLARE = b"<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"
BLOCKED = b"<html><body><h1>Access Denied</h1>You don't have permission.</body></html>"


@pytest.mark.parametrize("status,verdict", [
    (200, Verdict.OK), (204, Verdict.OK), (301, Verdict.OK),
    # A block is the target refusing us.
    (403, Verdict.BLOCK), (429, Verdict.BLOCK), (451, Verdict.BLOCK),
    # The proxy side failing is never a block.
    (407, Verdict.PROXY_ERROR), (502, Verdict.PROXY_ERROR), (503, Verdict.PROXY_ERROR),
    (504, Verdict.PROXY_ERROR), (509, Verdict.PROXY_ERROR),
    # A target that is broken, or answers something else, did not refuse us either.
    (500, Verdict.ERROR), (404, Verdict.ERROR), (401, Verdict.ERROR), (418, Verdict.ERROR),
    (100, Verdict.ERROR),
])
def test_classify_status(status, verdict):
    assert classify_status(status) is verdict


@pytest.mark.parametrize("body,verdict", [
    (CLOUDFLARE, Verdict.CHALLENGE), ("Please verify you are human", Verdict.CHALLENGE),
    (b"<div class='g-recaptcha'>", Verdict.CHALLENGE), (BLOCKED, Verdict.BLOCK),
    ("You have been BLOCKED", Verdict.BLOCK), (b"8.8.8.8\n", None), (b"", None),
    # Markers past the first 4 KB are not looked at.
    (b" " * 5000 + b"captcha", None), (b"\xff\xfe captcha", Verdict.CHALLENGE),
])
def test_sniff_body(body, verdict):
    assert sniff_body(body) == verdict


def test_every_challenge_marker_is_lower_case():
    assert all(marker == marker.lower() for marker in CHALLENGE_MARKERS)


@pytest.mark.parametrize("status,body,verdict", [
    (403, CLOUDFLARE, Verdict.CHALLENGE), (503, CLOUDFLARE, Verdict.CHALLENGE),
    (403, b"nope", Verdict.BLOCK), (200, BLOCKED, Verdict.BLOCK),
    (200, b"garbage", Verdict.ERROR), (201, b"8.8.8.8", Verdict.ERROR),
    (302, b"", Verdict.ERROR), (502, CLOUDFLARE, Verdict.PROXY_ERROR),
    (503, b"", Verdict.PROXY_ERROR), (504, b"Gateway Timeout", Verdict.PROXY_ERROR),
    (503, BLOCKED, Verdict.BLOCK), (500, b"oops", Verdict.ERROR), (404, b"", Verdict.ERROR),
])
def test_failed_response(status, body, verdict):
    assert failed_response(status, body) is verdict


@pytest.mark.parametrize("error,verdict", [
    (None, "ok"), ("timeout", "timeout"), ("proxy_auth", "proxy_error"),
    ("proxy_connect", "proxy_error"), ("challenge", "challenge"), ("blocked", "block"),
    ("tls", "error"), ("dns", "proxy_error"), ("connection", "proxy_error"),
    ("http_status", "error"),
])
def test_default_verdict_from_error_code(error, verdict):
    assert from_error(error) == verdict
    assert Outcome(1, error=error, ip=None if error else "8.8.8.8").verdict == verdict


def test_outcome_rejects_inconsistent_verdicts():
    with pytest.raises(ValueError):
        Outcome(1, ip="8.8.8.8", verdict="block")
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


@pytest.mark.parametrize("status,body,error,verdict", [
    (200, b"8.8.8.8\n", None, "ok"),
    (200, CLOUDFLARE, "challenge", "challenge"),
    (200, BLOCKED, "blocked", "block"),
    (200, b"<html>8.8.8.8</html>", "invalid_body", "error"),
    (403, b"", "http_status", "block"),
    (403, CLOUDFLARE, "http_status", "challenge"),
    (407, b"", "proxy_auth", "proxy_error"),
    (502, b"", "http_status", "proxy_error"),
    (503, b"", "http_status", "proxy_error"),
    (500, b"", "http_status", "error"),
    (301, b"", "http_status", "error"),
])
def test_echo_target_requires_a_bare_ip(status, body, error, verdict):
    result = registry.get("generic_http").assess(status, body)
    assert (result.error, result.verdict) == (error, verdict)
    assert (result.ip is not None) == (verdict == "ok")


def config(port, **kwargs):
    return RunConfig("generic_http", ProxyConfig("http", "127.0.0.1", port), **kwargs)


@pytest.mark.parametrize("status,body,error,counter", [
    (200, CLOUDFLARE, "challenge", "challenges"),
    # A challenge page larger than the read limit is still recognised.
    (200, CLOUDFLARE + b" " * 1000, "challenge", "challenges"),
    (200, BLOCKED, "blocked", "blocks"),
    (403, b"denied", "http_status", "blocks"),
    (429, b"x" * 1000, "body_too_large", "blocks"),
    (407, b"auth", "proxy_auth", "proxy_errors"),
    (502, b"", "http_status", "proxy_errors"),
    (504, b"", "http_status", "proxy_errors"),
])
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
    gateway = Gateway(delay=.2)
    async with server(gateway) as port:
        result = await run(config(port, requests=2, timeout_seconds=.02), JsonSink(tmp_path))
    assert result.errors == {"timeout": 2} and result.timeouts == 2
    assert result.bytes_up == result.bytes_down == 0


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


async def test_plain_http_estimate_matches_the_wire():
    gateway = Gateway(auth=True)
    async with server(gateway) as port:
        proxy = ProxyConfig("http", "127.0.0.1", port, "test", "secret")
        async with HttpTransport(RunConfig("generic_http", proxy)) as transport:
            outcome = await transport.execute(registry.get("generic_http"))
    assert outcome.verdict == "ok"
    sent = (b"HTTP/1.1 200 Test\r\nContent-Length: 8\r\nLocation: http://localhost/must-not-"
            b"follow\r\nConnection: close\r\n\r\n8.8.8.8\n")
    # Kernel counters on Linux, a sized estimate elsewhere: both are exact here.
    assert outcome.bytes_exact == bytes_io.LINUX
    assert outcome.bytes_up == len(gateway.requests[0])
    assert outcome.bytes_down == len(sent)


@pytest.mark.parametrize("scheme", ["http", "socks5"])
@pytest.mark.parametrize("tls", [False, True])
async def test_bytes_counted_for_every_proxy_and_target(tmp_path, scheme, tls):
    tls_server = tls_client = None
    if tls:
        ca = trustme.CA()
        tls_server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ca.issue_cert("localhost").configure_cert(tls_server)
        tls_client = ssl.create_default_context()
        ca.configure_trust(tls_client)

    async def origin(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        await Gateway().respond(writer)

    gateway = Gateway(scheme, auth=True)
    async with server(origin, tls=tls_server) as origin_port, server(gateway) as port:
        name = "generic_https" if tls else "generic_http"
        targets = TargetRegistry()
        targets.register(EchoIpTarget(name, f"{'https' if tls else 'http'}://localhost:"
                                      f"{origin_port}/", not tls))
        proxy = ProxyConfig(scheme, "127.0.0.1", port, "test", "secret")
        result = await run(
            RunConfig(name, proxy, requests=2), JsonSink(tmp_path), targets=targets,
            transport_factory=partial(HttpTransport, ssl_context=tls_client),
        )
    assert result.successes == 2
    # Request line + headers up; status line + headers + body down, per request.
    assert result.bytes_up > 2 * 40 and result.bytes_down > 2 * 100
    if tls and not bytes_io.LINUX:
        handshake = bytes_io.TLS_HANDSHAKE_DOWN_BYTES
        assert result.bytes_down >= 2 * handshake
    assert result.buckets[0]["bytes_up"] == result.bytes_up


class FakeSocket:
    def __init__(self, raw=None, error=None):
        self.raw, self.error = raw, error

    def getsockopt(self, level, option, size):
        if self.error:
            raise self.error
        return self.raw


def test_tcp_info_parsing(monkeypatch):
    raw = bytearray(232)
    struct.pack_into("<Q", raw, 96, 1201)
    struct.pack_into("<Q", raw, 104, 5300)
    monkeypatch.setattr(bytes_io, "LINUX", True)
    assert bytes_io.from_socket(FakeSocket(bytes(raw))) == bytes_io.ByteCount(1200, 5300, True)
    assert bytes_io.from_socket(FakeSocket(error=OSError())) is None
    assert bytes_io.from_socket(FakeSocket(b"short")) is None
    assert bytes_io.from_socket(None) is None
    monkeypatch.setattr(bytes_io, "LINUX", False)
    assert bytes_io.from_socket(FakeSocket(bytes(raw))) is None


def test_estimate_and_sizes():
    plain = bytes_io.estimate(request_line=10, request_headers=20, request_body=5,
                              response_headers=30, response_body=8)
    assert plain == bytes_io.ByteCount(35, 38, exact=False)
    tunnelled = bytes_io.estimate(request_line=10, request_headers=20, request_body=5,
                                  response_headers=30, response_body=8, tls=True)
    assert tunnelled.up == 35 + bytes_io.CONNECT_REQUEST_BYTES + bytes_io.TLS_HANDSHAKE_UP_BYTES
    socks = bytes_io.estimate(request_line=10, request_headers=20, request_body=5,
                              response_headers=30, response_body=8, tls=True, connect=False,
                              extra_up=7, extra_down=3)
    assert socks.up == 42 + bytes_io.TLS_HANDSHAKE_UP_BYTES
    assert socks.down == 41 + bytes_io.TLS_HANDSHAKE_DOWN_BYTES
    resumed = bytes_io.estimate(request_line=0, request_headers=0, request_body=0,
                                response_headers=0, response_body=0, tls=True, tls_resumed=True)
    assert resumed.down == bytes_io.CONNECT_RESPONSE_BYTES + bytes_io.TLS_RESUMED_DOWN_BYTES
    # greeting 4 + auth 3+4+6 + connect 4+1+9+2 up; 2 + 2 + 10 down.
    assert bytes_io.socks5_handshake(username="test", password="secret",
                                     host="localhost") == (33, 14)
    assert bytes_io.socks5_handshake(username=None, password=None, host="a.b") == (13, 12)
    assert bytes_io.header_bytes({"Host": "x"}) == 2 + 9
    assert bytes_io.header_bytes([(b"A", b"bc")]) == 2 + 7
    total = bytes_io.ByteCount(1, 2) + bytes_io.ByteCount(3, 4, exact=False)
    assert total == bytes_io.ByteCount(4, 6, exact=False)
    total += bytes_io.ByteCount(1, 1)
    assert total == bytes_io.ByteCount(5, 7, exact=False)


class Meter:
    """A loopback relay in front of the gateway that counts bytes each way."""

    def __init__(self, upstream_port):
        self.port, self.up, self.down = upstream_port, 0, 0

    async def __call__(self, reader, writer):
        remote_reader, remote_writer = await asyncio.open_connection("127.0.0.1", self.port)

        async def copy(source, sink, attribute):
            try:
                while data := await source.read(65536):
                    setattr(self, attribute, getattr(self, attribute) + len(data))
                    sink.write(data)
                    await sink.drain()
            finally:
                sink.close()

        await asyncio.gather(copy(reader, remote_writer, "up"), copy(remote_reader, writer, "down"),
                             return_exceptions=True)


@pytest.mark.parametrize("scheme", ["http", "socks5"])
async def test_plaintext_estimate_equals_metered_bytes_for_both_proxies(scheme):
    async def origin(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        await Gateway().respond(writer)

    gateway = Gateway(scheme, auth=True)
    async with server(origin) as origin_port, server(gateway) as gateway_port:
        meter = Meter(gateway_port)
        async with server(meter) as port:
            target = EchoIpTarget("generic_http", f"http://localhost:{origin_port}/", True)
            proxy = ProxyConfig(scheme, "127.0.0.1", port, "test", "secret")
            async with HttpTransport(RunConfig("generic_http", proxy)) as transport:
                outcome = await transport.execute(target)
    assert outcome.verdict == "ok"
    assert (outcome.bytes_up, outcome.bytes_down) == (meter.up, meter.down)
