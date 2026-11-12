"""BYTES-TRUTH-1, HTTP: a try that fails before any response counts what crossed the gateway
link. Exact on Linux from the gateway socket's TCP_INFO (equal to the fake gateway's own count);
elsewhere only the CONNECT exchange that surely crossed."""

import asyncio
import socket

import pytest
from helpers import Gateway, Network, server

from proxy_index import ProxyConfig, RunConfig, bytes_io
from proxy_index.bytes_io import ByteCount
from proxy_index.targets import EchoIpTarget
from proxy_index.transport import HttpTransport, _failed_count

REFUSED = b"HTTP/1.1 407 Nope\r\nProxy-Authenticate: Basic\r\nContent-Length: 0\r\n\r\n"
ESTABLISHED = b"HTTP/1.1 200 Established\r\n\r\n"
ESTIMATE = (bytes_io.CONNECT_REQUEST_BYTES, bytes_io.CONNECT_RESPONSE_BYTES)


class Metered:
    """A gateway that reads one CONNECT and answers 407, or (`stall`) 200 and then reads
    whatever follows without ever answering; it counts what it read and wrote."""

    def __init__(self, *, stall: bool = False) -> None:
        self.stall = stall
        self.read = self.written = 0

    async def __call__(self, reader, writer):
        self.read += len(await reader.readuntil(b"\r\n\r\n"))
        answer = ESTABLISHED if self.stall else REFUSED
        writer.write(answer)
        await writer.drain()
        self.written += len(answer)
        while self.stall and (data := await reader.read(65536)):
            self.read += len(data)


async def attempt(gateway, url="https://localhost:9/", seconds=1.0):
    async with server(gateway) as port:
        proxy = ProxyConfig("http", "127.0.0.1", port, "test", "secret")
        config = RunConfig("generic_https", proxy, timeout_seconds=seconds)
        async with HttpTransport(config) as transport:
            outcome = await transport.execute(EchoIpTarget("generic_https", url, False))
        # The gateway's handler has read everything the client sent before it closed.
        await asyncio.sleep(0.05)
    return outcome


@pytest.mark.parametrize("linux", [True, False])
async def test_a_refused_connect_counts_its_exchange(monkeypatch, linux):
    if linux and not bytes_io.LINUX:
        pytest.skip("TCP_INFO is Linux only")
    monkeypatch.setattr(bytes_io, "LINUX", linux)
    gateway = Metered()
    outcome = await attempt(gateway)
    assert (outcome.error, outcome.status) == ("proxy_auth", 407)
    assert outcome.bytes_up > 0 and outcome.bytes_exact is linux
    if linux:
        assert (outcome.bytes_up, outcome.bytes_down) == (gateway.read, gateway.written)
    else:
        assert (outcome.bytes_up, outcome.bytes_down) == ESTIMATE


@pytest.mark.parametrize("linux", [True, False])
async def test_a_tunnel_that_stalls_before_headers_counts_what_crossed(monkeypatch, linux):
    """CONNECT accepted, then nothing: the try times out in the TLS handshake. Linux counts the
    CONNECT and the ClientHello; the estimate claims only the CONNECT request, which surely
    crossed, and no handshake."""
    if linux and not bytes_io.LINUX:
        pytest.skip("TCP_INFO is Linux only")
    monkeypatch.setattr(bytes_io, "LINUX", linux)
    gateway = Metered(stall=True)
    outcome = await attempt(gateway, seconds=0.5)
    assert outcome.error == "timeout" and outcome.status is None
    assert outcome.bytes_up > 0 and outcome.bytes_exact is linux
    if linux:
        assert (outcome.bytes_up, outcome.bytes_down) == (gateway.read, gateway.written)
        assert gateway.read > len(b"CONNECT localhost:9 HTTP/1.1\r\n\r\n")
    else:
        assert (outcome.bytes_up, outcome.bytes_down) == (bytes_io.CONNECT_REQUEST_BYTES, 0)


async def test_a_refused_tcp_connection_counts_nothing():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    proxy = ProxyConfig("http", "127.0.0.1", port, "test", "secret")
    async with HttpTransport(RunConfig("generic_https", proxy, timeout_seconds=1)) as transport:
        outcome = await transport.execute(EchoIpTarget("generic_https", "https://localhost:9/"))
    assert outcome.error == "proxy_connect"
    assert (outcome.bytes_up, outcome.bytes_down) == (0, 0)
    assert outcome.bytes_exact is bytes_io.LINUX


async def test_a_plain_http_read_timeout_estimates_no_connect(monkeypatch):
    """No CONNECT on plain HTTP: without a kernel count nothing is claimed."""
    monkeypatch.setattr(bytes_io, "LINUX", False)
    async with server(Gateway(delay=5)) as port:
        proxy = ProxyConfig("http", "127.0.0.1", port, "test", "secret")
        async with HttpTransport(RunConfig("generic_http", proxy, timeout_seconds=0.3)) as t:
            outcome = await t.execute(EchoIpTarget("generic_http", "http://localhost:9/"))
    assert outcome.error == "timeout"
    assert (outcome.bytes_up, outcome.bytes_down, outcome.bytes_exact) == (0, 0, False)


def test_a_gateway_socket_keeps_its_count_when_closed(monkeypatch):
    counts = iter([ByteCount(10, 20), ByteCount(11, 21)])
    monkeypatch.setattr(bytes_io, "from_socket", lambda sock: next(counts))
    sock = bytes_io.GatewaySocket(socket.AF_INET, socket.SOCK_STREAM)
    assert sock.final() == (ByteCount(10, 20), False)
    sock.close()
    sock.close()
    assert sock.final() == (ByteCount(11, 21), False)


class Tunnel:
    """A SOCKS tunnel's transport socket, already closed: no count."""

    def getsockopt(self, *args):
        raise OSError("closed")


class Closed(bytes_io.GatewaySocket):
    def __init__(self, count, reached):
        super().__init__()
        self._count, self._reached = count, reached

    def final(self):
        return self._count, self._reached


@pytest.mark.parametrize(
    ("sockets", "connect", "answered", "tunnel", "expected"),
    [
        ([], True, True, False, ByteCount(0, 0, False)),
        ([(None, False)], True, False, False, ByteCount(0, 0, False)),
        ([(None, True)], True, False, False, ByteCount(ESTIMATE[0], 0, False)),
        ([(None, True)], True, True, False, ByteCount(*ESTIMATE, False)),
        # The tunnel was made: the proxy answered the CONNECT.
        ([(None, True)], True, False, True, ByteCount(*ESTIMATE, False)),
        ([(None, True)], False, True, False, ByteCount(0, 0, False)),
        ([(ByteCount(5, 7), True), (ByteCount(0, 0), False)], True, False, False, ByteCount(5, 7)),
        # A SOCKS5 tunnel socket with no count left.
        ([], False, False, True, ByteCount(0, 0, False)),
    ],
)
def test_the_failed_count_rules(sockets, connect, answered, tunnel, expected):
    slot = [Closed(*each) for each in sockets]
    try:
        assert _failed_count([*slot, *[Tunnel()] * tunnel], connect, answered) == expected
    finally:
        for sock in slot:
            sock.close()


def test_a_socks_tunnel_is_read_only_without_a_gateway_socket(monkeypatch):
    monkeypatch.setattr(bytes_io, "from_socket", lambda sock: ByteCount(3, 4))
    assert _failed_count([Tunnel()], False, False) == ByteCount(3, 4)


async def test_a_per_request_socks_session_counts_a_read_timeout_after_its_tunnel():
    """A dynamic SOCKS5 source opens a session per request and closes it after the try: the
    tunnel's count is read before it closes. The origin reads the request and never answers."""
    if not bytes_io.LINUX:
        pytest.skip("TCP_INFO is Linux only")
    received = []

    async def silent(reader, writer):
        received.append(await reader.readuntil(b"\r\n\r\n"))
        await asyncio.sleep(5)

    async with server(silent) as origin, server(Gateway("socks5")) as port:
        proxy = Network(ProxyConfig("socks5", "127.0.0.1", port), "default")
        async with HttpTransport(RunConfig("generic_http", proxy, timeout_seconds=0.5)) as t:
            outcome = await t.execute(EchoIpTarget("generic_http", f"http://localhost:{origin}/"))
    assert outcome.error == "timeout" and outcome.bytes_exact is True
    # The SOCKS5 greeting and CONNECT, then the request itself.
    assert outcome.bytes_up > len(received[0]) and outcome.bytes_down > 0
