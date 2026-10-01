from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import unquote, urlsplit

ErrorCode = Literal[
    "proxy_auth", "proxy_connect", "dns", "tls", "timeout", "http_status",
    "invalid_body", "body_too_large", "connection", "protocol",
]


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Stable, zero-based request/worker identity within one runner invocation."""

    run_id: str
    request_index: int
    worker_index: int


class ProxySource(Protocol):
    """Provider-owned configuration; resolve without I/O or shared mutable state."""

    scheme: str

    def resolve(self, context: RequestContext) -> ProxyConfig: ...
    def public(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    scheme: Literal["http", "socks5"]
    host: str
    port: int
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.scheme not in ("http", "socks5") or not self.host:
            raise ValueError("Proxy requires an HTTP or SOCKS5 host")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Proxy port must be between 1 and 65535")
        if (self.username is None) != (self.password is None):
            raise ValueError("Provide both proxy username and password")
        if self.username is not None and ":" in self.username and self.scheme == "http":
            raise ValueError("HTTP proxy usernames cannot contain colons")

    def resolve(self, context: RequestContext) -> ProxyConfig:
        return self

    def public(self) -> dict[str, Any]:
        return {"proxy_protocol": self.scheme}

    @classmethod
    def from_url(cls, value: str) -> ProxyConfig:
        try:
            parsed = urlsplit(value)
            if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                raise ValueError
            if parsed.scheme not in ("http", "socks5", "socks5h"):
                raise ValueError
            return cls(
                scheme="http" if parsed.scheme == "http" else "socks5",
                host=parsed.hostname or "",
                port=parsed.port if parsed.port is not None else (
                    8080 if parsed.scheme == "http" else 1080
                ),
                username=unquote(parsed.username) if parsed.username is not None else None,
                password=unquote(parsed.password) if parsed.password is not None else None,
            )
        except (ValueError, TypeError):
            # Never echo a connection string or parser exception carrying credentials.
            raise ValueError("Invalid proxy URL; expected http:// or socks5:// gateway") from None


@dataclass(frozen=True, slots=True)
class RunConfig:
    target: str
    proxy: ProxySource = field(repr=False)
    requests: int = 100
    concurrency: int = 10
    timeout_seconds: float = 15.0
    requests_per_second: float | None = None
    provider: str | None = None
    product: str | None = None
    requested_country: str | None = None
    runner_location: str | None = None
    target_url: str | None = None

    def __post_init__(self) -> None:
        if self.target_url is not None:
            try:
                expected = {"generic_http": "http", "generic_https": "https"}[self.target]
                if not isinstance(self.target_url, str) or any(
                    char.isspace() or ord(char) < 32 for char in self.target_url
                ):
                    raise ValueError
                url = urlsplit(self.target_url)
                if (url.scheme != expected or not url.hostname or url.username is not None
                        or url.password is not None or url.fragment
                        or (url.port is not None and not 1 <= url.port <= 65535)):
                    raise ValueError
                object.__setattr__(self, "target_url", url.geturl())
            except (KeyError, ValueError):
                raise ValueError("Target URL must match the generic target protocol, "
                                 "without credentials or a fragment") from None
        metadata = self.proxy.public()
        for name in ("provider", "product", "requested_country"):
            effective = metadata.get(name)
            supplied = getattr(self, name)
            if supplied is not None and supplied != effective and name in metadata:
                raise ValueError(f"{name} label conflicts with provider configuration")
            if name in metadata:
                object.__setattr__(self, name, effective)
        for name in ("requests", "concurrency"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("timeout_seconds", "requests_per_second"):
            value = getattr(self, name)
            if value is None and name == "timeout_seconds":
                raise ValueError("timeout_seconds must be finite and positive")
            if value is not None and (
                type(value) not in (float, int) or not math.isfinite(value) or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")

    def public(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__ if name != "proxy"
        } | self.proxy.public()


@dataclass(frozen=True, slots=True)
class Outcome:
    latency_ms: float
    ip: str | None = None
    error: ErrorCode | None = None
    status: int | None = None
    response_body_bytes: int = 0


@dataclass(slots=True)
class RunSummary:
    run_id: str
    schema_version: str
    framework_version: str
    target: dict[str, Any]
    config: dict[str, Any]
    started_at: str
    finished_at: str
    duration_seconds: float
    status: str
    requested: int
    started: int
    completed: int
    cancelled: int
    not_started: int
    successes: int
    total_errors: int
    errors: dict[str, int]
    status_codes: dict[str, int]
    success_rate: float | None
    requests_per_second: float
    unique_ips: int | None
    pool_eligible: bool
    latency_ms: dict[str, float | None]
    latency_histogram: dict[str, Any]
    response_body_bytes: int
    buckets: list[dict[str, Any]]
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
