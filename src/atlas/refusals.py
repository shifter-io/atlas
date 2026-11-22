"""Samples of refused answers (REFUSAL-RECORD-1): what a non-200 answer carried, so who refused
(the network's gateway, the target, or a CDN in front of it) can be told afterwards.

For every non-200 answer the HTTP transport receives, the target's answer or the gateway's answer
to our CONNECT, a run keeps at most SAMPLES_PER_KIND of each kind, a kind being how the answer
arrived (LEGS) and its status, and at most MAX_KINDS kinds. A sample holds the status, how it
arrived, the error code, every response header (at most MAX_HEADERS, each cut to HEADER_CHARS),
the first BODY_BYTES of the body as the transport read it (it reads at most its target's
max_body_bytes + 1, 513 bytes for the echo target; reading more would change measured bytes and
latency), the body's length read, the exit IP when known and the wall time. Credential-like
values are redacted before anything is kept: the headers of SECRET_HEADERS or naming a
SECRET_WORDS word, Set-Cookie values (their names stay), Basic/Bearer/Digest credentials, and the
request's proxy login wherever it appears, body included.

The transport captures an answer only while its own process has sampled fewer of its kind than
the quota, so every other answer costs one counter. A sample changes no verdict, counter,
latency or summary field: the run only keeps it beside them.

A run with samples writes them to FILE in its run directory, one JSON object per line with the
run's identifiers (JsonSink.refusals; a daily test through the scheduler's summary.json and
`daily ingest`'s conversion). The file rides the run's upload into the raw spool (ARCHIVE-RAW-1:
30 days on the database server, then the private archive like every raw file) and is never
imported into the database. `atlas archive refusals` reads it from the spool (spooled()).
"""

from __future__ import annotations

import gzip
import json
import re
import tarfile
import time
from collections.abc import Iterable, Iterator
from datetime import date
from pathlib import Path
from typing import Any

from .stats import iso

# The methodology values: samples per kind and run, kinds per run, and the size of each part.
SAMPLES_PER_KIND = 3
MAX_KINDS = 12
BODY_BYTES = 2048
MAX_HEADERS = 64
HEADER_CHARS = 1024
FILE = "refusals.jsonl.gz"
MAX_FILE = 8 << 20  # a spooled file larger than this is not ours
REDACTED = "[redacted]"
# How an answer arrived: the gateway's answer to our CONNECT; an answer through a CONNECT or SOCKS
# tunnel, so from the target's side (the target or a CDN in front of it); an answer to a
# plain-HTTP request sent to the gateway (the gateway's own, or the target's through it).
LEGS = ("connect", "tunnel", "plain")
SECRET_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "x-api-key"})
SECRET_WORDS = ("token", "secret", "password", "session", "credential", "api-key", "apikey")
# A credential after its scheme; 12 characters or more, so a challenge's `Basic realm="x"` stays.
_CREDENTIAL = re.compile(r"\b(Basic|Bearer|Digest)\s+[A-Za-z0-9+/=._~-]{12,}", re.IGNORECASE)


class Quota:
    """At most `per_kind` takes of each kind and at most `kinds` kinds."""

    def __init__(self, per_kind: int = SAMPLES_PER_KIND, kinds: int = MAX_KINDS) -> None:
        self.per_kind, self.kinds = per_kind, kinds
        self.counts: dict[tuple[str, int], int] = {}

    def take(self, kind: tuple[str, int]) -> bool:
        count = self.counts.get(kind)
        if count is None and len(self.counts) >= self.kinds:
            return False
        if (count or 0) >= self.per_kind:
            return False
        self.counts[kind] = (count or 0) + 1
        return True


def _text(value: Any) -> str:
    """Printable text that JSON and any later reader keep: UTF-8, no NUL, no lone surrogate."""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    return str(value).encode("utf-8", "replace").decode("utf-8").replace("\x00", "")


def scrub(text: str, secrets: Iterable[str]) -> str:
    """`text` without the given secrets (longest first) and any scheme credential."""
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return _CREDENTIAL.sub(rf"\1 {REDACTED}", text)


def _header(name: str, value: str, secrets: Iterable[str]) -> list[str]:
    lowered = name.lower()
    if lowered == "set-cookie":
        cookie, separator, attributes = value.partition(";")
        value = f"{cookie.split('=', 1)[0]}={REDACTED}{separator}{attributes}"
    elif lowered in SECRET_HEADERS or any(word in lowered for word in SECRET_WORDS):
        value = REDACTED
    return [name[:HEADER_CHARS], scrub(value, secrets)[:HEADER_CHARS]]


def capture(
    leg: str, status: int, headers: Any, body: bytes | bytearray, secrets: Iterable[str]
) -> dict:
    """One answer's sample, redacted and cut to size. `headers` is (name, value) pairs, bytes or
    text, or a mapping; `secrets` are the request's own login strings."""
    secrets = [_text(secret) for secret in secrets]
    pairs = headers.items() if hasattr(headers, "items") else headers or ()
    kept = [_header(_text(n), _text(v), secrets) for n, v in list(pairs)[:MAX_HEADERS]]
    # The whole read body is scrubbed before the cut, so no secret is kept half.
    text = scrub(_text(body), secrets).encode("utf-8")[:BODY_BYTES].decode("utf-8", "ignore")
    return {
        "captured_at": iso(time.time()),
        "leg": leg,
        "status": status,
        "headers": kept,
        "body": text,
        "body_bytes": len(body),
    }


class Refusals:
    """One run's samples: the transport's captures, within the run's own quota."""

    def __init__(self) -> None:
        self.quota = Quota()
        self.samples: list[dict] = []

    def add(self, outcome: Any) -> None:
        sample = outcome.refusal
        if sample is not None and self.quota.take((sample["leg"], sample["status"])):
            self.samples.append(sample | {"error": outcome.error, "exit_ip": outcome.ip})

    def rows(self, run_id: str, config: Any) -> list[dict]:
        """The samples with the run's identifiers, as FILE holds them."""
        ids = {
            "schema_version": "1",
            "run_id": run_id,
            "network": config.provider,
            "product": config.product,
            "country": config.requested_country,
            "runner_location": config.runner_location,
            "target": config.target,
        }
        return [ids | sample for sample in self.samples]


def write(directory: Path, rows: list[dict]) -> Path | None:
    """FILE in `directory` with `rows` (none written without rows); its path, else None."""
    if not rows:
        return None
    destination, partial = directory / FILE, directory / f"{FILE}.partial"
    with partial.open("wb") as raw:
        partial.chmod(0o600)
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as out:
            for row in rows:
                out.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
    partial.replace(destination)
    return destination


def _archived(path: Path) -> list[dict]:
    """The samples of one spooled run file; [] when it has none."""
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            if member.name == FILE and member.isfile() and member.size <= MAX_FILE:
                stream = archive.extractfile(member)
                with gzip.open(stream, "rt", encoding="utf-8") as lines:
                    return [json.loads(line) for line in lines]
    return []


def _spool_files(root: Path, first: date, last: date, network: str | None) -> Iterator[Path]:
    """The run files spooled on the days first..last (by spool hour), oldest first."""
    from .archive.rawfiles import files, hours, networks

    for hour, folder in hours(Path(root)):
        if first <= hour.date() <= last:
            for name, own in networks(folder):
                if network is None or name == network:
                    yield from files(own)


def spooled(
    root: Path, first: date, last: date, network: str | None = None, status: int | None = None
) -> Iterator[dict]:
    """The samples of the runs spooled on the days first..last, oldest first, each with its spool
    file's name; only `network`'s and `status`'s when given. Read-only."""
    for path in _spool_files(root, first, last, network):
        for row in _archived(path):
            if status is None or row.get("status") == status:
                yield row | {"file": path.name}
