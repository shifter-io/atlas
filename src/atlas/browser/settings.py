"""The browser pulse settings file (K-BP3, `schema = 2`; configs/browser-pulse.example.toml).

One private file, passed by path to the planner and every host: targets with their own tries,
interval and inputs (no global budget), and which logical host runs which networks. Errors are
UserError naming the field path, never a value: inputs can be personal data.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn
from urllib.parse import urlsplit

from ..errors import UserError
from ..orchestrator import browser_queue
from ..regions import load_countries, load_targets
from . import contracts as c
from .chrome import DEFAULT_EXECUTABLE
from .contracts import check_fields, check_int, check_string
from .engines import DEFAULT_ENGINE, ENGINES

SCHEMA = 2
SCREENSHOTS = ("failures", "all", "none")
PLANS = ("slots", "queue")
# Playwright resource types a run may block; never the document itself.
RESOURCE_TYPES = frozenset(
    "stylesheet image media font script texttrack"
    " xhr fetch eventsource websocket manifest other".split()
)
MAX_INPUTS = 1000
RELATIVE = r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*"


@dataclass(frozen=True, slots=True)
class Schedule:
    countries: tuple[str, ...]
    lead_seconds: int
    # "slots": each target on its own interval; "queue": once a period, one run per gap per
    # host (orchestrator/browser_queue.py).
    plan: str = "slots"
    queue_period_minutes: int = 1440
    run_deadline_minutes: int = 180
    # The UTC midnight the queue's periods count from; before it the daily queue (browser_queue).
    queue_start: datetime | None = None


@dataclass(frozen=True, slots=True)
class BrowserOptions:
    browsers_per_host: int
    recycle_after: int
    page_timeout_s: int
    headless: bool
    block_resources: tuple[str, ...]
    screenshots: str
    locale: str
    # The engine of every target without its own (K-BP3; engines.py).
    engine: str = DEFAULT_ENGINE
    # A try still running this long past its own longest time (page timeout plus the target's
    # wait) is hung: a timeout, its browser replaced (IDLE-TIMEOUT-1, transport.py).
    idle_timeout_s: float = 120
    # A run whose counters have not moved for this long is stopped and reported 'stalled'.
    stall_minutes: float = 30


@dataclass(frozen=True, slots=True)
class ChromeOptions:
    """The optional [chrome] table: the host's Google Chrome (driver.launch_chrome)."""

    executable: str = DEFAULT_EXECUTABLE


@dataclass(frozen=True, slots=True)
class Outbox:
    dir: str
    max_pending_gb: float


@dataclass(frozen=True, slots=True)
class TargetSettings:
    """One [[target]]: tries per network per run and the interval, both required."""

    id: str
    tries: int
    every_minutes: int
    inputs: tuple[Any, ...]
    timeout_s: int | None = None
    block_resources: tuple[str, ...] | None = None
    challenge_wait_s: int | None = None
    answer_wait_s: int | None = None
    engine: str | None = None
    # The estimated cost of one run, in browser-seconds: the queue's order and load check.
    browser_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class HostSettings:
    name: str
    networks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BrowserSettings:
    schema: int
    enabled: bool
    schedule: Schedule
    browser: BrowserOptions
    outbox: Outbox
    targets: tuple[TargetSettings, ...]
    hosts: tuple[HostSettings, ...]
    untargeted: tuple[str, ...]
    sha256: str
    chrome: ChromeOptions = ChromeOptions()

    def host(self, name: str) -> BrowserSettings:
        """One host's view: these settings with only its [[host]] and its untargeted networks."""
        for host in self.hosts:
            if host.name == name:
                untargeted = tuple(n for n in self.untargeted if n in host.networks)
                return replace(self, hosts=(host,), untargeted=untargeted)
        raise UserError("host: no [[host]] has this name")

    def engine(self, target_id: str) -> str:
        """The engine a run of this target uses: its [[target]]'s, else [browser] engine."""
        own = next((t.engine for t in self.targets if t.id == target_id), None)
        return own or self.browser.engine

    def engines(self) -> tuple[str, ...]:
        """The engines this settings' targets use, in ENGINES order."""
        used = {self.engine(target.id) for target in self.targets}
        return tuple(engine for engine in ENGINES if engine in used)

    def targets_for_planner(self) -> tuple[tuple[str, int, int], ...]:
        """(id, tries, every_minutes) per target, in start order."""
        return tuple((t.id, t.tries, t.every_minutes) for t in self.targets)

    def is_untargeted(self, network: str) -> bool:
        """Whether this network (`provider/product`) runs with no country flag (D11)."""
        return network in self.untargeted


def queue_load(settings: BrowserSettings) -> float | None:
    """In queue mode, the first host's estimated load (the settings check reports it, for
    one host's view): its runs' browser-seconds over its browsers' time; else None."""
    if settings.schedule.plan != "queue":
        return None
    return round(browser_queue.load(settings, settings.hosts[0].name), 3)


def _fail(path: str, message: str) -> NoReturn:
    raise ValueError(f"{path}: {message}")


def _choice(value: Any, path: str, options: Any, form: str = "") -> Any:
    # Of the same type too: true is not 1.
    if type(value) not in {type(option) for option in options} or value not in options:
        _fail(path, f"must be {form or f'one of {sorted(options)}'}")
    return value


def _list(value: Any, path: str, low: int = 1, high: int = MAX_INPUTS) -> list[Any]:
    if not isinstance(value, list) or not low <= len(value) <= high:
        _fail(path, f"must be a list of {low} to {high} entries")
    return value


def _unique(values: list[Any], path: str) -> tuple[Any, ...]:
    for index, value in enumerate(values):
        if value in values[:index]:
            _fail(f"{path}[{index}]", "is listed twice")
    return tuple(values)


def _resources(value: Any, path: str) -> tuple[str, ...]:
    values = _list(value, path, 0, len(RESOURCE_TYPES))
    for index, kind in enumerate(values):
        _choice(kind, f"{path}[{index}]", RESOURCE_TYPES)
    return _unique(values, path)


# instagram_web's one site (X profiles are x_web's plain handles), and each site's handles.
HANDLES = {"instagram": r"[A-Za-z0-9._]{1,30}"}
X_HANDLE = r"[A-Za-z0-9_]{1,15}"


def _social(value: Any, path: str) -> MappingProxyType:
    entry = check_fields(value, ("site", "handle"), path)
    site = _choice(entry["site"], f"{path}.site", HANDLES)
    check_string(entry["handle"], f"{path}.handle", HANDLES[site], "a handle of that site")
    return MappingProxyType(entry)


def _challenge(value: Any, path: str) -> MappingProxyType:
    entry = check_fields(value, ("url", "expect"), path)
    url = urlsplit(check_string(entry["url"], f"{path}.url", size=2048))
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        _fail(f"{path}.url", "must be an https URL without credentials")
    check_string(entry["expect"], f"{path}.expect", size=200)
    return MappingProxyType(entry)


_query = partial(check_string, size=200)
_network = partial(check_string, pattern=c.NETWORK, form="provider/product in lower case")
_asin = partial(check_string, pattern="[0-9A-Z]{10}", form="an ASIN")
_slug = partial(check_string, pattern="[a-z0-9-]{3,100}", form="a profile slug")
_x_handle = partial(check_string, pattern=X_HANDLE, form="an X handle")
# K-BP3 input shapes per run target: (check one input, most inputs allowed).
INPUT_SHAPES = {
    c.AMAZON_PDP: (_asin, MAX_INPUTS),
    c.GOOGLE_SEARCH: (_query, MAX_INPUTS),
    c.LINKEDIN_PUBLIC: (_slug, MAX_INPUTS),
    c.INSTAGRAM_WEB: (_social, MAX_INPUTS),
    c.X_WEB: (_x_handle, MAX_INPUTS),
    c.CLOUDFLARE_CHALLENGE: (_challenge, MAX_INPUTS),
    c.AI_OVERVIEW: (_query, MAX_INPUTS),
    # One fixed question, so every try on every network asks the same thing.
    c.AI_CHATGPT: (_query, 1),
    c.AI_GEMINI: (_query, 1),
}
TARGET_CHECKS = {
    "timeout_s": partial(check_int, low=5, high=600),
    "block_resources": _resources,
    "challenge_wait_s": partial(check_int, low=0, high=600),
    "answer_wait_s": partial(check_int, low=0, high=600),
    "engine": partial(_choice, options=ENGINES),
    "browser_seconds": partial(check_int, low=1, high=1_000_000),
}
TARGET_OPTIONAL = tuple(TARGET_CHECKS)


def _target(value: Any, path: str, known: set[str]) -> TargetSettings:
    entry = check_fields(value, ("id", "tries", "every_minutes", "inputs"), path, TARGET_OPTIONAL)
    if not isinstance(entry["id"], str) or entry["id"] not in known:
        _fail(f"{path}.id", "is not a browser run target in targets.toml")
    check, most = INPUT_SHAPES[entry["id"]]
    inputs = _list(entry["inputs"], f"{path}.inputs", 1, most)
    extra = {k: TARGET_CHECKS[k](v, f"{path}.{k}") for k, v in entry.items() if k in TARGET_CHECKS}
    return TargetSettings(
        entry["id"],
        check_int(entry["tries"], f"{path}.tries", 1, c.MAX_TRIES),
        check_int(entry["every_minutes"], f"{path}.every_minutes", 5, 1440),
        tuple(check(item, f"{path}.inputs[{i}]") for i, item in enumerate(inputs)),
        **extra,
    )


def _hosts(value: Any) -> list[HostSettings]:
    """Every [[host]]; a network runs on at most one host."""
    hosts: list[HostSettings] = []
    for index, entry in enumerate(_list(value, "host", 1, 64)):
        entry = check_fields(entry, ("name", "networks"), f"host[{index}]")
        path = f"host[{index}].name"
        name = check_string(entry["name"], path, c.HOST_NAME_PATTERN, "browser-<n>", 64)
        if name in (host.name for host in hosts):
            _fail(path, "is used by another host")
        networks = _list(entry["networks"], f"host[{index}].networks")
        for i, network in enumerate(networks):
            path = f"host[{index}].networks[{i}]"
            _network(network, path)
            if network in networks[:i] or any(network in host.networks for host in hosts):
                _fail(path, "is listed twice: a network runs on one host")
        hosts.append(HostSettings(name, tuple(networks)))
    return hosts


def _untargeted(value: Any, run: set[str]) -> tuple[str, ...]:
    entry = check_fields(value, (), "networks", ("untargeted",))
    networks = _list(entry.get("untargeted", []), "networks.untargeted", 0)
    for index, network in enumerate(networks):
        if _network(network, f"networks.untargeted[{index}]") not in run:
            _fail(f"networks.untargeted[{index}]", "is a network no host runs")
    return _unique(networks, "networks.untargeted")


def _schedule(value: Any, countries: dict[str, str]) -> Schedule:
    queue = ("plan", "queue_period_minutes", "run_deadline_minutes", "queue_start")
    entry = check_fields(value, ("countries", "lead_seconds"), "schedule", queue)
    codes = _list(entry["countries"], "schedule.countries", 1, len(countries))
    for index, code in enumerate(codes):
        _choice(code, f"schedule.countries[{index}]", countries, "a known alpha-2 code")
    lead = check_int(entry["lead_seconds"], "schedule.lead_seconds", 0, 3600)
    minutes = {
        key: check_int(entry.get(key, default), f"schedule.{key}", 5, 1440)
        for key, default in (("queue_period_minutes", 1440), ("run_deadline_minutes", 180))
    }
    plan = _choice(entry.get("plan", "slots"), "schedule.plan", PLANS)
    start = _midnight(entry["queue_start"]) if "queue_start" in entry else None
    return Schedule(_unique(codes, "schedule.countries"), lead, plan, **minutes, queue_start=start)


def _midnight(value: Any) -> datetime:
    """schedule.queue_start: a TOML date-time at a UTC midnight, such as 2026-11-18T00:00:00Z."""
    offset = value.utcoffset() if isinstance(value, datetime) else None
    if offset != timedelta(0) or value.time() != datetime.min.time():
        _fail("schedule.queue_start", "must be a UTC midnight, such as 2026-11-18T00:00:00Z")
    return value.astimezone(UTC)


def _browser(value: Any) -> BrowserOptions:
    optional = ("engine", "idle_timeout_s", "stall_minutes")
    names = tuple(f.name for f in fields(BrowserOptions) if f.name not in optional)
    entry = check_fields(value, names, "browser", optional)
    engine = _choice(entry.get("engine", DEFAULT_ENGINE), "browser.engine", ENGINES)
    return BrowserOptions(
        check_int(entry["browsers_per_host"], "browser.browsers_per_host", 1, 16),
        check_int(entry["recycle_after"], "browser.recycle_after", 1, c.MAX_TRIES),
        check_int(entry["page_timeout_s"], "browser.page_timeout_s", 5, 600),
        _choice(entry["headless"], "browser.headless", (False, True)),
        _resources(entry["block_resources"], "browser.block_resources"),
        _choice(entry["screenshots"], "browser.screenshots", SCREENSHOTS),
        check_string(entry["locale"], "browser.locale", "[a-z]{2,3}(-[A-Z]{2})?", "like en-US"),
        engine,
        check_int(entry.get("idle_timeout_s", 120), "browser.idle_timeout_s", 10, 1800),
        check_int(entry.get("stall_minutes", 30), "browser.stall_minutes", 5, 180),
    )


def _chrome(value: Any) -> ChromeOptions:
    entry = check_fields(value, (), "chrome", ("executable",))
    executable = entry.get("executable", DEFAULT_EXECUTABLE)
    return ChromeOptions(check_string(executable, "chrome.executable", "/.*", "an absolute path"))


def _outbox(value: Any) -> Outbox:
    entry = check_fields(value, ("dir", "max_pending_gb"), "outbox")
    # Relative to the host state directory, with no '.' or '..' segment.
    check_string(entry["dir"], "outbox.dir", RELATIVE, "a relative path", 200)
    size = entry["max_pending_gb"]
    if type(size) not in (int, float) or not 0 < size <= 10_000:
        _fail("outbox.max_pending_gb", "must be a number above 0 and at most 10000")
    return Outbox(entry["dir"], float(size))


def settings_sha256(path: str | Path) -> str:
    """SHA-256 of the settings file's bytes, recorded in every bundle manifest."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _parse(raw: bytes, targets_path: Any, countries_path: Any) -> BrowserSettings:
    top = ("schema", "enabled", "schedule", "browser", "outbox", "target", "host")
    optional = ("networks", "chrome")
    document = check_fields(tomllib.loads(raw.decode("utf-8")), top, "settings", optional)
    if type(document["schema"]) is not int or document["schema"] != SCHEMA:
        _fail("schema", f"must be {SCHEMA}")
    rows = load_targets(targets_path)
    known = {r["target_id"] for r in rows if r["engine"] == c.BROWSER_ENGINE} & set(INPUT_SHAPES)
    entries = _list(document["target"], "target")
    targets = tuple(_target(entry, f"target[{i}]", known) for i, entry in enumerate(entries))
    _unique([target.id for target in targets], "target")
    hosts = _hosts(document["host"])
    untargeted = _untargeted(document.get("networks", {}), {n for h in hosts for n in h.networks})
    browser, outbox = _browser(document["browser"]), _outbox(document["outbox"])
    schedule = _schedule(document["schedule"], load_countries(countries_path))
    enabled = _choice(document["enabled"], "enabled", (False, True))
    sha256 = hashlib.sha256(raw).hexdigest()
    settings = BrowserSettings(
        SCHEMA,
        enabled,
        schedule,
        browser,
        outbox,
        targets,
        tuple(hosts),
        untargeted,
        sha256,
        _chrome(document.get("chrome", {})),
    )
    if schedule.plan == "queue":
        browser_queue.check(settings)
    return settings


def load_settings(
    path: str | Path, *, targets_path: str | Path | None = None, countries_path=None
) -> BrowserSettings:
    """The checked settings file. targets_path and countries_path default to configs/."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise UserError("settings: the file cannot be read") from None
    try:
        return _parse(raw, targets_path, countries_path)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise UserError("settings: the file is not valid UTF-8 TOML") from None
    except ValueError as error:
        raise UserError(str(error)) from None
