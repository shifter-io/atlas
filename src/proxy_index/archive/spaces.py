"""A small DigitalOcean Spaces (S3) client on the standard library: AWS Signature V4.

Only what the archive needs: HEAD, PUT and GET of one object, a prefix's keys, and the bucket's
versioning and ACL. Objects are written private (`x-amz-acl: private`) with a signed payload
sha256 and a Content-MD5, so the server rejects a body that changed on the way; the file's
sha256 is also stored as `x-amz-meta-sha256`, which HEAD returns for the verification before any
deletion.

Settings: a `[spaces]` table (endpoint, bucket, access_key_id and optionally region and
secret_access_key) in a 0600 file. Without secret_access_key the secret is read from the macOS
keychain (service KEYCHAIN_SERVICE, account = the access key id). Nothing is logged or put in
argv. Plain http is accepted only for a loopback endpoint (tests).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from ..providers.credentials import read_credentials
from . import ArchiveError

KEYCHAIN_SERVICE = "Proxy Index Spaces archive"
EMPTY = hashlib.sha256(b"").hexdigest()
PUBLIC = ("http://acs.amazonaws.com/groups/global/AllUsers", "/AuthenticatedUsers")
LOOPBACK = ("127.0.0.1", "localhost", "::1")
TIMEOUT_S = 120
CHUNK = 1 << 20


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def sign(request: dict[str, Any], key_id: str, secret: str, region: str, now: datetime) -> str:
    """The Authorization header of a request {method, host, path, query, headers, payload}.

    headers must hold x-amz-date (now) and x-amz-content-sha256 (payload hash); every header
    given is signed.
    """
    stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    headers = {k.lower(): str(v).strip() for k, v in request["headers"].items()}
    headers["host"] = request["host"]
    names = sorted(headers)
    query = "&".join(
        f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in sorted(request["query"].items())
    )
    canonical = "\n".join(
        (
            request["method"],
            quote(request["path"], safe="/~"),
            query,
            "".join(f"{name}:{headers[name]}\n" for name in names),
            ";".join(names),
            request["payload"],
        )
    )
    scope = f"{day}/{region}/s3/aws4_request"
    text = "\n".join(
        ("AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest())
    )
    signing = _hmac(
        _hmac(_hmac(_hmac(f"AWS4{secret}".encode(), day), region), "s3"), "aws4_request"
    )
    signature = hmac.new(signing, text.encode(), hashlib.sha256).hexdigest()
    return (
        f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders={';'.join(names)}, "
        f"Signature={signature}"
    )


def keychain_secret(key_id: str) -> str:
    command = ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", key_id, "-w"]
    found = subprocess.run(command, capture_output=True, text=True, check=False)
    if found.returncode != 0 or not found.stdout.strip():
        raise ArchiveError(
            f"no Spaces secret: expected a keychain generic password with service "
            f"{KEYCHAIN_SERVICE!r} and the access key id as its account"
        )
    return found.stdout.strip()


def manager_settings(path: str | Path) -> bytes:
    """The [spaces] file for the manager: the local settings with the secret from the keychain
    (memory only; deploy streams it over SSH stdin into a root-only file)."""
    from ..campaign.deploy import toml_table

    try:
        values = read_credentials(path, "spaces")
    except ValueError as exc:
        raise ArchiveError(f"Spaces settings: {exc}") from None
    if "secret_access_key" not in values:
        values["secret_access_key"] = keychain_secret(values.get("access_key_id", ""))
    return toml_table("spaces", values)


async def push_settings(inventory: Path, data: bytes, shell=None) -> None:
    """Write the [spaces] file to the recorded manager's root-only .secrets/spaces.toml over
    SSH stdin (campaign.deploy.Manager.put_secret); nothing in argv."""
    from ..campaign import deploy

    remote = deploy.Manager(deploy.load_inventory(inventory), inventory, shell or deploy.run_shell)
    await remote.put_secret("spaces.toml", data)


class ArchiveChanged(ArchiveError):
    """The object at a key holds other bytes than the file meant for it; nothing was written."""


class Spaces:
    """One bucket. Raises ArchiveError on any answer other than the expected one."""

    def __init__(self, settings: dict[str, str], *, now=lambda: datetime.now(UTC)) -> None:
        url = urlsplit(settings["endpoint"])
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in LOOPBACK):
            raise ArchiveError("the Spaces endpoint must be https")
        self.scheme, self.host = url.scheme, url.netloc
        self.bucket = settings["bucket"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket):
            raise ArchiveError("invalid bucket name")
        self.region = settings.get("region") or (url.hostname or "").split(".")[0]
        self.key_id = settings["access_key_id"]
        self.secret = settings.get("secret_access_key") or keychain_secret(self.key_id)
        self.now = now

    @classmethod
    def from_file(cls, path: str | Path) -> Spaces:
        try:
            return cls(read_credentials(path, "spaces"))
        except (KeyError, ValueError, OSError) as exc:
            raise ArchiveError(f"Spaces settings: {type(exc).__name__}: {exc}") from None

    def _request(self, method, key="", query=None, headers=None, body=None, payload=EMPTY):
        path = f"/{self.bucket}/{key}" if key else f"/{self.bucket}"
        now = self.now()
        headers = {
            **(headers or {}),
            "x-amz-date": now.strftime("%Y%m%dT%H%M%SZ"),
            "x-amz-content-sha256": payload,
        }
        request = {"method": method, "host": self.host, "path": path, "query": query or {}}
        request |= {"headers": headers, "payload": payload}
        headers["Authorization"] = sign(request, self.key_id, self.secret, self.region, now)
        target = quote(path, safe="/~") + (
            "?" + "&".join(f"{k}={v}" if v else k for k, v in sorted((query or {}).items()))
            if query
            else ""
        )
        kind = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        connection = kind(self.host, timeout=TIMEOUT_S)
        try:
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response, connection
        except OSError as exc:
            connection.close()
            raise ArchiveError(f"Spaces {method} failed: {type(exc).__name__}") from None

    def _call(self, method, key="", expect=(200,), **options) -> tuple[int, dict, bytes]:
        status, headers, response, connection = self._request(method, key, **options)
        try:
            data = response.read()
        finally:
            connection.close()
        if status not in expect:
            code = re.search(rb"<Code>([A-Za-z]+)</Code>", data)
            what = code.group(1).decode() if code else status
            raise ArchiveError(f"Spaces {method} {key or self.bucket} answered {what}")
        return status, {k.lower(): v for k, v in headers.items()}, data

    def head(self, key: str) -> dict[str, Any] | None:
        """The object's size, MD5 ETag and metadata (sha256, ...), or None when it is absent."""
        status, headers, _ = self._call("HEAD", key, expect=(200, 404))
        if status == 404:
            return None
        if "content-length" not in headers or headers.get("etag") is None:
            raise ArchiveError(f"Spaces HEAD {key} answered without a size or ETag")
        meta = {k[11:]: v for k, v in headers.items() if k.startswith("x-amz-meta-")}
        return meta | {"bytes": int(headers["content-length"]), "md5": headers["etag"].strip('"')}

    def put_file(self, key: str, path: Path, digests: dict, meta: dict | None = None) -> None:
        """Upload a file private; digests are file_digests(path) (bytes, sha256, md5), meta
        more x-amz-meta- values (lowercase names)."""
        headers = {f"x-amz-meta-{k}": v for k, v in (meta or {}).items()} | {
            "Content-Length": str(digests["bytes"]),
            "Content-MD5": base64.b64encode(bytes.fromhex(digests["md5"])).decode(),
            "Content-Type": "application/octet-stream",
            "x-amz-acl": "private",
            "x-amz-meta-sha256": digests["sha256"],
        }
        with Path(path).open("rb") as body:
            self._call("PUT", key, headers=headers, body=body, payload=digests["sha256"])

    def put_verified(
        self,
        key: str,
        path: Path,
        digests: dict,
        meta: dict | None = None,
        *,
        existing: tuple[str, ...] | None = None,
        what: str = "the local file",
    ) -> dict[str, Any]:
        """Store a file at key once, verified: HEAD; PUT when absent; HEAD again.

        A new object must answer with the file's bytes, MD5 and sha256 and the meta given; an
        object already there only with the fields named in `existing` (default: all of them).
        Raises ArchiveChanged (`archive_changed: <key> ... differs from <what>`) otherwise and
        never overwrites. Returns the object's HEAD with its key.
        """
        expected = {k: digests[k] for k in ("bytes", "md5", "sha256")} | (meta or {})
        found = self.head(key)
        if found is None:
            self.put_file(key, path, digests, meta)
            found = self.head(key)
        elif existing is not None:
            expected = {k: expected[k] for k in existing}
        if found is None or any(found.get(k) != v for k, v in expected.items()):
            raise ArchiveChanged(f"archive_changed: {key} in the bucket differs from {what}")
        return {"key": key, **found}

    def get_file(self, key: str, path: Path) -> None:
        status, _, response, connection = self._request("GET", key)
        try:
            if status != 200:
                raise ArchiveError(f"Spaces GET {key} answered {status}")
            partial = Path(path).with_name(Path(path).name + ".partial")
            with partial.open("wb") as out:
                while chunk := response.read(CHUNK):
                    out.write(chunk)
            partial.replace(path)
        finally:
            connection.close()

    def list_keys(self, prefix: str) -> list[str]:
        """Every key starting with prefix, in key order (ListObjectsV2, paged by start-after:
        callers' keys are plain path characters)."""
        keys: list[str] = []
        while True:
            query = {"list-type": "2", "prefix": prefix} | (
                {"start-after": keys[-1]} if keys else {}
            )
            _, _, data = self._call("GET", query=query)
            page = [k.decode() for k in re.findall(rb"<Key>([^<]+)</Key>", data)]
            keys += page
            if not page or b"<IsTruncated>true</IsTruncated>" not in data:
                return keys

    def versioning(self) -> str | None:
        _, _, data = self._call("GET", query={"versioning": ""})
        found = re.search(rb"<Status>(\w+)</Status>", data)
        return found.group(1).decode() if found else None

    def enable_versioning(self) -> None:
        body = (
            b'<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            b"<Status>Enabled</Status></VersioningConfiguration>"
        )
        headers = {
            "Content-MD5": base64.b64encode(
                hashlib.md5(body, usedforsecurity=False).digest()
            ).decode()
        }
        payload = hashlib.sha256(body).hexdigest()
        self._call("PUT", query={"versioning": ""}, headers=headers, body=body, payload=payload)

    def public_grants(self) -> list[str]:
        """The bucket ACL's grants to everyone or to every authenticated user (want none)."""
        _, _, data = self._call("GET", query={"acl": ""})
        uris = re.findall(rb"<URI>([^<]+)</URI>", data)
        return [u.decode() for u in uris if u.decode().endswith(PUBLIC)]
