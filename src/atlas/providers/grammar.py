"""Declarative credential grammars: one reviewed TOML per provider, one shared engine.

Each provider encodes targeting and sessions differently (username or password flags,
custom separators, port-selected sessions). A grammar file describes those rules; this
module loads and checks it, and `providers.proxy.GrammarProxy` validates parameters against
it and resolves per-request proxy credentials without I/O or shared mutable state.
Grammars live in ``providers/grammars/<id>.toml``.
"""

from __future__ import annotations

import re
import string
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from types import MappingProxyType
from typing import Any

from ..models import NETWORK_PROTOCOLS, AnswerClasses, NetworkProtocol
from .flags import KINDS as KINDS
from .flags import MAX_CONCURRENCY as MAX_CONCURRENCY
from .flags import Flag, _flags, _range, _testing
from .flags import NoCountryList as NoCountryList
from .flags import _statuses as _statuses

ALPHABETS = {
    "hex": "0123456789abcdef",
    "alnum": string.ascii_lowercase + string.digits,
    "digits": string.digits,
}
HOST = re.compile(r"[A-Za-z0-9.-]+")
# The keys each grammar table may hold; Grammar._schema rejects any other.
TABLE_KEYS = {
    "credential": {
        "flags_in",
        "username",
        "password",
        "flagged",
        "first_separator",
        "separator",
        "rotation_suffix",
        "reserved",
    },
    "account": {"required", "pattern", "strip_prefixes"},
    "gateway": {
        "host",
        "hosts",
        "default",
        "host_override",
        "ports",
        "port_override",
        "rotating_ports",
    },
    "session": {
        "flag",
        "mode",
        "ports",
        "generate",
        "length",
        "no_session_policy",
        "slot_flag",
        "ttl_flag",
        "default_ttl_seconds",
    },
    "testing": {
        "protocol",
        "max_concurrency",
        "rate_limit_statuses",
        "rate_limit_connect",
        "no_inventory_statuses",
        "echo_http",
        "echo_https",
    },
}
# The top-level keys of a grammar: its tables and these.
GRAMMAR_KEYS = {
    *TABLE_KEYS,
    "schema_version",
    "id",
    "product",
    "version",
    "docs",
    "flags",
    "pools",
    "extra_flags",
}


@dataclass(frozen=True, slots=True)
class Grammar:
    id: str
    product: str
    version: str
    target: str  # username or password carries the flags
    username: str
    password: str
    flagged: str | None  # Base template once any flag is present (Decodo's user- prefix).
    first_separator: str
    separator: str
    rotation_suffix: str | None
    account_required: bool
    account_pattern: str
    strip_prefixes: tuple[str, ...]
    hosts: Mapping[str, str]
    default_host: str
    host_override: bool
    ports: Mapping[str, int]
    port_override: bool
    rotating_ports: Mapping[str, tuple[int, int]]
    flags: Mapping[str, Flag]
    session: str | None
    session_mode: str  # flag or port
    session_ports: tuple[int, int] | None
    generate: str | None
    generate_length: int
    no_session_policy: str
    # Port mode only: a session flag that carries the slot of sticky sessions (fixed,
    # per_worker, per_run), for logins whose ports alone do not keep an exit.
    slot_flag: str | None
    ttl: str | None
    ttl_seconds: int
    default_ttl_seconds: int | None
    pools: Mapping[str, frozenset[str]]
    extra_flags: bool
    reserved: frozenset[str]
    test_protocol: NetworkProtocol = "default"  # [testing] protocol; see models.NetworkProtocol
    # [testing] max_concurrency: the highest in-flight request count the network sustained
    # from one worker (`campaign probe`); campaign plans cap their concurrency at it.
    max_concurrency: int | None = None
    # [testing] answer classes (answer_classes() below; 429 is implicit on every network):
    # the network's own rate-limit statuses, whether gateway connection refusals signal its rate
    # limit, and the statuses it sends when it has no exits in the requested country.
    rate_limit_statuses: tuple[int, ...] = ()
    rate_limit_connect: bool = False
    no_inventory_statuses: tuple[int, ...] = ()
    # [testing] echo_http / echo_https: the network's own IP-check URLs by echo target id, run
    # instead of the built-in ones (targets.network_echo).
    echo_urls: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> Grammar:
        try:
            return cls._parse(data)
        except (KeyError, TypeError, IndexError, AttributeError, re.error) as exc:
            raise ValueError(f"Invalid provider grammar ({type(exc).__name__})") from None

    @classmethod
    def _parse(cls, data: Mapping[str, Any]) -> Grammar:
        provider = cls._schema(data)
        cred, account, gateway = data["credential"], data.get("account", {}), data["gateway"]
        session = data.get("session", {})
        if cred.get("flags_in") not in ("username", "password"):
            raise ValueError("Grammar flags must be carried in the username or password")
        flags = _flags(provider, data.get("flags", {}))
        session_flag, mode, slot_flag, generate, policy = cls._session(data, session, flags)
        hosts, default_host, ports = cls._hosts(gateway)
        return cls(
            id=provider,
            product=data.get("product", "residential"),
            version=str(data.get("version", "1")),
            target=cred["flags_in"],
            username=cred.get("username", "{account}"),
            password=cred.get("password", "{password}"),
            flagged=cred.get("flagged"),
            first_separator=cred.get("first_separator", cred.get("separator", "-")),
            separator=cred.get("separator", "-"),
            rotation_suffix=cred.get("rotation_suffix"),
            account_required=account.get("required", True),
            account_pattern=account.get("pattern", r"[A-Za-z0-9_]+"),
            strip_prefixes=tuple(account.get("strip_prefixes", ())),
            hosts=MappingProxyType(hosts),
            default_host=default_host,
            host_override=gateway.get("host_override", False),
            ports=MappingProxyType(ports),
            port_override=gateway.get("port_override", False),
            rotating_ports=cls._rotating_ports(gateway),
            flags=MappingProxyType(flags),
            session=session_flag,
            session_mode=mode,
            session_ports=(_range(session["ports"], "session ports") if mode == "port" else None),
            generate=generate if mode == "flag" else "digits",
            generate_length=session.get("length", 16),
            no_session_policy=policy,
            slot_flag=slot_flag,
            ttl=session.get("ttl_flag"),
            ttl_seconds=60,  # Canonical ttl is in minutes.
            default_ttl_seconds=session.get("default_ttl_seconds"),
            pools=cls._pools(data),
            extra_flags=data.get("extra_flags", False),
            reserved=frozenset(cred.get("reserved", ()))
            | {flag.key for flag in flags.values() if not flag.preset},
            **_testing(data.get("testing", {})),
        )

    @staticmethod
    def _rotating_ports(gateway: Mapping[str, Any]) -> Mapping[str, tuple[int, int]]:
        ranges = gateway.get("rotating_ports", {}).items()
        return MappingProxyType(
            {scheme: _range(value, "rotating_ports") for scheme, value in ranges}
        )

    @staticmethod
    def _pools(data: Mapping[str, Any]) -> Mapping[str, frozenset[str]]:
        pools = data.get("pools", {}).items()
        return MappingProxyType({name: frozenset(rule.get("forbid", ())) for name, rule in pools})

    @staticmethod
    def _schema(data: Mapping[str, Any]) -> str:
        """Reject unknown tables, keys and schema versions; return the provider id."""
        for table, keys in TABLE_KEYS.items():
            if set(data.get(table, {})) - keys:
                raise ValueError(f"Unknown keys in grammar table {table}")
        if set(data) - GRAMMAR_KEYS or data.get("schema_version") != 1:
            raise ValueError("Unsupported provider grammar schema")
        provider = data["id"]
        if not isinstance(provider, str) or not re.fullmatch(r"[a-z0-9-]+", provider):
            raise ValueError("Grammar id must be a lowercase provider slug")
        return provider

    @staticmethod
    def _session(
        data: Mapping[str, Any], session: Mapping[str, Any], flags: Mapping[str, Flag]
    ) -> tuple[str | None, str, str | None, str | None, str]:
        """Check the session table and every pattern; return its flag, mode, slot flag,
        alphabet and no-session policy."""
        ttl_flag = session.get("ttl_flag")
        if ttl_flag is not None and (ttl_flag not in flags or flags[ttl_flag].kind != "integer"):
            raise ValueError("Grammar ttl_flag must name an integer flag")
        if type(session.get("length", 16)) is not int or session.get("length", 16) < 1:
            raise ValueError("Grammar generated session length must be positive")
        for flag in flags.values():
            if flag.pattern is not None:
                re.compile(flag.pattern)
        re.compile(data.get("account", {}).get("pattern", ""))
        alphabet = ALPHABETS.get(session.get("generate"))
        pattern = flags[session["flag"]].pattern if session.get("flag") in flags else None
        if alphabet and pattern:
            length = session.get("length", 16)
            # Generated digit sessions never start with zero (see _generated).
            forward, reverse = alphabet * length, alphabet[::-1] * length
            samples = ("1" + forward[1:length], "1" + reverse[1:length])
            if not all(re.fullmatch(pattern, sample) for sample in samples):
                raise ValueError("Grammar generated sessions would not match the session pattern")
        session_flag = session.get("flag")
        mode = session.get("mode", "flag")
        if mode not in ("flag", "port") or (mode == "flag") != (session_flag is not None):
            raise ValueError("Grammar sessions use either a session flag or session ports")
        slot_flag = session.get("slot_flag")
        if slot_flag is not None and (
            mode != "port" or slot_flag not in flags or flags[slot_flag].kind != "session"
        ):
            raise ValueError("Grammar slot_flag must name a session flag of a port grammar")
        if session_flag is not None and flags[session_flag].kind != "session":
            raise ValueError("Grammar session flag must have kind session")
        generate = session.get("generate")
        if generate not in (None, *ALPHABETS):
            raise ValueError("Unknown grammar session alphabet")
        policy = session.get("no_session_policy", "rotate")
        if policy not in ("rotate", "gateway", *(("per_request",) if mode == "port" else ())):
            raise ValueError("Grammar default must be rotate or gateway (or per_request on ports)")
        return session_flag, mode, slot_flag, generate, policy

    @staticmethod
    def _hosts(gateway: Mapping[str, Any]) -> tuple[dict[str, str], str, dict[str, int]]:
        """The gateway's named hosts, its default host name and its ports."""
        hosts = dict(gateway.get("hosts", {}))
        default_host = gateway.get("default", "default")
        if "host" in gateway:
            hosts.setdefault(default_host, gateway["host"])
        if default_host not in hosts or not all(HOST.fullmatch(h) for h in hosts.values()):
            raise ValueError("Grammar gateway hosts are invalid")
        ports = dict(gateway["ports"])
        if set(ports) - {"http", "socks5"} or not ports:
            raise ValueError("Grammar ports are keyed by http or socks5")
        return hosts, default_host, ports


@cache
def load_grammar(provider: str) -> Grammar:
    if not re.fullmatch(r"[a-z0-9-]+", provider):
        raise ValueError("Unknown provider grammar")
    path = resources.files(__package__).joinpath("grammars", f"{provider}.toml")
    if not path.is_file():
        raise ValueError("Unknown provider grammar")
    grammar = Grammar.parse(tomllib.loads(path.read_text("utf-8")))
    if grammar.id != provider:
        raise ValueError("Provider grammar ID does not match its file name")
    return grammar


def answer_classes(provider: str) -> AnswerClasses:
    """The network's answer classes: 429 plus its grammar's [testing] statuses."""
    grammar = load_grammar(provider)
    return AnswerClasses(
        (
            "429",
            *map(str, grammar.rate_limit_statuses),
            *(("proxy_connect",) if grammar.rate_limit_connect else ()),
        ),
        tuple(map(str, grammar.no_inventory_statuses)),
    )


def grammar_ids() -> tuple[str, ...]:
    root = resources.files(__package__).joinpath("grammars")
    return tuple(
        sorted(
            item.name.removesuffix(".toml")
            for item in root.iterdir()
            if item.name.endswith(".toml")
        )
    )


def grammar_ids_by_protocol(protocol: NetworkProtocol) -> tuple[str, ...]:
    """Networks whose echo test uses `protocol`, e.g. 'https-only' for no plain-HTTP test."""
    if protocol not in NETWORK_PROTOCOLS:
        raise ValueError("Unknown network test protocol")
    return tuple(item for item in grammar_ids() if load_grammar(item).test_protocol == protocol)


# GrammarProxy (providers.proxy), imported after everything it uses is defined above.
# providers.proxy imports this module back: that works because atlas.providers
# always loads this module before providers.proxy.
from .proxy import GrammarProxy as GrammarProxy  # noqa: E402
