import asyncio
import ssl
from functools import partial

import pytest
import trustme
from helpers import Gateway, server

from milf_framework import JsonSink, ProxyConfig, RunConfig, run
from milf_framework.targets import Assessment, EchoIpTarget, RequestSpec, TargetRegistry
from milf_framework.transport import HttpTransport


def config(port, *, scheme="http", **kwargs):
    return RunConfig("generic_http", ProxyConfig(scheme, "127.0.0.1", port), **kwargs)


@pytest.mark.parametrize("status,body,error", [
    (200, b"8.8.8.8\n", None), (200, b"2001:4860:4860::8888\n", None),
    (200, b"8.8.8.8 garbage", "invalid_body"), (200, b"8.8.8.8" + b" " * 600, "body_too_large"),
    (302, b"", "http_status"), (403, b"denied", "http_status"),
    (429, b"limited", "http_status"), (500, b"bad", "http_status"),
    (403, b"x" * 1000, "body_too_large"),
    (407, b"auth", "proxy_auth"),
])
async def test_http_results(tmp_path, status, body, error):
    gateway = Gateway(status=status, body=body, fragmented=True)
    async with server(gateway) as port:
        result = await run(config(port, requests=2), JsonSink(tmp_path))
    assert result.errors == ({error: 2} if error else {})
    assert gateway.connections == 2
    assert result.successes == (0 if error else 2)
    if error:
        assert result.latency_ms["p50"] is None


async def test_fresh_connections_and_concurrency(tmp_path):
    gateway = Gateway(delay=.005)
    async with server(gateway) as port:
        result = await run(config(port, requests=60, concurrency=3), JsonSink(tmp_path))
    assert result.successes == 60
    assert gateway.connections == 60
    assert 1 < gateway.peak <= 3
    assert all(b"http://checkip.amazonaws.com/" in r for r in gateway.requests)


async def test_no_hidden_retry(tmp_path):
    gateway = Gateway(disconnect=True)
    async with server(gateway) as port:
        result = await run(config(port, requests=3), JsonSink(tmp_path))
    assert gateway.connections == 3
    assert result.errors == {"connection": 3}


async def test_timeout(tmp_path):
    gateway = Gateway(delay=.2)
    async with server(gateway) as port:
        result = await run(config(port, requests=2, timeout_seconds=.02), JsonSink(tmp_path))
    assert result.errors == {"timeout": 2}


@pytest.mark.parametrize("valid", [True, False])
async def test_http_auth(tmp_path, valid):
    gateway = Gateway(auth=True)
    async with server(gateway) as port:
        proxy = ProxyConfig("http", "127.0.0.1", port, "test", "secret" if valid else "bad")
        result = await run(RunConfig("generic_http", proxy, requests=2), JsonSink(tmp_path))
    assert result.errors == ({} if valid else {"proxy_auth": 2})


@pytest.mark.parametrize("scheme", ["http", "socks5"])
@pytest.mark.parametrize("trusted", [True, False])
async def test_tls_tunnel_and_verification(tmp_path, scheme, trusted):
    ca = trustme.CA()
    cert = ca.issue_cert("localhost")
    tls_server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cert.configure_cert(tls_server)
    tls_client = ssl.create_default_context()
    if trusted:
        ca.configure_trust(tls_client)
    origin_requests = []

    async def origin(reader, writer):
        origin_requests.append(await reader.readuntil(b"\r\n\r\n"))
        await Gateway().respond(writer)

    gateway = Gateway(scheme, auth=True)
    async with server(origin, tls=tls_server) as origin_port, server(gateway) as port:
        targets = TargetRegistry()
        targets.register(EchoIpTarget("generic_https", f"https://localhost:{origin_port}/"))
        proxy = ProxyConfig(scheme, "127.0.0.1", port, "test", "secret")
        result = await run(
            RunConfig("generic_https", proxy, requests=2), JsonSink(tmp_path), targets=targets,
            transport_factory=partial(HttpTransport, ssl_context=tls_client),
        )
    assert result.errors == ({} if trusted else {"tls": 2})
    # HTTPS records the exit IP like every echo target: one stub address, or none on TLS failure.
    assert result.unique_ips == (1 if trusted else 0)
    assert result.artifacts["ips"]["rows"] == (1 if trusted else 0)
    assert all(b"Proxy-Authorization" not in r and b"secret" not in r for r in origin_requests)
    if scheme == "socks5":
        assert gateway.domains == ["localhost", "localhost"]


@pytest.mark.parametrize("valid", [True, False])
async def test_socks_http_auth_and_remote_dns(tmp_path, valid):
    async def origin(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        assert b"Proxy-Authorization" not in request
        await Gateway().respond(writer)

    gateway = Gateway("socks5", auth=True)
    async with server(origin) as origin_port, server(gateway) as port:
        targets = TargetRegistry()
        targets.register(EchoIpTarget("generic_http", f"http://localhost:{origin_port}/", True))
        proxy = ProxyConfig("socks5", "127.0.0.1", port, "test", "secret" if valid else "bad")
        result = await run(RunConfig("generic_http", proxy, requests=3),
                           JsonSink(tmp_path), targets=targets)
    assert result.errors == ({} if valid else {"proxy_auth": 3})
    if valid:
        assert gateway.domains == ["localhost"] * 3


async def test_stop_in_flight(tmp_path):
    gateway = Gateway(delay=1)
    stop = asyncio.Event()
    async with server(gateway) as port:
        task = asyncio.create_task(run(config(port, requests=100, concurrency=3),
                                       JsonSink(tmp_path), stop=stop))
        await asyncio.sleep(.04)
        stop.set()
        result = await task
    assert result.status == "cancelled"
    assert result.started == result.cancelled == 3
    assert result.not_started == 97


async def test_socks_without_authentication(tmp_path):
    async def origin(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        await Gateway().respond(writer)

    gateway = Gateway("socks5")
    async with server(origin) as origin_port, server(gateway) as port:
        targets = TargetRegistry()
        targets.register(EchoIpTarget("generic_http", f"http://localhost:{origin_port}/", True))
        result = await run(config(port, scheme="socks5", requests=1),
                           JsonSink(tmp_path), targets=targets)
    assert result.successes == 1


async def test_custom_target_controls_method_headers_and_success(tmp_path):
    class Custom:
        id, version, engine, pool_eligible = "custom", "1", "http", False

        def build_request(self):
            return RequestSpec("http://example.test/", method="POST",
                               headers={"X-Test": "custom"}, body=b"hello")

        def assess(self, status, body):
            assert status == 201
            return Assessment(ip="8.8.8.8")

    targets = TargetRegistry()
    targets.register(Custom())
    gateway = Gateway(status=201, auth=True)
    async with server(gateway) as port:
        proxy = ProxyConfig("http", "127.0.0.1", port, "test", "secret")
        result = await run(RunConfig("custom", proxy, requests=1),
                           JsonSink(tmp_path), targets=targets)
    assert result.successes == 1
    assert b"POST http://example.test/" in gateway.requests[0]
    assert b"X-Test: custom" in gateway.requests[0]
