from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import socket
import ssl
import struct
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

# How GitHub Actions runs a `shell: bash` step.
BASH = ("bash", "--noprofile", "--norc", "-eo", "pipefail", "-c")
CI_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def workflow_step(name: str) -> str:
    """The run block of a ci.yml step, dedented as GitHub Actions runs it."""
    step = CI_WORKFLOW.read_text().split(f"- name: {name}", 1)[1].split("\n      - name:", 1)[0]
    block = step.split("run: |\n", 1)[1]
    return "".join(line[10:] if line.strip() else "\n" for line in block.splitlines(True))


def scratch_database_name() -> str:
    """A unique test database, `proxy_test_<xdist worker>_<uuid hex>` (`main` without xdist)."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    return f"proxy_test_{worker}_{uuid.uuid4().hex}"


@asynccontextmanager
async def server(handler, *, tls: ssl.SSLContext | None = None) -> AsyncIterator[int]:
    tasks = set()

    async def wrapped(reader, writer):
        tasks.add(asyncio.current_task())
        try:
            await handler(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, ssl.SSLError):
                await writer.wait_closed()
            tasks.discard(asyncio.current_task())

    listener = await asyncio.start_server(wrapped, "127.0.0.1", 0, ssl=tls)
    try:
        yield listener.sockets[0].getsockname()[1]
    finally:
        listener.close()
        await listener.wait_closed()
        remaining = list(tasks)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


class Gateway:
    """Minimal loopback gateways: no public hosts or credentials are contacted."""

    def __init__(
        self,
        scheme="http",
        *,
        auth=False,
        body=b"203.0.113.8\n",
        status=200,
        disconnect=False,
        delay=0,
        fragmented=False,
    ):
        self.scheme = scheme
        self.auth = auth
        self.body = body
        self.status = status
        self.disconnect = disconnect
        self.delay = delay
        self.fragmented = fragmented
        self.requests = []
        self.connections = 0
        self.active = 0
        self.peak = 0
        self.domains = []
        self.credentials = []
        self.accepted = asyncio.Event()

    async def __call__(self, reader, writer):
        self.connections += 1
        self.accepted.set()
        self.active += 1
        self.peak = max(self.active, self.peak)
        try:
            if self.disconnect:
                return
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.scheme == "socks5":
                await self.socks(reader, writer)
                return
            request = await reader.readuntil(b"\r\n\r\n")
            self.requests.append(request)
            auth_line = next(
                (
                    line
                    for line in request.split(b"\r\n")
                    if line.startswith(b"Proxy-Authorization: Basic ")
                ),
                None,
            )
            credentials = (None, None)
            if auth_line:
                credentials = tuple(base64.b64decode(auth_line.split()[-1]).decode().split(":", 1))
                self.credentials.append(credentials)
            if self.auth and not self.valid_auth(*credentials):
                writer.write(b"HTTP/1.1 407 Nope\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            method, endpoint, _ = request.split(b"\r\n", 1)[0].split(b" ")
            if method == b"CONNECT":
                host, port = endpoint.decode().rsplit(":", 1)
                assert host in ("localhost", "127.0.0.1")
                remote_reader, remote_writer = await asyncio.open_connection("127.0.0.1", int(port))
                writer.write(b"HTTP/1.1 200 Established\r\n\r\n")
                await writer.drain()
                await asyncio.gather(pipe(reader, remote_writer), pipe(remote_reader, writer))
                return
            await self.respond(writer)
        finally:
            self.active -= 1

    async def respond(self, writer):
        headers = (
            f"HTTP/1.1 {self.status} Test\r\nContent-Length: {len(self.body)}\r\n"
            "Location: http://localhost/must-not-follow\r\nConnection: close\r\n\r\n"
        ).encode()
        writer.write(headers)
        if self.fragmented:
            writer.write(self.body[:7])
            await writer.drain()
            await asyncio.sleep(0.01)
            writer.write(self.body[7:])
        else:
            writer.write(self.body)
        await writer.drain()

    def valid_auth(self, username, password):
        if callable(self.auth):
            return self.auth(username, password)
        return username == "test" and password == "secret"

    async def socks(self, reader, writer):
        version, nmethods = await reader.readexactly(2)
        assert version == 5
        await reader.readexactly(nmethods)
        writer.write(bytes([5, 2 if self.auth else 0]))
        await writer.drain()
        if self.auth:
            version, length = await reader.readexactly(2)
            username = await reader.readexactly(length)
            length = (await reader.readexactly(1))[0]
            password = await reader.readexactly(length)
            self.credentials.append((username.decode(), password.decode()))
            valid = self.valid_auth(username.decode(), password.decode())
            writer.write(bytes([1, 0 if valid else 1]))
            await writer.drain()
            if not valid:
                return
        version, command, _, atyp = await reader.readexactly(4)
        assert version == 5 and command == 1
        if atyp == 3:
            length = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(length)).decode()
            self.domains.append(host)
        else:
            size = 4 if atyp == 1 else 16
            host = socket.inet_ntop(
                socket.AF_INET if atyp == 1 else socket.AF_INET6, await reader.readexactly(size)
            )
        port = struct.unpack("!H", await reader.readexactly(2))[0]
        assert host in ("localhost", "127.0.0.1")
        remote_reader, remote_writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
        await writer.drain()
        await asyncio.gather(pipe(reader, remote_writer), pipe(remote_reader, writer))


class FakeTransport:
    def __init__(self, config):
        self.count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, target, *, context=None):
        from proxy_index.models import Outcome

        self.count += 1
        return Outcome(10.1, ip="8.8.8.8", status=200, response_body_bytes=8)


class Network:
    """A loopback proxy source labelled with a network test protocol (models.NetworkProtocol)."""

    def __init__(self, proxy, protocol):
        self.proxy = proxy
        self.protocol = protocol
        self.scheme = proxy.scheme

    def resolve(self, context):
        return self.proxy

    def public(self):
        return {"proxy_protocol": self.scheme, "test_protocol": self.protocol}


def self_signed(directory: Path, name: str, address: str = "127.0.0.1") -> tuple[Path, Path]:
    """A self-signed EC P-256 certificate for the IP `address`, made as the manager makes its
    own (`openssl req -x509`: CN proxy-index-orchestrator, CA:TRUE, subjectAltName IP), and
    its owner-only key; returns their paths."""
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "proxy-index-orchestrator")])
    own = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]), False
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .add_extension(own, False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(own), False)
        .sign(key, hashes.SHA256())
    )
    cert, private = directory / f"{name}.crt", directory / f"{name}.key"
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private.chmod(0o600)
    return cert, private
