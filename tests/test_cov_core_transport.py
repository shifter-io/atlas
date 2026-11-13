"""transport.py paths the end-to-end transport tests do not reach (COV-7): misuse refusals,
the wire-size estimate used where the kernel has no TCP_INFO, the oversized-body sniff and the
failure classification of each exception family. Loopback gateways only."""

import ssl
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp_socks import ProxyConnectionError, ProxyError
from helpers import Gateway, server
from yarl import URL

from atlas import bytes_io
from atlas.models import ProxyConfig, RunConfig
from atlas.outcome import Verdict
from atlas.targets import Assessment, RequestSpec, registry
from atlas.transport import HttpTransport, _assess, _classify, _estimate


async def test_execute_outside_the_context_manager_is_refused():
    transport = HttpTransport(RunConfig("generic_http", ProxyConfig("http", "127.0.0.1", 9)))
    with pytest.raises(RuntimeError, match="async context manager"):
        await transport.execute(registry.get("generic_http"))


class Switching:
    """A source that resolves an HTTP network to a SOCKS5 proxy."""

    scheme = "http"

    def resolve(self, context):
        return ProxyConfig("socks5", "127.0.0.1", 9)

    def public(self):
        return {"proxy_protocol": "http"}


async def test_a_source_may_not_change_the_scheme_within_a_run():
    async with HttpTransport(RunConfig("generic_http", Switching())) as transport:
        with pytest.raises(ValueError, match="scheme cannot change"):
            await transport.execute(registry.get("generic_http"))


async def test_without_tcp_info_the_bytes_are_estimated_from_the_wire(monkeypatch):
    # macOS has no TCP_INFO: the estimate from the request and response sizes is used.
    monkeypatch.setattr(bytes_io, "from_socket", lambda sock: None)
    gateway = Gateway(auth=True)
    async with server(gateway) as port:
        proxy = ProxyConfig("http", "127.0.0.1", port, "test", "secret")
        async with HttpTransport(RunConfig("generic_http", proxy)) as transport:
            result = await transport.execute(registry.get("generic_http"))
    assert result.error is None and result.ip == "203.0.113.8"
    assert result.bytes_exact is False
    sent = (
        f"HTTP/1.1 200 Test\r\nContent-Length: {len(gateway.body)}\r\n"
        "Location: http://localhost/must-not-follow\r\nConnection: close\r\n\r\n"
    ).encode() + gateway.body
    # Plain HTTP through a gateway: no CONNECT, no handshake, so the estimate is exact.
    assert result.bytes_down == len(sent)
    assert result.bytes_up == len(gateway.requests[0])


class Exploding:
    id, version, engine, pool_eligible = "exploding", "1", "http", False

    def build_request(self):
        return RequestSpec("http://example.test/")

    def assess(self, status, body):
        raise LookupError("a bug in the target")


async def test_an_unclassified_failure_propagates():
    async with server(Gateway()) as port:
        config = RunConfig("generic_http", ProxyConfig("http", "127.0.0.1", port))
        async with HttpTransport(config) as transport:
            with pytest.raises(LookupError, match="a bug in the target"):
                await transport.execute(Exploding())


class Echo:
    def assess(self, status, body):
        return Assessment(ip="203.0.113.8")


def test_an_oversized_200_is_sniffed_for_a_challenge_page():
    spec = RequestSpec("http://example.test/", max_body_bytes=16)
    page = b"<html><title>Just a moment...</title></html>"
    assert _assess(Echo(), spec, 200, page) == ("challenge", None, Verdict.CHALLENGE)
    assert _assess(Echo(), spec, 200, b"x" * 17) == ("body_too_large", None, Verdict.ERROR)
    assert _assess(Echo(), spec, 200, b"203.0.113.8") == (None, "203.0.113.8", Verdict.OK)


def response(url, *, version=None):
    return SimpleNamespace(
        request_info=SimpleNamespace(url=URL(url), method="GET", headers={"Host": "example.test"}),
        version=version,
        status=200,
        reason="OK",
        raw_headers=((b"Content-Length", b"2"),),
    )


def test_a_socks5_estimate_adds_the_negotiation():
    proxy = ProxyConfig("socks5", "127.0.0.1", 1080, "u", "p")
    spec = RequestSpec("http://example.test/ip")
    counted = _estimate(spec, response("http://example.test/ip"), proxy, 2, False)
    # Request line "GET /ip HTTP/1.1\r\n" (18) + headers (22) + greeting, auth and
    # CONNECT to a 12-character host (28); status line (17) + headers (21) + body (2) +
    # the replies (14).
    assert (counted.up, counted.down, counted.exact) == (68, 54, False)


def test_an_https_estimate_adds_the_connect_credential_and_handshake():
    proxy = ProxyConfig("http", "127.0.0.1", 8080, "u", "p")
    spec = RequestSpec("https://example.test/ip")
    version = SimpleNamespace(major=1, minor=1)
    counted = _estimate(spec, response("https://example.test/ip", version=version), proxy, 2, True)
    # The tunnelled request carries only the path; the credential rides on the CONNECT
    # ("Proxy-Authorization: Basic dTpw\r\n", 33), plus the CONNECT exchange (60/39) and a
    # full handshake (600/4400).
    assert (counted.up, counted.down) == (18 + 22 + 33 + 60 + 600, 17 + 21 + 2 + 39 + 4400)


def chained(error, cause):
    error.__cause__ = cause
    return error


@pytest.mark.parametrize(
    "error,expected",
    [
        (
            aiohttp.ClientConnectorDNSError(SimpleNamespace(), OSError(-2, "unknown")),
            ("dns", Verdict.PROXY_ERROR, None),
        ),
        (ProxyConnectionError(111, "refused"), ("proxy_connect", Verdict.PROXY_ERROR, None)),
        (ProxyError("Authentication failed"), ("proxy_auth", Verdict.PROXY_ERROR, None)),
        (ProxyError("General SOCKS server failure"), ("proxy_connect", Verdict.PROXY_ERROR, None)),
        (aiohttp.ClientPayloadError("truncated"), ("protocol", Verdict.ERROR, None)),
        (chained(aiohttp.ClientOSError(), ssl.SSLError()), ("tls", Verdict.ERROR, None)),
        (ConnectionResetError(), ("connection", Verdict.PROXY_ERROR, None)),
        (aiohttp.ClientError(), ("protocol", Verdict.ERROR, None)),
        (ValueError("not a request failure"), None),
    ],
)
def test_request_failures_are_classified_by_type(error, expected):
    assert _classify(error) == expected
