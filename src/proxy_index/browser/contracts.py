"""The browser pulse's frozen contracts K-BP1, K-BP2, K-BP4, K-BP5, K-BP7 (docs/browser.html).

Every browser module reads its names, outcomes, formats and ingest rules from here; changing one
is a contract change. No I/O. Errors name the field, never the value (it can be a credential).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..models import VERDICTS, ErrorCode, VerdictName

# K-BP1 and K-BP5: the engine, the batch kind and the one runner region of the browser hosts.
BROWSER_ENGINE = "browser"
BROWSER_KIND = "browser"
BROWSER_REGION = "browser"
BROWSER_REGION_ID = 20
BROWSER_REGION_CLOUD = "local"
BROWSER_REGION_PATTERN = r"browser(-[a-z0-9]+)?"
# The logical name a host registers with (browser-1, browser-2, ...), never its machine name.
HOST_NAME_PATTERN = r"browser-[a-z0-9]+"

AMAZON_PDP = "amazon_pdp"
GOOGLE_SEARCH = "google_search"
LINKEDIN_PUBLIC = "linkedin_public"
INSTAGRAM_WEB = "instagram_web"
CLOUDFLARE_CHALLENGE = "cloudflare_challenge"
AI_ANSWERS = "ai_answers"
AI_OVERVIEW = "ai_overview"
AI_CHATGPT = "ai_chatgpt"
AI_GEMINI = "ai_gemini"
# A group target is computed over its sources and never scheduled itself.
AI_SOURCES = (AI_OVERVIEW, AI_CHATGPT, AI_GEMINI)
TARGET_GROUPS = {AI_ANSWERS: AI_SOURCES}
PAGE_TARGETS = (AMAZON_PDP, GOOGLE_SEARCH, LINKEDIN_PUBLIC, INSTAGRAM_WEB, CLOUDFLARE_CHALLENGE)
# The targets a host runs, and all nine in the K-BP1 sort order.
RUN_TARGETS = (*PAGE_TARGETS, *AI_SOURCES)
BROWSER_TARGETS = (*PAGE_TARGETS, AI_ANSWERS, *AI_SOURCES)


# K-BP2: the owner's outcomes, as the (verdict, error codes) a browser try records.
OUTCOMES: dict[str, tuple[VerdictName, tuple[ErrorCode | None, ...]]] = {
    "success": ("ok", (None,)),
    "challenge": ("challenge", ("challenge",)),
    "blocked": ("block", ("blocked",)),
    "login_wall": ("error", ("login_wall",)),
    "timeout": ("timeout", ("timeout",)),
    "proxy_error": ("proxy_error", ("proxy_connect", "proxy_auth", "connection", "dns")),
    "no_content": ("error", ("no_content",)),
    "no_answer": ("error", ("no_answer",)),
    "browser_error": ("error", ("browser_error",)),
    "tls": ("error", ("tls",)),
}
# Every error code a browser try may record, with the one verdict it implies.
CODE_VERDICTS: dict[str, VerdictName] = {
    code: verdict for verdict, codes in OUTCOMES.values() for code in codes if code
}
# Firefox network errors by exact name; the exact names win over the prefixes below.
FIREFOX_ERRORS: dict[str, ErrorCode] = {
    "NS_ERROR_UNKNOWN_PROXY_HOST": "proxy_connect",
    "NS_ERROR_PROXY_AUTHENTICATION_FAILED": "proxy_auth",
    "NS_ERROR_NET_RESET": "connection",
    "NS_ERROR_NET_INTERRUPT": "connection",
    "NS_ERROR_CONNECTION_REFUSED": "connection",
    "NS_ERROR_UNKNOWN_HOST": "dns",
    "NS_ERROR_NET_TIMEOUT": "timeout",
    "TimeoutError": "timeout",  # Playwright's navigation and wait timeouts
    "NS_ERROR_NET_INADEQUATE_SECURITY": "tls",
}
FIREFOX_ERROR_PREFIXES: tuple[tuple[str, ErrorCode], ...] = (
    ("NS_ERROR_PROXY_", "proxy_connect"),
    ("SEC_ERROR_", "tls"),
    ("SSL_ERROR_", "tls"),
)
# A main document answered with 407 is the gateway refusing the credentials: proxy_auth.
PROXY_AUTH_STATUS = 407


def classify_navigation_error(name: str | None) -> tuple[ErrorCode, VerdictName]:
    """(code, verdict) of a failed navigation by the browser's error name; else browser_error."""
    name = name if isinstance(name, str) else ""
    code = FIREFOX_ERRORS.get(name)
    if code is None:
        prefixed = (mapped for start, mapped in FIREFOX_ERROR_PREFIXES if name.startswith(start))
        code = next(prefixed, "browser_error")
    return code, CODE_VERDICTS[code]


# Shared field checks (the settings loader uses them too). `what` is the field's path.
# A record never carries page text, answers, cookies or credentials: those names are refused.
FORBIDDEN_FIELDS = frozenset(
    "text answer cookies username password credentials proxy token".split()
)


def check_fields(
    document: Any, names: tuple[str, ...], what: str, optional: tuple[str, ...] = ()
) -> dict[str, Any]:
    """The named fields of an object, required ones first; unknown fields are refused."""
    if not isinstance(document, dict):
        raise ValueError(f"{what}: must be a table or object")
    if forbidden := sorted(FORBIDDEN_FIELDS & set(document)):
        raise ValueError(f"{what}: must never hold {forbidden}")
    if unknown := sorted(set(document) - {*names, *optional}):
        raise ValueError(f"{what}: unknown fields {unknown}")
    if missing := [name for name in names if name not in document]:
        raise ValueError(f"{what}: missing fields {missing}")
    return {name: document[name] for name in (*names, *optional) if name in document}


def check_int(value: Any, what: str, low: int = 0, high: int = 2**63 - 1) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{what}: must be an integer from {low} to {high}")
    return value


def check_string(value: Any, what: str, pattern: str = "", form: str = "", size=512) -> str:
    """A non-blank string of at most `size` characters, matching `pattern` when given."""
    if not isinstance(value, str) or not 0 < len(value.strip()) <= size:
        raise ValueError(f"{what}: must be a non-blank string of at most {size} characters")
    if pattern and not re.fullmatch(pattern, value):
        raise ValueError(f"{what}: must be {form or 'in the documented form'}")
    return value


def optional(check: Callable[..., Any], value: Any, what: str, *args: Any) -> Any:
    return None if value is None else check(value, what, *args)


# K-BP4: names of the bundle, its outbox files and its Spaces object.
PART = r"[a-z0-9_-]+"
# A network as the settings file writes it.
NETWORK = rf"{PART}/{PART}"
RUN_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
SHA256 = r"[0-9a-f]{64}"
COUNTRY = r"[A-Z]{2}"
ARCHIVE_PREFIX = "browser-files"
# Top-level prefixes of other archives (ARCHIVE-1's raw rows and files, CALC-INC's inputs).
RESERVED_PREFIXES = frozenset({"raw", "raw-files", "inputs"})
UNTARGETED = "untargeted"
MAX_TRIES = 10_000
SCREENSHOT_QUALITY = 60
GZIP_LEVEL = 6


def network_slug(provider: str, product: str) -> str:
    """`<provider>.<product>`, lower-cased: the network segment of keys and host paths."""
    parts = [value.lower() if isinstance(value, str) else "" for value in (provider, product)]
    if not all(re.fullmatch(PART, part) for part in parts):
        raise ValueError("network: provider and product are letters, digits, '_' or '-'")
    return ".".join(parts)


def slot_stamp(slot: datetime) -> str:
    """A slot start as `YYYY-MM-DDTHHMMZ` (UTC). Slots start on whole minutes."""
    if not isinstance(slot, datetime) or slot.utcoffset() is None:
        raise ValueError("slot: must be a timezone-aware datetime")
    slot = slot.astimezone(UTC)
    if slot.second or slot.microsecond:
        raise ValueError("slot: must start on a whole minute")
    return slot.strftime("%Y-%m-%dT%H%MZ")


@dataclass(frozen=True, slots=True)
class BundleMembers:
    """The members a browser bundle adds to the node's run directory."""

    manifest: str = "browser/manifest.json"
    tries: str = "browser/tries.jsonl"
    screenshots: str = "browser/screenshots"

    def screenshot(self, i: int) -> str:
        """The screenshot of try i (zero-based): `browser/screenshots/NNNN.jpg`."""
        return f"{self.screenshots}/{check_int(i, 'i', 0, MAX_TRIES - 1):04d}.jpg"

    def is_member(self, name: str) -> bool:
        screenshot = re.escape(self.screenshots) + r"/\d{4}\.jpg"
        return name in (self.manifest, self.tries) or re.fullmatch(screenshot, name) is not None


# bundle_members() is the member layout every bundle uses.
bundle_members = BundleMembers


def outbox_paths(root: str | Path, run_id: str) -> tuple[Path, Path]:
    """The bundle `<root>/<run_id>.tar.gz` and its sidecar `<root>/<run_id>.json`."""
    run_id = check_string(run_id, "run_id", RUN_ID, "a lower-case UUID")
    return Path(root) / f"{run_id}.tar.gz", Path(root) / f"{run_id}.json"


def object_key(prefix: str, network: str, target: str, country: str | None, slot: datetime) -> str:
    """The Spaces key `<prefix>/<network>/<target>/<cc>/<YYYY-MM-DDTHHMMZ>.tar.gz`; a run that
    requested no country (country None, an untargeted network, D11) gets cc `untargeted`."""
    check_string(prefix, "prefix", rf"{PART}(/{PART})*", "lower-case path segments")
    if prefix.split("/")[0] in RESERVED_PREFIXES:
        raise ValueError("prefix: must not share another archive's top level")
    check_string(network, "network", rf"{PART}\.{PART}", "a network_slug()")
    if target not in RUN_TARGETS:
        raise ValueError("target: not a browser run target")
    if country is not None:
        country = check_string(country, "country", "[A-Za-z]{2}", "an alpha-2 code").lower()
    return f"{prefix}/{network}/{target}/{country or UNTARGETED}/{slot_stamp(slot)}.tar.gz"


# K-BP4 records.
TIMINGS = ("queue", "context", "navigate", "settle", "total")
TOKEN = r"[a-z0-9_]{1,64}"
# The settings' table inputs (K-BP3): instagram_web and cloudflare_challenge.
INPUT_TABLES = (("site", "handle"), ("url", "expect"))
TRY_FIELDS = tuple(
    "i input source verdict error"
    " status final_url timings_ms bytes_down_est reason screenshot".split()
)


def page_url(url: str) -> str:
    """The scheme, host (and port) and path of a page URL: no credentials, query or fragment."""
    parts = urlsplit(check_string(url, "final_url", size=8192))
    host, port = parts.hostname, parts.port
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("final_url: must be an http or https URL")
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}{f':{port}' if port else ''}{parts.path or '/'}"


def _input(value: Any) -> str | dict[str, str]:
    """A try's input as the settings give it: a string, or one of the INPUT_TABLES tables."""
    if not isinstance(value, Mapping):
        return check_string(value, "input", size=2048)
    names = next((n for n in INPUT_TABLES if set(value) == set(n)), INPUT_TABLES[0])
    table = check_fields(dict(value), names, "input")
    return {name: check_string(text, f"input.{name}", size=2048) for name, text in table.items()}


def try_record(**fields: Any) -> dict[str, Any]:
    """One validated tries.jsonl line (K-BP4); every field is required, absent values None. The
    verdict is the one its error code implies (ok: no error); a screenshot is the try's own."""
    record = check_fields(fields, TRY_FIELDS, "try")
    check_int(record["i"], "i", 0, MAX_TRIES - 1)
    record["input"] = _input(record["input"])
    optional(check_string, record["source"], "source", TOKEN)
    optional(check_string, record["error"], "error", TOKEN)
    if record["verdict"] != (CODE_VERDICTS.get(record["error"]) if record["error"] else "ok"):
        raise ValueError("verdict: must be the one its error code implies")
    optional(check_int, record["status"], "status", 100, 599)
    record["final_url"] = optional(lambda url, _: page_url(url), record["final_url"], "final_url")
    timings = check_fields(record["timings_ms"], TIMINGS, "timings_ms")
    for key, ms in timings.items():
        optional(check_int, ms, f"timings_ms.{key}", 0, 86_400_000)
    optional(check_int, record["bytes_down_est"], "bytes_down_est")
    optional(check_string, record["reason"], "reason", TOKEN)
    shot = record["screenshot"]
    if shot is not None and shot != bundle_members().screenshot(record["i"]):
        raise ValueError("screenshot: must be this try's own member")
    return record


MANIFEST_FORMAT = "proxy-index-browser-raw"
MANIFEST_VERSION = 1
CAMOUFOX_FIELDS = ("release", "engine_version", "user_agent")
MANIFEST_FIELDS = tuple(
    "format version run_id batch_id network target country requested_country geo_targeting"
    " slot started_at finished_at tries outcomes settings_sha256 camoufox host files".split()
)


def _timestamp(value: Any, what: str) -> str:
    """An ISO 8601 time with a zero UTC offset; a datetime is written out first."""
    try:
        value = value.isoformat() if isinstance(value, datetime) else value
        offset = datetime.fromisoformat(value).utcoffset()
    except (TypeError, ValueError):
        raise ValueError(f"{what}: must be an ISO 8601 time") from None
    if offset is None or offset.total_seconds():
        raise ValueError(f"{what}: must be in UTC")
    return value


def _outcomes(value: Any, tries: int) -> dict[str, dict[str, int]]:
    """Counts by verdict and by error code, zeros included; each failed try has one code."""
    outcomes = check_fields(value, ("verdicts", "errors"), "outcomes")
    verdicts = check_fields(outcomes["verdicts"], VERDICTS, "outcomes.verdicts")
    errors = check_fields(outcomes["errors"], tuple(CODE_VERDICTS), "outcomes.errors")
    for name, count in (verdicts | errors).items():
        check_int(count, f"outcomes.{name}", 0, MAX_TRIES)
    if sum(verdicts.values()) != tries:
        raise ValueError("outcomes.verdicts: must add up to tries")
    for verdict in VERDICTS:
        coded = sum(n for code, n in errors.items() if CODE_VERDICTS[code] == verdict)
        if verdict != "ok" and coded != verdicts[verdict]:
            raise ValueError("outcomes.errors: must add up to each failed verdict")
    return {"verdicts": verdicts, "errors": errors}


def _files(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("files: must be a list")
    members, names = bundle_members(), []
    for entry in value:
        file = check_fields(entry, ("name", "bytes", "sha256"), "files[]")
        name = check_string(file["name"], "files[].name", size=64)
        if name == members.manifest or not members.is_member(name) or name in names:
            raise ValueError("files: must list each bundle member once, not the manifest")
        check_int(file["bytes"], "files[].bytes")
        check_string(file["sha256"], "files[].sha256", SHA256, "a lower-case SHA-256")
        names.append(name)
    if members.tries not in names:
        raise ValueError("files: must list browser/tries.jsonl")
    return value


def _identity(doc: dict[str, Any]) -> None:
    check_string(doc["run_id"], "run_id", RUN_ID, "a lower-case UUID")
    check_int(doc["batch_id"], "batch_id", 1)
    network = check_fields(doc["network"], ("provider", "product"), "network")
    network_slug(network["provider"], network["product"])
    if doc["target"] not in RUN_TARGETS:
        raise ValueError("target: not a browser run target")
    check_string(doc["country"], "country", COUNTRY, "an upper-case alpha-2 code")
    requested = doc["requested_country"]
    optional(check_string, requested, "requested_country", COUNTRY, "an alpha-2 code")
    if type(doc["geo_targeting"]) is not bool:
        raise ValueError("geo_targeting: must be true or false")
    # An untargeted run (D11) requests no country; a targeted one requests its batch's.
    if doc["geo_targeting"] != (requested is not None) or requested not in (None, doc["country"]):
        raise ValueError("requested_country: disagrees with geo_targeting or country")
    check_string(doc["host"], "host", HOST_NAME_PATTERN, "a logical host name", 64)


def check_manifest(document: Any) -> dict[str, Any]:
    """A validated browser/manifest.json document (K-BP4), as read back from a bundle."""
    doc = check_fields(document, MANIFEST_FIELDS, "manifest")
    version = doc["version"]
    if doc["format"] != MANIFEST_FORMAT or type(version) is not int or version != MANIFEST_VERSION:
        raise ValueError("manifest: not a version 1 browser manifest")
    _identity(doc)
    for key in ("slot", "started_at", "finished_at"):
        doc[key] = _timestamp(doc[key], key)
    slot_stamp(datetime.fromisoformat(doc["slot"]))
    if datetime.fromisoformat(doc["started_at"]) > datetime.fromisoformat(doc["finished_at"]):
        raise ValueError("started_at: must not be after finished_at")
    doc["outcomes"] = _outcomes(doc["outcomes"], check_int(doc["tries"], "tries", 0, MAX_TRIES))
    check_string(doc["settings_sha256"], "settings_sha256", SHA256, "a lower-case SHA-256")
    camoufox = check_fields(doc["camoufox"], CAMOUFOX_FIELDS, "camoufox")
    for key, text in camoufox.items():
        check_string(text, f"camoufox.{key}")
    if user_agent_major(camoufox["user_agent"]) != engine_major(camoufox["engine_version"]):
        raise ValueError("camoufox.user_agent: Firefox version must equal the engine's major")
    doc["files"] = _files(doc["files"])
    return doc


def manifest(**fields: Any) -> dict[str, Any]:
    """Build a validated manifest; format and version are filled in. Times may be datetimes."""
    return check_manifest({"format": MANIFEST_FORMAT, "version": MANIFEST_VERSION} | fields)


# K-BP7: the ingest endpoint's headers, reply fields and error codes.
CONTENT_TYPE = "application/gzip"
HEADER_SHA256 = "X-Content-SHA256"
HEADER_BUNDLE_ID = "X-Bundle-Id"
REPLY_INGESTED = "ingested"
REPLY_RUN_KEY = "run_key"
REPLY_ARCHIVED = "archived"
REPLY_ARCHIVED_KEY = "key"
REPLY_ARCHIVED_SHA256 = "sha256"
ERROR_CHECKSUM_MISMATCH = "checksum_mismatch"
ERROR_BUNDLE_CHANGED = "bundle_changed"
ALERT_BUNDLE_CHANGED = "browser_bundle_changed"
# Statuses the host's outbox retries with backoff; every other non-200 is kept and marked.
RETRY_STATUSES = frozenset({429, 503})


def confirmed(reply: tuple[int, Any], local_sha256: str) -> bool:
    """Whether the host may delete its bundle, from the reply's status and parsed JSON body:
    only a 200 that archived exactly the host's bytes (equal sha256) or says `ingested: false`
    (run killed or skipped). Anything else, a missing or malformed field too, keeps the file."""
    status, body = reply
    if type(status) is not int or status != 200 or not isinstance(body, dict):
        return False
    ingested = body.get(REPLY_INGESTED)
    if ingested is False:
        return True
    archived = body.get(REPLY_ARCHIVED)
    remote = archived.get(REPLY_ARCHIVED_SHA256) if isinstance(archived, dict) else None
    local_ok = isinstance(local_sha256, str) and re.fullmatch(SHA256, local_sha256) is not None
    return ingested is True and local_ok and remote == local_sha256


# D7: the user agent a host reports. Its Firefox version is the engine's major version.
UA_PLATFORMS = {"macos": "Macintosh; Intel Mac OS X 10.15", "linux": "X11; Linux x86_64"}


def engine_major(engine_version: str) -> int:
    """The major version of a Firefox engine version such as `135.0.1`."""
    check_string(engine_version, "engine_version", r"\d{1,4}(\.[0-9A-Za-z]+){0,4}", "135.0.1")
    return int(engine_version.split(".")[0])


def user_agent_major(user_agent: str) -> int | None:
    """The Firefox major version a user agent claims, or None when it claims none."""
    match = re.search(r"\bFirefox/(\d{1,4})\.", user_agent) if isinstance(user_agent, str) else None
    return int(match.group(1)) if match else None


def user_agent(engine_version: str, os: str) -> str:
    """The standard Firefox user agent of this engine on this OS (macos or linux)."""
    major = engine_major(engine_version)
    if os not in UA_PLATFORMS:
        raise ValueError(f"os: must be one of {sorted(UA_PLATFORMS)}")
    return f"Mozilla/5.0 ({UA_PLATFORMS[os]}; rv:{major}.0) Gecko/20100101 Firefox/{major}.0"
