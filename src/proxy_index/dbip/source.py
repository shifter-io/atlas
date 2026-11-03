"""Where a DB-IP release comes from: the account URL, its JSON index and the download.

The account URL and the index's download URLs carry the account key, a secret: they are never
printed, logged, stored or passed in argv, and every error here names the step and the error
type or HTTP status only.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .. import __version__

URL_FILE_ENV = "DBIP_ACCOUNT_URL_FILE"
URL_ENV = "DBIP_ACCOUNT_URL"
MAX_INDEX_BYTES = 1 << 20
MAX_DOWNLOAD_BYTES = 16 << 30
TIMEOUT_S = 60
CHUNK = 1 << 20
SUMS = {"md5sum": "md5", "sha1sum": "sha1", "sha256sum": "sha256", "sha256": "sha256"}
# The index fields kept as a release identity (never its URL).
PUBLIC = ("name", "version", "date", "size", *SUMS)
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class UpdateError(Exception):
    """A failed step; the message is public (never a URL)."""

    code = 3


class ConfigError(UpdateError):
    code = 2


class Busy(UpdateError):
    code = 4


def account_url(url_file: str | Path | None = None) -> str:
    """The account URL from the owner-only file, else $DBIP_ACCOUNT_URL."""
    from ..node import read_token  # the same owner-only, no-link, one-token file check

    path = url_file or os.environ.get(URL_FILE_ENV)
    if path:
        try:
            url = read_token(path)
        except (OSError, ValueError) as exc:
            raise ConfigError(
                f"account URL file: needs one URL, owner-only, mode 600 ({type(exc).__name__})"
            ) from None
    else:
        url = os.environ.get(URL_ENV, "").strip()
    if not url:
        raise ConfigError(f"no DB-IP account URL: set --url-file, ${URL_FILE_ENV} or ${URL_ENV}")
    _check_url(url, "account URL", ConfigError)
    return url


def _check_url(url: str, what: str, error: type[UpdateError]) -> None:
    parts = urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK):
        return
    raise error(f"{what} must be https")


def _open(url: str, step: str):
    try:
        request = Request(url, headers={"User-Agent": f"proxy-index/{__version__}"})
        return urlopen(request, timeout=TIMEOUT_S)  # noqa: S310 - https only (_check_url)
    except HTTPError as exc:
        raise UpdateError(f"{step}: HTTP {exc.code}") from None
    except (URLError, OSError, ValueError) as exc:
        raise UpdateError(f"{step}: {type(exc).__name__}") from None


def fetch_index(url: str) -> dict[str, Any]:
    """The MMDB entry of the account index."""
    with _open(url, "index") as response:
        body = response.read(MAX_INDEX_BYTES + 1)
    if len(body) > MAX_INDEX_BYTES:
        raise UpdateError("index: larger than expected")
    try:
        index = json.loads(body)
    except ValueError:
        raise UpdateError("index: not JSON") from None
    levels = [index, *index.values()] if isinstance(index, dict) else []
    entries = [
        level["mmdb"]
        for level in levels
        if isinstance(level, dict)
        and isinstance(level.get("mmdb"), dict)
        and isinstance(level["mmdb"].get("url"), str)
    ]
    if len(entries) != 1:
        raise UpdateError(f"index: {len(entries)} MMDB downloads instead of one")
    _check_url(entries[0]["url"], "index: the download URL", UpdateError)
    return entries[0]


def identity(entry: dict[str, Any]) -> dict[str, Any]:
    """The public fields that identify a release."""
    return {key: entry[key] for key in PUBLIC if entry.get(key) is not None}


class _Hashes:
    def __init__(self) -> None:
        self.size = 0
        self.digests = {name: hashlib.new(name) for name in set(SUMS.values())}

    def update(self, chunk: bytes) -> None:
        self.size += len(chunk)
        for digest in self.digests.values():
            digest.update(chunk)

    def hexdigest(self, name: str) -> str:
        return self.digests[name].hexdigest()


def _copy(source, target, hashes: _Hashes, limit: int) -> None:
    while chunk := source.read(CHUNK):
        hashes.update(chunk)
        if hashes.size > limit:
            raise UpdateError("download: larger than expected")
        target.write(chunk)


def download(entry: dict[str, Any], partial: Path) -> tuple[_Hashes, _Hashes]:
    """The MMDB in `partial` (decompressed when gzip); hashes of the bytes served and of the
    MMDB. The index's size and checksums must match the served bytes or the MMDB."""
    raw = partial.with_name(partial.name + ".download")
    served = _Hashes()
    with _open(entry["url"], "download") as response, private_file(raw) as out:
        try:
            _copy(response, out, served, MAX_DOWNLOAD_BYTES)
        except OSError as exc:
            raise UpdateError(f"download: {type(exc).__name__}") from None
    with raw.open("rb") as stream:
        packed = stream.read(2) == b"\x1f\x8b"
    plain = served
    if packed:
        plain = _Hashes()
        try:
            with gzip.open(raw, "rb") as source, private_file(partial) as out:
                _copy(source, out, plain, MAX_DOWNLOAD_BYTES)
        except (OSError, EOFError) as exc:
            raise UpdateError(f"download: not a valid gzip file ({type(exc).__name__})") from None
        raw.unlink()
    else:
        raw.replace(partial)
    _check_served(entry, served, plain)
    return served, plain


@contextmanager
def private_file(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        yield stream
        stream.flush()
        os.fsync(stream.fileno())


def _check_served(entry: dict[str, Any], served: _Hashes, plain: _Hashes) -> None:
    size = entry.get("size")
    if size is not None and str(size).isdigit() and int(size) not in (served.size, plain.size):
        raise UpdateError("verify: size differs from the index")
    for key, name in SUMS.items():
        expected = str(entry.get(key) or "").strip().lower()
        if expected and expected not in (served.hexdigest(name), plain.hexdigest(name)):
            raise UpdateError(f"verify: {key} differs from the index")


