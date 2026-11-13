"""The browser pulse's frozen contracts K-BP1, K-BP2, K-BP4, K-BP5, K-BP7 (docs/browser.html).

Every browser module reads its names, outcomes, formats and ingest rules from here; changing one
is a contract change. No I/O. Errors name the field, never the value (it can be a credential).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..models import VERDICTS, ErrorCode, VerdictName
from .engines import CAMOUFOX, ENGINES, check_engine
from .field_checks import (  # noqa: F401 (callers import them here)
    FORBIDDEN_FIELDS,
    check_fields,
    check_int,
    check_string,
    optional,
)
from .net_errors import (  # noqa: F401 (callers import the Firefox tables here)
    ERROR_PREFIXES,
    ERRORS,
    FIREFOX_ERROR_PREFIXES,
    FIREFOX_ERRORS,
)

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
# A main document answered with 407 is the gateway refusing the credentials: proxy_auth.
PROXY_AUTH_STATUS = 407


def classify_navigation_error(name: str | None) -> tuple[ErrorCode, VerdictName]:
    """(code, verdict) of a failed navigation by the browser's error name (Firefox's, or
    Chrome's with or without `net::`; net_errors.py); else browser_error."""
    name = name if isinstance(name, str) else ""
    name = name.removeprefix("net::")
    code = ERRORS.get(name)
    if code is None:
        prefixed = (mapped for start, mapped in ERROR_PREFIXES if name.startswith(start))
        code = next(prefixed, "browser_error")
    return code, CODE_VERDICTS[code]


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
    # The run's procstats samples, one per line, then {"summary": procstats.summary()}.
    samples: str = "browser/samples.jsonl"

    def screenshot(self, i: int) -> str:
        """The screenshot of try i (zero-based): `browser/screenshots/NNNN.jpg`."""
        return f"{self.screenshots}/{check_int(i, 'i', 0, MAX_TRIES - 1):04d}.jpg"

    def is_member(self, name: str) -> bool:
        screenshot = re.escape(self.screenshots) + r"/\d{4}\.jpg"
        fixed = (self.manifest, self.tries, self.samples)
        return name in fixed or re.fullmatch(screenshot, name) is not None


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
# A Chrome try's optional `fingerprint` (chrome.Fingerprint.record()): values and their SHA-256.
FINGERPRINT_FIELDS = tuple(
    "screen_width screen_height device_scale_factor locale accept_language timezone_id"
    " hardware_concurrency device_memory".split()
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


def _fingerprint(value: Any) -> dict[str, Any]:
    fingerprint = check_fields(value, ("values", "sha256"), "fingerprint")
    values = check_fields(fingerprint["values"], FINGERPRINT_FIELDS, "fingerprint.values")
    for key, item in values.items():
        check = check_int if type(item) is int else check_string
        check(item, f"fingerprint.values.{key}")
    text = json.dumps(values, sort_keys=True, separators=(",", ":"))
    if fingerprint["sha256"] != hashlib.sha256(text.encode()).hexdigest():
        raise ValueError("fingerprint.sha256: must be the SHA-256 of the values")
    return {"values": values, "sha256": fingerprint["sha256"]}


def try_record(**fields: Any) -> dict[str, Any]:
    """One validated tries.jsonl line (K-BP4); every field but the `engine` (from manifest v2)
    and a Chrome try's `fingerprint` is required, absent values None. The verdict is the one
    its error code implies (ok: no error); a screenshot is the try's own."""
    record = check_fields(fields, TRY_FIELDS, "try", ("engine", "fingerprint"))
    if "fingerprint" in record:
        record["fingerprint"] = _fingerprint(record["fingerprint"])
    if record.get("engine", CAMOUFOX) not in ENGINES:
        raise ValueError(f"engine: must be one of {list(ENGINES)}")
    check_int(record["i"], "i", 0, MAX_TRIES - 1)
    record["input"] = _input(record["input"])
    optional(check_string, record["source"], "source", TOKEN)
    optional(check_string, record["error"], "error", TOKEN)
    if record["verdict"] != (CODE_VERDICTS.get(record["error"]) if record["error"] else "ok"):
        raise ValueError("verdict: must be the one its error code implies")
    # Any three-digit status: LinkedIn answers its block with 999 (K-BP2 reads it as block).
    optional(check_int, record["status"], "status", 100, 999)
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
# Version 2 adds the `engine` block; its `camoufox` block is a Camoufox bundle's only. Ingest
# reads both versions; a version 1 bundle is a Camoufox one.
MANIFEST_VERSION = 2
MANIFEST_VERSIONS = (1, 2)
CAMOUFOX_FIELDS = ("release", "engine_version", "user_agent")
MANIFEST_FIELDS = tuple(
    "format version run_id batch_id network target country requested_country geo_targeting"
    " slot started_at finished_at tries outcomes settings_sha256 camoufox host files".split()
)
MANIFEST_V2_FIELDS = (*(name for name in MANIFEST_FIELDS if name != "camoufox"), "engine")


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
    """A validated browser/manifest.json document (K-BP4), version 1 or 2, as read back from a
    bundle."""
    version = document.get("version") if isinstance(document, dict) else None
    v1 = type(version) is int and version == 1
    names, extra = (MANIFEST_FIELDS, ()) if v1 else (MANIFEST_V2_FIELDS, ("camoufox",))
    doc = check_fields(document, names, "manifest", extra)
    known = type(version) is int and version in MANIFEST_VERSIONS
    if doc["format"] != MANIFEST_FORMAT or not known:
        raise ValueError("manifest: not a version 1 or 2 browser manifest")
    _identity(doc)
    for key in ("slot", "started_at", "finished_at"):
        doc[key] = _timestamp(doc[key], key)
    slot_stamp(datetime.fromisoformat(doc["slot"]))
    if datetime.fromisoformat(doc["started_at"]) > datetime.fromisoformat(doc["finished_at"]):
        raise ValueError("started_at: must not be after finished_at")
    doc["outcomes"] = _outcomes(doc["outcomes"], check_int(doc["tries"], "tries", 0, MAX_TRIES))
    check_string(doc["settings_sha256"], "settings_sha256", SHA256, "a lower-case SHA-256")
    if "camoufox" in doc:
        doc["camoufox"] = _camoufox(doc["camoufox"])
    if not v1:
        doc["engine"] = check_engine(doc["engine"], doc.get("camoufox"))
    doc["files"] = _files(doc["files"])
    return doc


def _camoufox(value: Any) -> dict[str, str]:
    camoufox = check_fields(value, CAMOUFOX_FIELDS, "camoufox")
    for key, text in camoufox.items():
        check_string(text, f"camoufox.{key}")
    if user_agent_major(camoufox["user_agent"]) != engine_major(camoufox["engine_version"]):
        raise ValueError("camoufox.user_agent: Firefox version must equal the engine's major")
    return camoufox


def manifest_engine(doc: dict[str, Any]) -> str:
    """The engine of a validated manifest: its engine block's, Camoufox for a version 1."""
    return doc["engine"]["name"] if "engine" in doc else CAMOUFOX


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
RETRY_STATUSES = frozenset({408, 429, 503})


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
