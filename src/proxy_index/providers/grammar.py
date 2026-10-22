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

from ..models import ProxyConfig, RequestContext

POLICIES = ("rotate", "gateway", "fixed", "per_request", "per_worker", "per_run")
GENERATED = ("per_request", "per_worker", "per_run")
KINDS = ("country", "name", "integer", "boolean", "choice", "token", "session")
ALPHABETS = {"hex": "0123456789abcdef", "alnum": string.ascii_lowercase + string.digits,
             "digits": string.digits}
HOST = re.compile(r"[A-Za-z0-9.-]+")


def _range(value: Any, name: str) -> tuple[int, int]:
    if (not isinstance(value, list) or len(value) != 2
            or not all(type(item) is int for item in value) or value[0] > value[1]):
        raise ValueError(f"Grammar {name} must be an inclusive [low, high] integer range")
    return value[0], value[1]


@dataclass(frozen=True, slots=True)
class Flag:
    name: str
    format: str
    kind: str
    case: str | None = None
    spaces: str = "_"
    pattern: str | None = None
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    true: str | None = None
    false: str | None = None
    requires: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    requires_session: bool = False
    sticky_ports_only: bool = False
    scale: int = 1  # Rendered value multiplier, e.g. canonical minutes to native seconds.

    @property
    def key(self) -> str:
        """Literal key word of the rendered flag, used to detect delimiter injection."""
        return re.split(r"[^A-Za-z0-9]", self.format.split("{}")[0] or "_")[0].lower()

    def normalize(self, value: Any, provider: str) -> Any:
        label = f"{provider} {self.name}"
        if self.kind == "boolean":
            if type(value) is not bool:
                raise ValueError(f"{label} must be a boolean")
            return value
        if self.kind == "integer":
            if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
                value = int(value)
            if type(value) is not int or not (
                (self.minimum is None or value >= self.minimum)
                and (self.maximum is None or value <= self.maximum)
            ):
                raise ValueError(f"{label} is outside its supported range")
            return value
        if not isinstance(value, str):
            raise ValueError(f"{label} must be a string")
        if self.kind in ("country", "name"):
            value = self.spaces.join(value.strip().split())
        if self.case == "lower":
            value = value.lower()
        elif self.case == "upper":
            value = value.upper()
        pattern = self.pattern or {
            "country": r"[A-Za-z]{2}", "name": r"[A-Za-z0-9_]+", "session": r"[A-Za-z0-9]+",
        }.get(self.kind)
        if self.kind == "choice":
            if value not in self.choices:
                raise ValueError(f"Unsupported {label} value")
        elif pattern is None or not re.fullmatch(pattern, value):
            raise ValueError(f"Invalid {label} syntax")
        return value

    def render(self, value: Any) -> str | None:
        if self.kind == "boolean":
            text = self.true if value else self.false
            return text  # A boolean without a rendering is omitted.
        if self.kind == "integer":
            value *= self.scale
        return self.format.replace("{}", str(value))


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
    ttl: str | None
    ttl_seconds: int
    default_ttl_seconds: int | None
    pools: Mapping[str, frozenset[str]]
    extra_flags: bool
    reserved: frozenset[str]

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> Grammar:
        try:
            return cls._parse(data)
        except (KeyError, TypeError, IndexError, AttributeError, re.error) as exc:
            raise ValueError(f"Invalid provider grammar ({type(exc).__name__})") from None

    @classmethod
    def _parse(cls, data: Mapping[str, Any]) -> Grammar:
        tables = {
            "credential": {"flags_in", "username", "password", "flagged", "first_separator",
                           "separator", "rotation_suffix", "reserved"},
            "account": {"required", "pattern", "strip_prefixes"},
            "gateway": {"host", "hosts", "default", "host_override", "ports", "port_override",
                        "rotating_ports"},
            "session": {"flag", "mode", "ports", "generate", "length", "no_session_policy",
                        "ttl_flag", "default_ttl_seconds"},
        }
        for table, keys in tables.items():
            if set(data.get(table, {})) - keys:
                raise ValueError(f"Unknown keys in grammar table {table}")
        allowed = {"schema_version", "id", "product", "version", "docs", "credential",
                   "account", "gateway", "flags", "session", "pools", "extra_flags"}
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
            for key in ("requires", "conflicts", "choices"):
                if key in spec:
                    spec[key] = tuple(spec[key])
            if "min" in spec:
                spec["minimum"] = spec.pop("min")
            if "max" in spec:
                spec["maximum"] = spec.pop("max")
            flags[name] = Flag(name=name, kind=kind, **spec)
        names = set(flags)
        for flag in flags.values():
            if set(flag.requires + flag.conflicts) - names:
                raise ValueError(f"Grammar {provider} {flag.name} references an unknown flag")
            if flag.kind != "session" and "session" in flag.requires + flag.conflicts:
                raise ValueError("Use requires_session for flags that need a session")
            if flag.case not in (None, "lower", "upper") or type(flag.scale) is not int:
                raise ValueError(f"Grammar {provider} {flag.name} has an invalid rendering")
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
        if session_flag is not None and flags[session_flag].kind != "session":
            raise ValueError("Grammar session flag must have kind session")
        generate = session.get("generate")
        if generate not in (None, *ALPHABETS):
            raise ValueError("Unknown grammar session alphabet")
        policy = session.get("no_session_policy", "rotate")
        if policy not in ("rotate", "gateway"):
            raise ValueError("Grammar default must be rotate or gateway")
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
            id=provider, product=data.get("product", "residential"),
            version=str(data.get("version", "1")), target=cred["flags_in"],
            username=cred.get("username", "{account}"), password=cred.get("password", "{password}"),
            flagged=cred.get("flagged"), first_separator=cred.get("first_separator",
                                                                cred.get("separator", "-")),
            separator=cred.get("separator", "-"), rotation_suffix=cred.get("rotation_suffix"),
            account_required=account.get("required", True),
            account_pattern=account.get("pattern", r"[A-Za-z0-9_]+"),
            strip_prefixes=tuple(account.get("strip_prefixes", ())),
            hosts=MappingProxyType(hosts), default_host=default_host,
            host_override=gateway.get("host_override", False),
            ports=MappingProxyType(ports), port_override=gateway.get("port_override", False),
            rotating_ports=MappingProxyType({
                scheme: _range(value, "rotating_ports")
                for scheme, value in gateway.get("rotating_ports", {}).items()
            }),
            flags=MappingProxyType(flags), session=session_flag, session_mode=mode,
            session_ports=(_range(session["ports"], "session ports") if mode == "port" else None),
            generate=generate if mode == "flag" else "digits",
            generate_length=session.get("length", 16), no_session_policy=policy,
            ttl=session.get("ttl_flag"), ttl_seconds=60,  # Canonical ttl is in minutes.
            default_ttl_seconds=session.get("default_ttl_seconds"),
            pools=MappingProxyType({
                name: frozenset(rule.get("forbid", ()))
                for name, rule in data.get("pools", {}).items()
            }),
            extra_flags=data.get("extra_flags", False),
            reserved=frozenset(cred.get("reserved", ())) | {
                flag.key for flag in flags.values()
            },
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


def grammar_ids() -> tuple[str, ...]:
    root = resources.files(__package__).joinpath("grammars")
    return tuple(sorted(item.name.removesuffix(".toml") for item in root.iterdir()
                        if item.name.endswith(".toml")))


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

    def __reduce__(self):
        # Spawned HTTP workers rebuild the config through validation.
        return (_rebuild, (self.provider, self.account, self.password, dict(self.parameters),
                           dict(self.extra_flags), self.scheme, self.gateway, self.host,
                           self.port, self.session_policy, self.pool))

    @property
    def id(self) -> str:
        return self.provider

    @property
    def grammar(self) -> Grammar:
        return load_grammar(self.provider)

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
                g.target == "username" and g.separator in account
                and set(account.lower().split(g.separator)) & g.reserved
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
        if set(self.parameters) - set(g.flags) - ({"session"} if g.session_mode == "port"
                                                   else set()):
            raise ValueError(f"Unknown {name} parameter")
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
            if isinstance(value, str):
                self._check_delimiters(g, value, name, key)
        session_key = g.session or "session"
        policy = self.session_policy or ("fixed" if session_key in params
                                          else g.no_session_policy)
        if policy not in POLICIES:
            raise ValueError(f"Unknown {name} session policy")
        if (policy == "fixed") != (session_key in params):
            raise ValueError("Only fixed sessions accept and require a supplied session")
        if policy in GENERATED and g.generate is None:
            raise ValueError(f"{name} sessions cannot be generated; supply a session")
        if g.session is None and g.session_mode == "flag" and policy not in ("rotate", "gateway"):
            raise ValueError(f"{name} does not support sessions")
        if policy == "gateway" and g.no_session_policy != "gateway" and not g.rotating_ports:
            raise ValueError(f"{name} rotates per request; use the rotate policy")
        if policy == "rotate" and g.no_session_policy == "gateway" and not g.rotating_ports:
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
            if on_rotating and (sticky or any(
                g.flags[k].requires_session or g.flags[k].sticky_ports_only
                for k in params if k in g.flags
            )):
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
            if not g.host_override or not isinstance(self.host, str) or not HOST.fullmatch(
                self.host
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

    def _check_delimiters(self, g: Grammar, value: str, name: str, key: str) -> None:
        # Values may contain the separator (e.g. hyphenated Shifter sids or Byteful's
        # new_york) but must not smuggle another flag into the credential.
        words = {re.split(r"[^a-z0-9]", token)[0] for token in value.lower().split(g.separator)}
        if g.separator in value and words & g.reserved:
            raise ValueError(f"{name} {key} contains an ambiguous flag delimiter")

    def _extras(self, g: Grammar, params: Mapping[str, Any], name: str) -> dict[str, str]:
        if self.extra_flags and not g.extra_flags:
            raise ValueError(f"Unsupported {name} extra flags; use documented parameters")
        extras = {}
        for key, value in self.extra_flags.items():
            if (not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", key)
                    or key in g.flags or key in g.reserved or key in ("account", "password")):
                raise ValueError(f"Invalid or reserved {name} extra flag name")
            if value is None:
                continue
            value = str(value).lower() if type(value) is bool else value
            value = str(value) if type(value) is int else value
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_]+", value):
                raise ValueError(f"Invalid {name} extra flag value")
            self._check_delimiters(g, value, name, key)
            session = str(params.get(g.session or "", ""))
            if any(re.search(rf"(?:^|{re.escape(g.separator)}){key}(?:{re.escape(g.separator)}|$)",
                             item.lower()) for item in (self.account or "", session)):
                raise ValueError(f"{name} extra flag conflicts with an authentication token")
            extras[key] = value
        return extras

    def _generated(self, g: Grammar, context: RequestContext) -> str | int:
        scope = {"per_request": context.request_index, "per_worker": context.worker_index,
                 "per_run": 0}[self.session_policy]
        seed = f"{context.run_id}:{self.session_policy}:{scope}".encode()
        number = int.from_bytes(hashlib.sha256(seed).digest(), "big")
        if g.session_mode == "port":
            low, high = g.session_ports
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
            port = g.session_ports[0] + params.pop("session")
        pieces = [rendered for key, flag in g.flags.items() if key in params
                  if (rendered := flag.render(params[key])) is not None]
        pieces.extend(f"{key}{g.separator}{value}" for key, value in sorted(
            self.extra_flags.items()))
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
        ttl = None
        if self.session_policy not in ("rotate", "gateway"):
            ttl = (self.parameters[g.ttl] * g.ttl_seconds if g.ttl in self.parameters
                   else g.default_ttl_seconds)
        return {
            "proxy_protocol": self.scheme, "provider": self.provider, "product": g.product,
            "requested_country": (
                self.parameters["country"].lower() if "country" in self.parameters else None
            ),
            "provider_config": {
                "adapter": self.provider, "version": g.version,
                "gateway": {"host": self.host, "port": self.port}, "pool": self.pool,
                "parameters": params, "session_policy": self.session_policy,
                "effective_ttl_seconds": ttl,
                # Future flags may contain credentials. Values are private by default.
                "extra_flags": dict.fromkeys(self.extra_flags, "[redacted]"),
            },
        }


def _rebuild(*args: Any) -> GrammarProxy:
    return GrammarProxy(*args)
