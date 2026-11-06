"""Byte counting: the wire estimate per proxy and target, Linux TCP_INFO parsing, the sized
estimate, and the plaintext estimate against a metering relay."""

import asyncio
import socket
import ssl
import struct
from functools import partial

import pytest
import trustme
from helpers import Gateway, server

from proxy_index import JsonSink, ProxyConfig, RunConfig, RunOptions, bytes_io, run
from proxy_index.targets import EchoIpTarget, TargetRegistry, registry
from proxy_index.transport import HttpTransport


async def test_plain_http_estimate_matches_the_wire():
    gateway = Gateway(auth=True)
    async with server(gateway) as port:
        proxy = ProxyConfig("http", "127.0.0.1", port, "test", "secret")
        async with HttpTransport(RunConfig("generic_http", proxy)) as transport:
            outcome = await transport.execute(registry.get("generic_http"))
    assert outcome.verdict == "ok"
    sent = (
        b"HTTP/1.1 200 Test\r\nContent-Length: 12\r\nLocation: http://localhost/must-not-"
        b"follow\r\nConnection: close\r\n\r\n203.0.113.8\n"
    )
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
        targets.register(
            EchoIpTarget(name, f"{'https' if tls else 'http'}://localhost:{origin_port}/", not tls)
        )
        proxy = ProxyConfig(scheme, "127.0.0.1", port, "test", "secret")
        result = await run(
            RunConfig(name, proxy, requests=2),
            JsonSink(tmp_path),
            RunOptions(
                targets=targets, transport_factory=partial(HttpTransport, ssl_context=tls_client)
            ),
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
    # Linux UAPI: receive window/retransmits and pacing rates are not byte totals.
    struct.pack_into("=Q", raw, 96, 14480)
    struct.pack_into("=Q", raw, 104, 900_000_000)
    struct.pack_into("=Q", raw, 112, 2**64 - 1)
    struct.pack_into("=Q", raw, 120, 1201)
    struct.pack_into("=Q", raw, 128, 5300)
    monkeypatch.setattr(bytes_io, "LINUX", True)
    assert bytes_io.from_socket(FakeSocket(bytes(raw))) == bytes_io.ByteCount(1200, 5300, True)
    assert bytes_io.from_socket(FakeSocket(error=OSError())) is None
    assert bytes_io.from_socket(FakeSocket(b"short")) is None
    assert bytes_io.from_socket(FakeSocket(bytes(raw[:135]))) is None
    assert bytes_io.from_socket(FakeSocket(bytes(raw[:136]))) == bytes_io.ByteCount(
        1200,
        5300,
        True,
    )
    assert bytes_io.from_socket(None) is None
    monkeypatch.setattr(bytes_io, "LINUX", False)
    assert bytes_io.from_socket(FakeSocket(bytes(raw))) is None


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (1, (1200, 5300)),  # ESTABLISHED
        (4, (1200, 5300)),  # FIN_WAIT1: our FIN is not acknowledged yet
        (5, (1199, 5300)),  # FIN_WAIT2: our FIN is acknowledged
        (8, (1200, 5299)),  # CLOSE_WAIT: the gateway's FIN arrived
        (9, (1200, 5299)),  # LAST_ACK
        (11, (1200, 5299)),  # CLOSING
        (6, (1199, 5299)),  # TIME_WAIT: both FINs
    ],
)
def test_tcp_info_excludes_fin_sequence_numbers(monkeypatch, state, expected):
    # Linux advances both byte counters by one for a FIN, like the SYN; a
    # gateway that closes after its response must not bill a phantom byte.
    raw = bytearray(232)
    raw[0] = state
    struct.pack_into("=Q", raw, 120, 1201)
    struct.pack_into("=Q", raw, 128, 5300)
    monkeypatch.setattr(bytes_io, "LINUX", True)
    assert bytes_io.from_socket(FakeSocket(bytes(raw))) == bytes_io.ByteCount(*expected, True)


@pytest.mark.skipif(not bytes_io.LINUX, reason="Linux TCP_INFO integration")
def test_tcp_info_counts_real_loopback_payload():
    # Exercise the kernel ABI, independently of the synthetic struct fixture.
    with socket.socket() as listener, socket.socket() as client:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client.settimeout(2)
        client.connect(listener.getsockname())
        peer, _ = listener.accept()
        with peer:
            peer.settimeout(2)
            before = bytes_io.from_socket(client)
            assert before is not None and before.exact
            client.sendall(b"request" * 173)
            remaining = 7 * 173
            while remaining:
                received = peer.recv(remaining)
                assert received
                remaining -= len(received)
            peer.sendall(b"response" * 257)
            remaining = 8 * 257
            while remaining:
                received = client.recv(remaining)
                assert received
                remaining -= len(received)
            after = bytes_io.from_socket(client)
            assert after is not None and after.exact
            assert after.up - before.up == 7 * 173
            assert after.down - before.down == 8 * 257


def test_estimate_and_sizes():
    plain = bytes_io.estimate(
        request_line=10, request_headers=20, request_body=5, response_headers=30, response_body=8
    )
    assert plain == bytes_io.ByteCount(35, 38, exact=False)
    tunnelled = bytes_io.estimate(
        request_line=10,
        request_headers=20,
        request_body=5,
        response_headers=30,
        response_body=8,
        tls=True,
    )
    assert tunnelled.up == 35 + bytes_io.CONNECT_REQUEST_BYTES + bytes_io.TLS_HANDSHAKE_UP_BYTES
    socks = bytes_io.estimate(
        request_line=10,
        request_headers=20,
        request_body=5,
        response_headers=30,
        response_body=8,
        tls=True,
        connect=False,
        extra_up=7,
        extra_down=3,
    )
    assert socks.up == 42 + bytes_io.TLS_HANDSHAKE_UP_BYTES
    assert socks.down == 41 + bytes_io.TLS_HANDSHAKE_DOWN_BYTES
    resumed = bytes_io.estimate(
        request_line=0,
        request_headers=0,
        request_body=0,
        response_headers=0,
        response_body=0,
        tls=True,
        tls_resumed=True,
    )
    assert resumed.down == bytes_io.CONNECT_RESPONSE_BYTES + bytes_io.TLS_RESUMED_DOWN_BYTES
    # greeting 4 + auth 3+4+6 + connect 4+1+9+2 up; 2 + 2 + 10 down.
    assert bytes_io.socks5_handshake(username="test", password="secret", host="localhost") == (
        33,
        14,
    )
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

        await asyncio.gather(
            copy(reader, remote_writer, "up"),
            copy(remote_reader, writer, "down"),
            return_exceptions=True,
        )


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
