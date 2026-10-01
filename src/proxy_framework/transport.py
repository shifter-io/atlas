"""HTTP and SOCKS5 adapters. Request failures expose codes, never credentials."""
from __future__ import annotations

import asyncio
import ssl
import time
import uuid
from typing import Protocol

import aiohttp
from aiohttp_socks import (
    ProxyConnectionError,
    ProxyConnector,
    ProxyError,
    ProxyTimeoutError,
    ProxyType,
)

from .models import Outcome, ProxyConfig, RequestContext, RunConfig
from .targets import Target


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
            connector = ProxyConnector(
                proxy_type=ProxyType.SOCKS5, host=proxy.host, port=proxy.port,
                username=proxy.username, password=proxy.password, rdns=True, **options,
            )
        else:
            connector = aiohttp.TCPConnector(**options)
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
        session = self.session if self.session is not None else self._make_session(proxy)
        started = time.perf_counter()
        try:
            # Outer timeout also bounds SOCKS negotiation and all body reads.
            async with asyncio.timeout(self.config.timeout_seconds):
                async with session.request(
                    spec.method, spec.url, data=spec.body, allow_redirects=False, **kwargs,
                ) as response:
                    status = response.status
                    # read(n) may return a partial chunk; continue through EOF.
                    while len(raw) <= spec.max_body_bytes:
                        part = await response.content.read(spec.max_body_bytes + 1 - len(raw))
                        if not part:
                            break
                        raw.extend(part)
                    if len(raw) > spec.max_body_bytes:
                        error = "body_too_large"
                    else:
                        assessment = target.assess(status, bytes(raw))
                        error, exit_ip = assessment.error, assessment.ip
        except (TimeoutError, ProxyTimeoutError):
            error = "timeout"
        except aiohttp.ClientHttpProxyError as exc:
            status = exc.status
            error = "proxy_auth" if exc.status == 407 else "proxy_connect"
        except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError, ssl.SSLError):
            error = "tls"
        except aiohttp.ClientConnectorDNSError:
            error = "dns"
        except (aiohttp.ClientProxyConnectionError, ProxyConnectionError):
            error = "proxy_connect"
        except ProxyError as exc:
            error = "proxy_auth" if "authentication" in str(exc).lower() else "proxy_connect"
        except aiohttp.ClientPayloadError:
            error = "protocol"
        except (aiohttp.ClientConnectionError, OSError) as exc:
            # aiohttp wraps raw SOCKS TLS failures in ClientOSError. Classify
            # by the exception chain, without retaining/publishing its text.
            cause = exc
            error = "connection"
            while cause is not None:
                if isinstance(cause, ssl.SSLError):
                    error = "tls"
                    break
                cause = cause.__cause__
        except aiohttp.ClientError:
            error = "protocol"
        finally:
            latency_ms = (time.perf_counter() - started) * 1000
            if session is not self.session:
                await session.close()
        return Outcome(
            latency_ms=latency_ms,
            ip=exit_ip, error=error, status=status, response_body_bytes=len(raw),
        )
