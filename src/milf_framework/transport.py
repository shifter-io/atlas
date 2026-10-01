"""HTTP and SOCKS5 adapters. Request failures expose codes, never credentials."""
from __future__ import annotations

import asyncio
import ssl
import time
import uuid
from contextvars import ContextVar
from typing import Any, Protocol

import aiohttp
from aiohttp_socks import (
    ProxyConnectionError,
    ProxyConnector,
    ProxyError,
    ProxyTimeoutError,
    ProxyType,
)

from . import bytes_io
from .bytes_io import ByteCount
from .models import Outcome, ProxyConfig, RequestContext, RunConfig
from .outcome import BODY_CODES, Verdict, failed_response, sniff_body
from .targets import RequestSpec, Target

# The socket of the connection serving the current request. A response whose
# body arrives with its headers releases (and, with force_close, closes) its
# connection before `session.request` returns, so the connector records the
# socket the moment the connection is made. Each request runs in its own task
# context, so concurrent requests on one session never see each other's slot.
_SOCKET: ContextVar[list[Any] | None] = ContextVar("milf_request_socket", default=None)


class _SocketTap:
    async def connect(self, req, traces, timeout):  # noqa: ASYNC109 - aiohttp signature
        connection = await super().connect(req, traces, timeout)  # type: ignore[misc]
        slot = _SOCKET.get()
        if slot is not None and connection.transport is not None:
            # The TLS transport, including a CONNECT or SOCKS tunnel, exposes
            # the raw TCP socket to the gateway, which is what is metered.
            slot.append(connection.transport.get_extra_info("socket"))
        return connection


class _TapTCPConnector(_SocketTap, aiohttp.TCPConnector):
    pass


class _TapProxyConnector(_SocketTap, ProxyConnector):
    pass


class Transport(Protocol):
    async def __aenter__(self) -> Transport: ...
    async def __aexit__(self, *args: object) -> None: ...
    async def execute(
        self, target: Target, *, context: RequestContext | None = None,
    ) -> Outcome: ...


class HttpTransport:
    def __init__(self, config: RunConfig, *, ssl_context: ssl.SSLContext | None = None):
        self.config = config
        self.ssl_context = ssl_context
        self.session: aiohttp.ClientSession | None = None
        self._opened = False
        self._run_id = str(uuid.uuid4())
        self._request_index = 0

    async def __aenter__(self) -> HttpTransport:
        # HTTP auth is per request. A dynamic SOCKS source needs an isolated
        # connector per request, closed immediately, never an unbounded SID cache.
        if isinstance(self.config.proxy, ProxyConfig) or self.config.proxy.scheme == "http":
            self.session = self._make_session(
                self.config.proxy if isinstance(self.config.proxy, ProxyConfig) else None
            )
        self._opened = True
        return self

    def _make_session(self, proxy: ProxyConfig | None) -> aiohttp.ClientSession:
        options = {"limit": self.config.concurrency, "force_close": True}
        if self.ssl_context is not None:
            options["ssl"] = self.ssl_context
        if proxy is not None and proxy.scheme == "socks5":
            connector = _TapProxyConnector(
                proxy_type=ProxyType.SOCKS5, host=proxy.host, port=proxy.port,
                username=proxy.username, password=proxy.password, rdns=True, **options,
            )
        else:
            connector = _TapTCPConnector(**options)
        session = aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(
                total=self.config.timeout_seconds, ceil_threshold=float("inf"),
            ),
            cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False, trust_env=False,
            skip_auto_headers={"User-Agent", "Accept", "Accept-Encoding"},
        )
        # aiohttp retries idempotent requests once on disconnect, even on fresh
        # connections. No public switch exists in 3.x; keep the shim here and
        # cover a disconnect with an integration test counting gateway accepts.
        session._retry_connection = False
        return session

    async def __aexit__(self, *args: object) -> None:
        if self.session:
            await self.session.close()
        self.session = None
        self._opened = False

    async def execute(self, target: Target, *, context: RequestContext | None = None) -> Outcome:
        if not self._opened:
            raise RuntimeError("Transport must be used as an async context manager")
        spec = target.build_request()
        if context is None:
            context = RequestContext(self._run_id, self._request_index, 0)
            self._request_index += 1
        proxy = self.config.proxy.resolve(context)
        if proxy.scheme != self.config.proxy.scheme:
            raise ValueError("Provider transport scheme cannot change within a run")
        kwargs = {"headers": dict(spec.headers)}
        if proxy.scheme == "http":
            host = f"[{proxy.host}]" if ":" in proxy.host else proxy.host
            kwargs["proxy"] = f"http://{host}:{proxy.port}"
            if proxy.username is not None:
                # Plain HTTP goes to the gateway in absolute-URI form. For
                # HTTPS the credentials belong on CONNECT only, never TLS traffic.
                header_key = "proxy_headers" if spec.url.startswith("https:") else "headers"
                auth_header = aiohttp.encode_basic_auth(proxy.username, proxy.password)
                kwargs.setdefault(header_key, {})["Proxy-Authorization"] = auth_header
        raw = bytearray()
        status = None
        error = None
        exit_ip = None
        verdict: str | None = None
        counted: ByteCount | None = None
        tls = spec.url.startswith("https:")
        slot: list[Any] = []
        token = _SOCKET.set(slot)
        session = self.session if self.session is not None else self._make_session(proxy)
        started = time.perf_counter()
        try:
            # Outer timeout also bounds SOCKS negotiation and all body reads.
            async with asyncio.timeout(self.config.timeout_seconds):
                async with session.request(
                    spec.method, spec.url, data=spec.body, allow_redirects=False, **kwargs,
                ) as response:
                    status = response.status
                    sock = slot[-1] if slot else None

                    def measure() -> ByteCount:
                        # Kernel counters are read before any await can let the
                        # connector close the socket; the estimate is the fallback.
                        exact = bytes_io.from_socket(sock)
                        if exact is not None:
                            return exact
                        return _estimate(spec, response, proxy, len(raw), tls)

                    counted = measure()
                    # read(n) may return a partial chunk; continue through EOF.
                    while len(raw) <= spec.max_body_bytes:
                        part = await response.content.read(spec.max_body_bytes + 1 - len(raw))
                        counted = measure()
                        if not part:
                            break
                        raw.extend(part)
                    if len(raw) > spec.max_body_bytes:
                        # Too large to assess, but a challenge page announces
                        # itself in its first few hundred bytes.
                        sniffed = sniff_body(bytes(raw)) if status == 200 else None
                        error = BODY_CODES[sniffed] if sniffed else "body_too_large"
                        verdict = sniffed or failed_response(status, bytes(raw))
                    else:
                        assessment = target.assess(status, bytes(raw))
                        error, exit_ip = assessment.error, assessment.ip
                        verdict = assessment.verdict
                        if verdict is None:
                            verdict = (Verdict.OK if error is None
                                       else failed_response(status, bytes(raw)))
        except (TimeoutError, ProxyTimeoutError):
            error, verdict = "timeout", Verdict.TIMEOUT
        except aiohttp.ClientHttpProxyError as exc:
            # How a CONNECT-tunnelled request surfaces a gateway status.
            status = exc.status
            error = "proxy_auth" if exc.status == 407 else "proxy_connect"
            verdict = Verdict.PROXY_ERROR
        except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError, ssl.SSLError):
            error, verdict = "tls", Verdict.ERROR
        except aiohttp.ClientConnectorDNSError:
            error, verdict = "dns", Verdict.ERROR
        except (aiohttp.ClientProxyConnectionError, ProxyConnectionError):
            error, verdict = "proxy_connect", Verdict.PROXY_ERROR
        except ProxyError as exc:
            error = "proxy_auth" if "authentication" in str(exc).lower() else "proxy_connect"
            verdict = Verdict.PROXY_ERROR
        except aiohttp.ClientPayloadError:
            error, verdict = "protocol", Verdict.ERROR
        except (aiohttp.ClientConnectionError, OSError) as exc:
            # aiohttp wraps raw SOCKS TLS failures in ClientOSError. Classify
            # by the exception chain, without retaining/publishing its text.
            cause = exc
            error, verdict = "connection", Verdict.ERROR
            while cause is not None:
                if isinstance(cause, ssl.SSLError):
                    error = "tls"
                    break
                cause = cause.__cause__
        except aiohttp.ClientError:
            error, verdict = "protocol", Verdict.ERROR
        finally:
            latency_ms = (time.perf_counter() - started) * 1000
            _SOCKET.reset(token)
            if session is not self.session:
                await session.close()
        if counted is None:
            # No response arrived; nothing countable was received.
            counted = ByteCount(0, 0, exact=False)
        return Outcome(
            latency_ms=latency_ms,
            ip=exit_ip, error=error, status=status, response_body_bytes=len(raw),
            verdict=verdict, bytes_up=counted.up, bytes_down=counted.down,
            bytes_exact=counted.exact,
        )


def _estimate(
    spec: RequestSpec, response: aiohttp.ClientResponse, proxy: ProxyConfig, body: int, tls: bool,
) -> ByteCount:
    """Wire-size estimate from what was actually sent and received."""
    info = response.request_info
    url = info.url
    # A plaintext request through an HTTP gateway carries the absolute URI;
    # a tunnelled or SOCKS request carries only the path.
    absolute = proxy.scheme == "http" and not tls
    target = str(url) if absolute else (url.raw_path_qs or "/")
    request_line = len(info.method) + 1 + len(target) + len(" HTTP/1.1\r\n")
    version = response.version
    status_line = len(
        f"HTTP/{version.major}.{version.minor} {response.status} {response.reason or ''}\r\n"
        if version is not None else f"HTTP/1.1 {response.status} {response.reason or ''}\r\n"
    )
    extra_up = extra_down = 0
    if proxy.scheme == "socks5":
        extra_up, extra_down = bytes_io.socks5_handshake(
            username=proxy.username, password=proxy.password, host=url.host or "",
        )
    elif tls and proxy.username is not None:
        # The credential rides on the CONNECT, outside the request headers.
        auth = aiohttp.encode_basic_auth(proxy.username, proxy.password)
        extra_up = len("Proxy-Authorization: ") + len(auth) + 2
    return bytes_io.estimate(
        request_line=request_line,
        request_headers=bytes_io.header_bytes(info.headers),
        request_body=len(spec.body or b""),
        response_headers=status_line + bytes_io.header_bytes(response.raw_headers),
        response_body=body,
        tls=tls,
        connect=tls and proxy.scheme == "http",
        extra_up=extra_up,
        extra_down=extra_down,
    )
