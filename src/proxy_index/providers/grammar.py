"""Declarative credential grammars: one reviewed TOML per provider, one shared engine.

Each provider encodes targeting and sessions differently (username or password flags,
custom separators, port-selected sessions). A grammar file describes those rules; this
module validates parameters against it and resolves per-request proxy credentials
without I/O or shared mutable state. Grammars live in ``providers/grammars/<id>.toml``.
"""

from __future__ import annotations

import hashlib
import re
import string
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from types import MappingProxyType
from typing import Any

from ..models import (
    NETWORK_PROTOCOLS,
    AnswerClasses,
    NetworkProtocol,
    ProxyConfig,
    RequestContext,
)
from .flags import MAX_CONCURRENCY as MAX_CONCURRENCY
from .flags import Flag, NoCountryList, _range, _testing
from .flags import _statuses as _statuses

POLICIES = ("rotate", "gateway", "fixed", "per_request", "per_worker", "per_run")
GENERATED = ("per_request", "per_worker", "per_run")
KINDS = ("country", "name", "integer", "boolean", "choice", "token", "session")
ALPHABETS = {
    "hex": "0123456789abcdef",
    "alnum": string.ascii_lowercase + string.digits,
    "digits": string.digits,
}
HOST = re.compile(r"[A-Za-z0-9.-]+")


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

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> Grammar:
        try:
            return cls._parse(data)
        except (KeyError, TypeError, IndexError, AttributeError, re.error) as exc:
            raise ValueError(f"Invalid provider grammar ({type(exc).__name__})") from None

    @classmethod
    def _parse(cls, data: Mapping[str, Any]) -> Grammar:
        tables = {
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
            },
        }
        for table, keys in tables.items():
            if set(data.get(table, {})) - keys:
                raise ValueError(f"Unknown keys in grammar table {table}")
        allowed = {
            "schema_version",
            "id",
            "product",
            "version",
            "docs",
            "credential",
            "account",
            "gateway",
            "flags",
            "session",
            "pools",
            "extra_flags",
            "testing",
        }
        if set(data) - allowed or data.get("schema_version") != 1:
            raise ValueError("Unsupported provider grammar schema")
        provider = data["id"]
        if not isinstance(provider, str) or not re.fullmatch(r"[a-z0-9-]+", provider):
            raise ValueError("Grammar id must be a lowercase provider slug")
        cred, account, gateway = data["credential"], data.get("account", {}), data["gateway"]
        session = data.get("session", {})
        if cred.get("flags_in") not in ("username", "password"):
            raise ValueError("Grammar flags must be carried in the username or password")
        flags = {}
        for name, spec in data.get("flags", {}).items():
            spec = dict(spec)
            kind = spec.pop("kind")
            if kind not in KINDS:
                raise ValueError(f"Unknown grammar flag kind for {provider} {name}")
            for key in ("requires", "conflicts", "choices", "countries"):
                if key in spec:
                    spec[key] = tuple(spec[key])
            if "min" in spec:
                spec["minimum"] = spec.pop("min")
            if "max" in spec:
                spec["maximum"] = spec.pop("max")
            if spec.get("preset"):
                spec.setdefault("format", "")
            flags[name] = Flag(name=name, kind=kind, **spec)
        names = set(flags)
        for flag in flags.values():
            if set(flag.requires + flag.conflicts) - names:
                raise ValueError(f"Grammar {provider} {flag.name} references an unknown flag")
            if flag.kind != "session" and "session" in flag.requires + flag.conflicts:
                raise ValueError("Use requires_session for flags that need a session")
            if flag.countries and (
                flag.kind != "country"
                or not all(re.fullmatch(r"[A-Z]{2}", code) for code in flag.countries)
            ):
                raise ValueError(f"Grammar {provider} {flag.name} countries must be ISO codes")
            if flag.case not in (None, "lower", "upper") or type(flag.scale) is not int:
                raise ValueError(f"Grammar {provider} {flag.name} has an invalid rendering")
            if flag.preset and (
                flag.format
                or flag.kind in ("session", "boolean")
                or flag.requires
                or flag.conflicts
            ):
                raise ValueError(f"Grammar {provider} {flag.name} cannot be a preset")
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
        hosts = dict(gateway.get("hosts", {}))
        default_host = gateway.get("default", "default")
        if "host" in gateway:
            hosts.setdefault(default_host, gateway["host"])
        if default_host not in hosts or not all(HOST.fullmatch(h) for h in hosts.values()):
            raise ValueError("Grammar gateway hosts are invalid")
        ports = dict(gateway["ports"])
        if set(ports) - {"http", "socks5"} or not ports:
            raise ValueError("Grammar ports are keyed by http or socks5")
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
            rotating_ports=MappingProxyType(
                {
                    scheme: _range(value, "rotating_ports")
                    for scheme, value in gateway.get("rotating_ports", {}).items()
                }
            ),
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
            pools=MappingProxyType(
                {
                    name: frozenset(rule.get("forbid", ()))
                    for name, rule in data.get("pools", {}).items()
                }
            ),
            extra_flags=data.get("extra_flags", False),
            reserved=frozenset(cred.get("reserved", ()))
            | {flag.key for flag in flags.values() if not flag.preset},
            **_testing(data.get("testing", {})),
        )


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


@dataclass(frozen=True, slots=True)
class GrammarProxy:
    """Immutable provider configuration resolved through the provider's grammar.

    None parameters are omitted. A supplied session implies fixed sessions unless
    session_policy is given; generated sessions are isolated by run, worker or request.
    """

    provider: str
    account: str | None = field(default=None, repr=False)
    password: str = field(default="", repr=False)
    parameters: Mapping[str, Any] = field(default_factory=dict, repr=False)
    extra_flags: Mapping[str, Any] = field(default_factory=dict, repr=False)
    scheme: str = "http"
    gateway: str | None = None
    host: str | None = None
    port: int | None = None
    session_policy: str | None = None
    pool: str | None = None
    # Values of the grammar's preset flags, as configured on the provider's side.
    presets: Mapping[str, Any] = field(default_factory=dict)

    def __reduce__(self):
        # Spawned HTTP workers rebuild the config through validation.
        return (
            _rebuild,
            (
                self.provider,
                self.account,
                self.password,
                dict(self.parameters),
                dict(self.extra_flags),
                self.scheme,
                self.gateway,
                self.host,
                self.port,
                self.session_policy,
                self.pool,
                dict(self.presets),
            ),
        )

    @property
    def id(self) -> str:
        return self.provider

    @property
    def grammar(self) -> Grammar:
        return load_grammar(self.provider)

    @property
    def test_protocol(self) -> NetworkProtocol:
        return load_grammar(self.provider).test_protocol

    def __post_init__(self) -> None:
        g = load_grammar(self.provider)
        name = g.id.capitalize()
        if g.account_required:
            if not isinstance(self.account, str):
                raise ValueError(f"{name} account is required")
            account = self.account
            for prefix in g.strip_prefixes:
                account = account.removeprefix(prefix)
            if not re.fullmatch(g.account_pattern, account) or (
                g.target == "username" and set(account.lower().split(g.separator)) & g.reserved
            ):
                raise ValueError(f"Invalid {name} account syntax")
            object.__setattr__(self, "account", account)
        elif self.account is not None:
            raise ValueError(f"{name} authenticates without an account")
        if not isinstance(self.password, str) or not self.password:
            raise ValueError(f"{name} password is required")
        if self.scheme not in g.ports:
            raise ValueError(f"{name} transport must be one of: {', '.join(g.ports)}")
        self._gateway(g, name)
        if not isinstance(self.parameters, Mapping) or not isinstance(self.extra_flags, Mapping):
            raise ValueError(f"{name} parameters and extra_flags must be mappings")
        if (
            set(self.parameters)
            - set(g.flags)
            - ({"session"} if g.session_mode == "port" else set())
        ):
            raise ValueError(f"Unknown {name} parameter")
        if g.slot_flag in self.parameters:
            raise ValueError(f"{name} {g.slot_flag} is set from the session slot")
        params = {}
        for key, value in self.parameters.items():
            if value is None:
                continue
            if key == "session" and g.session_mode == "port":  # A zero-based slot index.
                low, high = g.session_ports
                if type(value) is not int or not 0 <= value <= high - low:
                    raise ValueError(f"{name} session slot is outside the port range")
                params[key] = value
                continue
            params[key] = g.flags[key].normalize(value, name)
        params = self._presets(g, params, name)
        for key, flag in g.flags.items():
            if flag.kind == "boolean" and key in params and flag.render(params[key]) is None:
                del params[key]
        for key in list(params):
            if key == "session" and g.session_mode == "port":
                continue
            flag = g.flags[key]
            if key == g.session:
                continue  # Session requirements apply to generated sessions too (below).
            missing = [r for r in flag.requires if r != "session" and r not in params]
            if missing:
                raise ValueError(f"{name} {key} requires {', '.join(missing)}")
            if set(flag.conflicts) & set(params):
                raise ValueError(f"{name} cannot combine {key} with {flag.conflicts[0]}")
        for key, value in params.items():
            # A two-letter country cannot carry a separator; ISO codes such as CC, CR or ST
            # may still equal a flag key and are valid in the country slot. Checked here, not
            # assumed, so a grammar that widens a country pattern keeps the guard.
            if isinstance(value, str) and not (
                g.flags[key].kind == "country" and re.fullmatch(r"[A-Za-z]{2}", value)
            ):
                self._check_delimiters(g, value, name, key)
        session_key = g.session or "session"
        policy = self.session_policy or ("fixed" if session_key in params else g.no_session_policy)
        if policy not in POLICIES:
            raise ValueError(f"Unknown {name} session policy")
        if (policy == "fixed") != (session_key in params):
            raise ValueError("Only fixed sessions accept and require a supplied session")
        if policy in GENERATED and g.generate is None:
            raise ValueError(f"{name} sessions cannot be generated; supply a session")
        if g.session is None and g.session_mode == "flag" and policy not in ("rotate", "gateway"):
            raise ValueError(f"{name} does not support sessions")
        if policy == "gateway" and g.no_session_policy == "rotate" and not g.rotating_ports:
            raise ValueError(f"{name} rotates per request; use the rotate policy")
        if policy == "rotate" and g.no_session_policy != "rotate" and not g.rotating_ports:
            raise ValueError(f"{name} rotation is configured by the provider, not per request")
        sticky = policy not in ("rotate", "gateway")
        if sticky and g.session is not None:
            missing = [r for r in g.flags[g.session].requires if r not in params]
            if missing:
                raise ValueError(f"{name} sessions require {', '.join(missing)}")
        if any(g.flags[k].requires_session for k in params if k in g.flags) and not sticky:
            raise ValueError(f"{name} session lifetime requires a supplied or generated session")
        rotating = g.rotating_ports.get(self.scheme)
        if rotating is not None:
            on_rotating = rotating[0] <= self.port <= rotating[1]
            if on_rotating and (
                sticky
                or any(
                    g.flags[k].requires_session or g.flags[k].sticky_ports_only
                    for k in params
                    if k in g.flags
                )
            ):
                raise ValueError(f"{name} sessions require a sticky gateway port")
            if policy == "rotate" and not on_rotating:
                raise ValueError(f"{name} rotation requires a rotating gateway port")
        if self.pool is not None:
            if self.pool not in g.pools:
                raise ValueError(f"Unknown {name} account pool")
            if set(params) & g.pools[self.pool]:
                raise ValueError(f"{name} targeting is not supported by the configured pool")
        extras = self._extras(g, params, name)
        object.__setattr__(self, "parameters", MappingProxyType(params))
        object.__setattr__(self, "extra_flags", MappingProxyType(extras))
        object.__setattr__(self, "session_policy", policy)
        # Fail before sending traffic, e.g. for SOCKS's 255-byte authentication limit.
        self.resolve(RequestContext("validation", 0, 0))

    def _gateway(self, g: Grammar, name: str) -> None:
        if self.host is not None and self.gateway is not None:
            # Rebuilt copies carry both; anything else is an ambiguous choice.
            if g.hosts.get(self.gateway) != self.host:
                raise ValueError(f"Choose either a named {name} gateway or a host")
        elif self.host is not None:
            if (
                not g.host_override
                or not isinstance(self.host, str)
                or not HOST.fullmatch(self.host)
            ):
                raise ValueError(f"Invalid {name} gateway host")
        else:
            gateway = self.gateway or g.default_host
            if gateway not in g.hosts:
                raise ValueError(f"Unknown {name} regional gateway")
            object.__setattr__(self, "host", g.hosts[gateway])
            object.__setattr__(self, "gateway", gateway)
        if self.port is None:
            object.__setattr__(self, "port", g.ports[self.scheme])
        elif not g.port_override and self.port != g.ports[self.scheme]:
            raise ValueError(f"{name} uses fixed gateway ports")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError(f"{name} port must be an integer between 1 and 65535")

    def _presets(self, g: Grammar, params: dict[str, Any], name: str) -> dict[str, Any]:
        """Fill preset flags from `presets` and refuse any other value: the credential
        cannot change them, so a request for another value would be mislabelled."""
        bound = {key for key, flag in g.flags.items() if flag.preset}
        if not isinstance(self.presets, Mapping) or set(self.presets) - bound:
            raise ValueError(f"{name} presets accept only: {', '.join(sorted(bound)) or 'none'}")
        declared = {key: g.flags[key].normalize(value, name) for key, value in self.presets.items()}
        object.__setattr__(self, "presets", MappingProxyType(declared))
        for key in bound & set(params):
            if key not in declared:
                raise ValueError(
                    f"{name} {key} is set on the provider's side; declare it in "
                    "the config's presets"
                )
            if params[key] != declared[key]:
                raise ValueError(f"{name} {key} differs from the provider-side preset")
        return declared | params

    def _check_delimiters(self, g: Grammar, value: str, name: str, key: str) -> None:
        # Values may contain the separator (e.g. hyphenated Shifter sids or Byteful's
        # new_york) but must not smuggle another flag into the credential.
        words = {re.split(r"[^a-z0-9]", token)[0] for token in value.lower().split(g.separator)}
        if words & g.reserved:
            raise ValueError(f"{name} {key} contains an ambiguous flag delimiter")

    def _extras(self, g: Grammar, params: Mapping[str, Any], name: str) -> dict[str, str]:
        if self.extra_flags and not g.extra_flags:
            raise ValueError(f"Unsupported {name} extra flags; use documented parameters")
        extras = {}
        for key, value in self.extra_flags.items():
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]*", key)
                or key in g.flags
                or key in g.reserved
                or key in ("account", "password")
            ):
                raise ValueError(f"Invalid or reserved {name} extra flag name")
            if value is None:
                continue
            value = str(value).lower() if type(value) is bool else value
            value = str(value) if type(value) is int else value
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_]+", value):
                raise ValueError(f"Invalid {name} extra flag value")
            self._check_delimiters(g, value, name, key)
            session = str(params.get(g.session or "", ""))
            if any(
                re.search(
                    rf"(?:^|{re.escape(g.separator)}){key}(?:{re.escape(g.separator)}|$)",
                    item.lower(),
                )
                for item in (self.account or "", session)
            ):
                raise ValueError(f"{name} extra flag conflicts with an authentication token")
            extras[key] = value
        return extras

    def _generated(self, g: Grammar, context: RequestContext) -> str | int:
        scope = {
            "per_request": context.request_index,
            "per_worker": context.worker_index,
            "per_run": 0,
        }[self.session_policy]
        seed = f"{context.run_id}:{self.session_policy}:{scope}".encode()
        digest = hashlib.sha256(seed)
        if g.session_mode == "flag" and g.generate == "hex":
            return digest.hexdigest()[: g.generate_length]  # Stable with earlier releases.
        number = int.from_bytes(digest.digest(), "big")
        if g.session_mode == "port":
            low, high = g.session_ports
            if self.session_policy == "per_request":  # The next port, wrapping to the first.
                return context.request_index % (high - low + 1)
            return number % (high - low + 1)
        alphabet = ALPHABETS[g.generate]
        chars = []
        for _ in range(g.generate_length):
            number, index = divmod(number, len(alphabet))
            chars.append(alphabet[index])
        if g.generate == "digits" and chars[0] == "0":
            chars[0] = "1"  # Avoid numbers that a gateway could parse differently.
        return "".join(chars)

    def resolve(self, context: RequestContext) -> ProxyConfig:
        g = load_grammar(self.provider)
        params = dict(self.parameters)
        port = self.port
        if self.session_policy in GENERATED:
            params[g.session or "session"] = self._generated(g, context)
        if g.session_mode == "port" and "session" in params:
            slot = params.pop("session")
            port = g.session_ports[0] + slot
            if g.slot_flag and self.session_policy != "per_request":
                params[g.slot_flag] = slot  # Sticky: the slot names the session too.
        pieces = [
            rendered
            for key, flag in g.flags.items()
            if key in params and not flag.preset
            if (rendered := flag.render(params[key])) is not None
        ]
        pieces.extend(
            f"{key}{g.separator}{value}" for key, value in sorted(self.extra_flags.items())
        )
        if g.rotation_suffix and self.session_policy == "rotate":
            pieces.append(g.rotation_suffix)
        fields = {"account": self.account or "", "password": self.password}
        base = (g.flagged if pieces and g.flagged else getattr(g, g.target)).format(**fields)
        flagged = base + (g.first_separator if base and pieces else "") + g.separator.join(pieces)
        if not flagged:
            raise ValueError(f"{g.id.capitalize()} requires at least one targeting flag")
        other = "password" if g.target == "username" else "username"
        values = {g.target: flagged, other: getattr(g, other).format(**fields)}
        if self.scheme == "socks5" and any(
            not item.isascii() or len(item) > 255 for item in values.values()
        ):
            raise ValueError("SOCKS5 authentication requires at most 255 ASCII bytes per field")
        return ProxyConfig(self.scheme, self.host, port, values["username"], values["password"])

    def public(self) -> dict[str, Any]:
        g = load_grammar(self.provider)
        params = dict(self.parameters)
        session_key = g.session or "session"
        if session_key in params:
            params[session_key] = "[redacted]"
        ttl = self.parameters[g.ttl] * g.ttl_seconds if g.ttl in self.parameters else None
        if ttl is None and self.session_policy not in ("rotate", "gateway"):
            ttl = g.default_ttl_seconds
        return {
            "proxy_protocol": self.scheme,
            "provider": self.provider,
            "product": g.product,
            "requested_country": (
                self.parameters["country"].lower() if "country" in self.parameters else None
            ),
            "provider_config": {
                "adapter": self.provider,
                "version": g.version,
                "gateway": {"host": self.host, "port": self.port},
                "pool": self.pool,
                "parameters": params,
                "session_policy": self.session_policy,
                "effective_ttl_seconds": ttl,
                # Future flags may contain credentials. Values are private by default.
                "extra_flags": dict.fromkeys(self.extra_flags, "[redacted]"),
            },
            # Only non-default networks publish it, so default run configs are unchanged.
        } | ({"test_protocol": g.test_protocol} if g.test_protocol != "default" else {})


def _rebuild(*args: Any) -> GrammarProxy:
    return GrammarProxy(*args)
