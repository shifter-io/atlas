"""Where a DB-IP release comes from: dated files under one CDN base URL.

The CDN serves each release as `<base URL>/dbip-full-YYYY-MM-DD.mmdb` (the release date). The
newest one is found by probing (HEAD) from today backwards: the first name that exists wins,
404 means "no release that day", any other answer stops the update. The base URL is
configuration (`--base-url`, $DBIP_BASE_URL, the orchestrator's `dbip.base_url` setting),
never hard-coded; errors name the file and the HTTP status or error type, never the URL.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .. import __version__

BASE_URL_ENV = "DBIP_BASE_URL"
PREFIX = "dbip-full-"
MAX_DOWNLOAD_BYTES = 16 << 30
TIMEOUT_S = 60
CHUNK = 1 << 20
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class UpdateError(Exception):
    """A failed step; the message is public (never a URL)."""

    code = 3


class ConfigError(UpdateError):
    code = 2


class Busy(UpdateError):
    code = 4


class Missing(UpdateError):
    """HTTP 404: no such file on the CDN."""


def release_name(day: date) -> str:
    return f"{PREFIX}{day.isoformat()}.mmdb"


def base_url(value: str | None = None) -> str:
    """The CDN base URL (argument, else $DBIP_BASE_URL), with one trailing slash."""
    url = (value or os.environ.get(BASE_URL_ENV) or "").strip()
    if not url:
        raise ConfigError(f"no DB-IP base URL: set --base-url or ${BASE_URL_ENV}")
    parts = urlsplit(url)
    if not _secure(url) or parts.query or parts.fragment:
        raise ConfigError("the DB-IP base URL must be https, without a query")
    return url.rstrip("/") + "/"


def _secure(url: str) -> bool:
    """https, or plain http to this machine (the tests' loopback CDN)."""
    parts = urlsplit(url)
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK)


class _InsecureRedirect(Exception):
    pass


class _SecureRedirects(HTTPRedirectHandler):
    """Follow a redirect only to another secure URL, so base_url's https promise holds for the
    whole request and not only its first hop. A probe stays a HEAD (urllib would follow it
    with a GET, starting the whole download)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _secure(newurl):
            fp.close()
            raise _InsecureRedirect
        followed = super().redirect_request(req, fp, code, msg, headers, newurl)
        if followed is not None and req.get_method() == "HEAD":
            followed.method = "HEAD"
        return followed


_OPENER = build_opener(_SecureRedirects)


@dataclass(frozen=True, slots=True)
class Release:
    """One dated file on the CDN, as its HEAD answered."""

    name: str
    day: date
    url: str
    size: int | None
    etag: str | None
    last_modified: str | None

    def identity(self) -> dict[str, Any]:
        """What tells a release apart (the installed one is compared with it): never the URL."""
        found = {"size": self.size, "etag": self.etag, "last_modified": self.last_modified}
        known = {key: value for key, value in found.items() if value is not None}
        return {"name": self.name, "date": self.day.isoformat()} | known


def _open(url: str, step: str, method: str = "GET"):
    request = Request(url, method=method, headers={"User-Agent": f"proxy-index/{__version__}"})
    try:
        return _OPENER.open(request, timeout=TIMEOUT_S)
    except _InsecureRedirect:
        raise UpdateError(f"{step}: redirected to a URL that is not https") from None
    except HTTPError as exc:
        exc.close()
        raise (Missing if exc.code == 404 else UpdateError)(f"{step}: HTTP {exc.code}") from None
    except (URLError, OSError, ValueError, HTTPException) as exc:
        raise UpdateError(f"{step}: {type(exc).__name__}") from None


def newest(base: str, today: date, oldest: date) -> Release | None:
    """The newest dated release from `today` back to `oldest` (both included), or None."""
    day = today
    while day >= oldest:
        name = release_name(day)
        try:
            with _open(base + name, f"probe {name}", "HEAD") as response:
                headers = response.headers
        except Missing:
            day -= timedelta(days=1)
            continue
        length = headers.get("Content-Length")
        size = int(length) if length and length.isdigit() else None
        return Release(
            name, day, base + name, size, headers.get("ETag"), headers.get("Last-Modified")
        )
    return None


@contextmanager
def private_file(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        yield stream
        stream.flush()
        os.fsync(stream.fileno())


def _copy(response, out, digest, step: str) -> int:
    """Copy the response into `out` and `digest`; the bytes copied (at most MAX_DOWNLOAD_BYTES)."""
    size = 0
    try:
        while chunk := response.read(CHUNK):
            size += len(chunk)
            if size > MAX_DOWNLOAD_BYTES:
                raise UpdateError(f"{step}: larger than expected")
            digest.update(chunk)
            out.write(chunk)
    except (OSError, HTTPException) as exc:
        raise UpdateError(f"{step}: {type(exc).__name__}") from None
    return size


def download(release: Release, partial: Path) -> tuple[int, str]:
    """Stream the release into `partial`; its size and SHA-256. The size must be the one the
    HEAD and the GET announced (a cut-short download fails here)."""
    digest = hashlib.sha256()
    step = f"download {release.name}"
    with _open(release.url, step) as response, private_file(partial) as out:
        announced = response.headers.get("Content-Length")
        size = _copy(response, out, digest, step)
    expected = {int(n) for n in (announced, release.size) if n is not None and str(n).isdigit()}
    if expected - {size}:
        raise UpdateError(f"{step}: {size} bytes, not the {max(expected)} announced")
    return size, digest.hexdigest()
