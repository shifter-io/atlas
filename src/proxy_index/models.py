from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import unquote, urlsplit

from .outcome import from_error

ErrorCode = Literal[
    "proxy_auth", "proxy_connect", "dns", "tls", "timeout", "http_status",
    "invalid_body", "body_too_large", "connection", "protocol",
    # A 200 whose body is a challenge or block page (see outcome.sniff_body).
    "challenge", "blocked",
]
VerdictName = Literal["ok", "challenge", "block", "timeout", "proxy_error", "error"]
VERDICTS = ("ok", "challenge", "block", "timeout", "proxy_error", "error")
StopRule = Literal["count", "unique_ratio"]
# How a network runs the echo test (provider grammar [testing] protocol). 'default' sends
# each target's own scheme; 'https-only' upgrades plain-HTTP echo targets to HTTPS before
# any request (the gateway answers plain HTTP with a 301); 'mixed' sends HTTP and follows
# one gateway redirect to the same URL over HTTPS, counting it as a redirect.
NetworkProtocol = Literal["default", "https-only", "mixed"]
NETWORK_PROTOCOLS: tuple[NetworkProtocol, ...] = ("default", "https-only", "mixed")


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


def protocol_of(proxy: ProxySource) -> NetworkProtocol:
    """The network's test protocol, published by its provider adapter (default otherwise)."""
    protocol = proxy.public().get("test_protocol", "default")
    if protocol not in NETWORK_PROTOCOLS:
        raise ValueError("Unknown network test protocol")
    return protocol


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


# Written to a run's config only for non-count runs (RunConfig.public).
STOP_RULE_FIELDS = ("stop_rule", "min_sample", "stall_seconds")
CENSUS_RULE_VERSION = 2
CENSUS_RETRY_469_RULE_VERSION = 3
CENSUS_ANSWER_CLASSES_RULE_VERSION = 4


def census_rule(retry_http_469: bool, answer_classes: bool) -> tuple[int, str]:
    """The census rule version and the basis of its unique-IP ratio."""
    if answer_classes:
        return CENSUS_ANSWER_CLASSES_RULE_VERSION, "completed_requests_excluding_rate_limits"
    if retry_http_469:
        return CENSUS_RETRY_469_RULE_VERSION, "completed_requests_excluding_http_469"
    return CENSUS_RULE_VERSION, "completed_requests"


@dataclass(frozen=True, slots=True)
class AnswerClasses:
    """A network's answer classes (see answers.py and docs/onboarding.html#answer-classes).

    ``rate_limits`` holds 3-digit HTTP statuses (400-599) and optionally ``"proxy_connect"``
    (gateway connection refusals, answers.REFUSALS); ``no_inventory`` holds the statuses a
    network sends when it has no exits in the requested country. No duplicates, the sets are
    disjoint, and 429 is never a no-IP status.
    """

    rate_limits: tuple[str, ...]
    no_inventory: tuple[str, ...]

    def __post_init__(self) -> None:
        status = re.compile(r"[45][0-9][0-9]")
        for name in ("rate_limits", "no_inventory"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or len(set(values)) != len(values) or any(
                    not isinstance(value, str) or (
                        not status.fullmatch(value)
                        and not (name == "rate_limits" and value == "proxy_connect"))
                    for value in values):
                raise ValueError(f"Answer class {name} must hold distinct HTTP statuses 400-599")
        if set(self.rate_limits) & set(self.no_inventory) or "429" in self.no_inventory:
            raise ValueError("Rate-limit and no-IP statuses must be disjoint (429 is a rate limit)")

    def public(self) -> dict[str, list[str]]:
        return {"rate_limits": list(self.rate_limits), "no_inventory": list(self.no_inventory)}


@dataclass(frozen=True, slots=True)
class RunConfig:
    target: str
    proxy: ProxySource = field(repr=False)
    # None is unbounded and requires a stop rule that ends the run by itself.
    requests: int | None = 100
    concurrency: int = 10
    # 0 selects available CPU cores; concurrency is the total across processes.
    processes: int = 0
    timeout_seconds: float = 15.0
    requests_per_second: float | None = None
    provider: str | None = None
    product: str | None = None
    requested_country: str | None = None
    runner_location: str | None = None
    target_url: str | None = None
    # 'count' stops at `requests`; 'unique_ratio' stops issuing new requests
    # once completed requests > 2 * unique_ips, evaluated from `min_sample`
    # completions on (including failures), or after no success in the first 5,000 completions or
    # 10 minutes (and still honours `requests` if set).
    stop_rule: StopRule = "count"
    min_sample: int = 1000
    # 'unique_ratio' only: once a run has had a success, this long without
    # another one means the network went down; the run stops as 'stalled'.
    stall_seconds: float = 600
    # Opt-in census policy: keep 469 attempts in telemetry, but replace them
    # without consuming the unique-ratio sample. Explicit attempt/time caps remain.
    retry_http_469: bool = False
    # Census rule 4 and count runs (answers.py): rate-limited answers are replaced and never
    # count toward the goal; no-IP and other-country answers count like duplicates.
    answer_classes: AnswerClasses | None = None

    def _check_policies(self) -> None:
        if type(self.retry_http_469) is not bool:
            raise ValueError("retry_http_469 must be a boolean")
        if self.retry_http_469 and self.stop_rule != "unique_ratio":
            raise ValueError("HTTP 469 retries require the unique-ratio census rule")
        if self.answer_classes is not None and not isinstance(self.answer_classes, AnswerClasses):
            raise ValueError("answer_classes must be AnswerClasses")
        if self.answer_classes is not None and self.retry_http_469:
            raise ValueError("Answer classes replace retry_http_469; set only one")

    def __post_init__(self) -> None:
        self._check_policies()
        if type(self.processes) is not int or self.processes < 0:
            raise ValueError("processes must be a nonnegative integer (0 means automatic)")
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
        protocol_of(self.proxy)  # Reject an unknown network test protocol before any traffic.
        for name in ("provider", "product", "requested_country"):
            effective = metadata.get(name)
            supplied = getattr(self, name)
            if supplied is not None and supplied != effective and name in metadata:
                raise ValueError(f"{name} label conflicts with provider configuration")
            if name in metadata:
                object.__setattr__(self, name, effective)
        if self.stop_rule not in ("count", "unique_ratio"):
            raise ValueError("stop_rule must be 'count' or 'unique_ratio'")
        if self.requests is None and self.stop_rule == "count":
            raise ValueError("A count stop rule requires a request count")
        for name in ("requests", "concurrency", "min_sample"):
            value = getattr(self, name)
            if value is None and name == "requests":
                continue
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("timeout_seconds", "requests_per_second", "stall_seconds"):
            value = getattr(self, name)
            if value is None and name in ("timeout_seconds", "stall_seconds"):
                raise ValueError(f"{name} must be finite and positive")
            if value is not None and (
                type(value) not in (float, int) or not math.isfinite(value) or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")

    def public(self) -> dict[str, Any]:
        # The stop-rule fields are omitted for count runs, so they keep the
        # configuration (and storage scope hash) they had before stop rules.
        version, basis = census_rule(self.retry_http_469, self.answer_classes is not None)
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name not in ("proxy", "answer_classes")
            and not (name == "retry_http_469" and not self.retry_http_469)
            and not (name in STOP_RULE_FIELDS and self.stop_rule == "count")
        } | ({"answer_classes": self.answer_classes.public()} if self.answer_classes else {}
             ) | self.proxy.public() | ({
                 "census_rule_version": version, "census_ratio_basis": basis,
             } if self.stop_rule == "unique_ratio" else {})


@dataclass(frozen=True, slots=True)
class Outcome:
    latency_ms: float
    ip: str | None = None
    error: ErrorCode | None = None
    status: int | None = None
    response_body_bytes: int = 0
    # None derives the verdict from the error code (outcome.from_error).
    verdict: VerdictName | None = None
    # Bytes on the wire to and from the gateway; exact only from Linux TCP_INFO.
    bytes_up: int = 0
    bytes_down: int = 0
    bytes_exact: bool = False
    # Transport completion on the system monotonic clock, before IPC/aggregation.
    completed_at: float | None = None
    # A 'mixed' network answered HTTP with a redirect that was followed over HTTPS;
    # latency, bytes and body then cover both legs and status is the HTTPS answer.
    redirected: bool = False

    def __post_init__(self) -> None:
        verdict = from_error(self.error) if self.verdict is None else self.verdict
        if verdict not in VERDICTS:
            raise ValueError("Unknown verdict")
        if (verdict == "ok") != (self.error is None):
            raise ValueError("Only an error-free outcome has the ok verdict")
        object.__setattr__(self, "verdict", str(verdict))


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
    requested: int | None
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
    challenges: int = 0
    blocks: int = 0
    timeouts: int = 0
    proxy_errors: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    stop_reason: str | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)
    # Wall time of the last success before a 'stalled' stop; omitted otherwise.
    outage_started_at: str | None = None
    census_requests: int | None = None
    http_469_responses: int | None = None
    # Completed requests that followed an HTTP-to-HTTPS redirect; 'mixed' networks only.
    redirects: int | None = None
    # Answer classes (RunConfig.answer_classes; answers.py), all kept in the raw counters too:
    # completed answers per rate-limit signal, no-IP answers, and successes whose exit was in
    # another country than the requested one (None unless the country check ran).
    rate_limited: dict[str, int] | None = None
    no_inventory: int | None = None
    other_country: int | None = None

    def to_dict(self) -> dict[str, Any]:
        summary = asdict(self)
        if summary["outage_started_at"] is None:
            del summary["outage_started_at"]
        for key in ("census_requests", "http_469_responses", "redirects", "rate_limited",
                    "no_inventory", "other_country"):
            if summary[key] is None:
                del summary[key]
        return summary
