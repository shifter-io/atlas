"""Asynchronous DigitalOcean client that can only read, create and assign within our scope.

It has no delete method. `request()` refuses `DELETE` unless the caller passes the guard token that
only `scope.py` holds, so `scope.guarded_delete` is the single delete path. The API token lives in
memory only and never appears in `repr`, exceptions or logs.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
import subprocess
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from .contracts import (
    BUILDER_NAME_RE,
    BUILDER_TAG,
    MANAGER_NAME_RE,
    MANAGER_TAG,
    RESOURCE_PREFIX,
    WORKER_NAME_RE,
    WORKER_TAG,
    Account,
    Droplet,
)

API = "https://api.digitalocean.com"
PAGE_SIZE = 200
CREATE_BATCH = 10
PER_MINUTE = 200
BACKOFF = (1, 2, 4, 8)
MAX_WAIT = 60.0
# A delete always names one resource; never a collection, a tag or a query.
DELETE_PATH = r"/v2/(droplets|account/keys|firewalls|snapshots)/[0-9A-Za-z-]+"
TokenSource = Callable[[], str]


class ApiError(Exception):
    """A failed API call: method, path and status only, never headers or the token."""

    def __init__(self, method: str, path: str, status: int | None, reason: str = ""):
        super().__init__(f"{method} {path} failed ({status or reason})")
        self.method, self.path, self.status = method, path, status


class ScopeError(ValueError):
    """A create, list or delete request outside the campaign's ownership markers."""


class TokenError(RuntimeError):
    pass


def keychain_token(account: Account) -> TokenSource:
    def read() -> str:
        command = ["security", "find-generic-password", "-s", account.keychain_service,
                   "-a", account.keychain_account, "-w"]
        found = subprocess.run(command, capture_output=True, text=True, check=False)
        if found.returncode != 0 or not found.stdout.strip():
            raise TokenError(f"no keychain token for account {account.key}")
        return found.stdout.strip()

    return read


def file_token(path: Path) -> TokenSource:
    def read() -> str:
        info = path.lstat()
        private = stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077
        if not private or info.st_uid != os.getuid():
            raise TokenError("token file must be a regular 0600 file owned by this user")
        token = path.read_text().strip()
        if not token:
            raise TokenError("token file is empty")
        return token

    return read


def parse_droplet(raw: dict[str, Any]) -> Droplet:
    public = [n["ip_address"] for n in raw.get("networks", {}).get("v4", [])
              if n.get("type") == "public"]
    return Droplet(
        id=str(raw["id"]), name=raw["name"], region=raw.get("region", {}).get("slug", ""),
        size=raw.get("size_slug", ""), status=raw.get("status", ""),
        created_at=datetime.fromisoformat(raw["created_at"]), tags=tuple(raw.get("tags", [])),
        public_ipv4=public[0] if public else None,
    )


def check_create(names: list[str], tags: list[str], ssh_keys: list[str]) -> None:
    """Refuse a create whose names, tags or keys do not carry all ownership markers."""
    roles = [tag for tag in (WORKER_TAG, MANAGER_TAG, BUILDER_TAG) if tag in tags]
    if len(roles) != 1 or any(not tag.startswith(RESOURCE_PREFIX) for tag in tags):
        raise ScopeError("create needs exactly one role tag and only pi-camp- tags")
    if not names or not ssh_keys:
        raise ScopeError("create needs names and ssh_keys")
    pattern = {WORKER_TAG: WORKER_NAME_RE, MANAGER_TAG: MANAGER_NAME_RE,
               BUILDER_TAG: BUILDER_NAME_RE}[roles[0]]
    if any(not re.fullmatch(pattern, name) for name in names):
        raise ScopeError("create name does not match its role's pattern")
    if roles[0] == WORKER_TAG:
        sids = {name.split("-")[1] for name in names}
        if len(sids) != 1 or f"{RESOURCE_PREFIX}{sids.pop()}" not in tags:
            raise ScopeError("worker create needs the campaign tag of its names")
    elif len(names) != 1:
        raise ScopeError("manager and builder are created one at a time")


class DigitalOcean:
    """`async with DigitalOcean(keychain_token(ACCOUNTS["leftclick"])) as cloud: ...`"""

    def __init__(self, token_source: TokenSource, *, base_url: str = API,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic, per_minute: int = PER_MINUTE):
        self._token_source, self._base_url = token_source, base_url.rstrip("/")
        self._per_minute = per_minute
        self._sleep, self._clock = sleep, clock
        self.__token: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self._sent: deque[float] = deque()
        self.ratelimit_remaining: int | None = None

    def __repr__(self) -> str:
        return f"DigitalOcean(base_url={self._base_url!r})"

    async def __aenter__(self) -> DigitalOcean:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _throttle(self) -> None:
        while self._sent and self._clock() - self._sent[0] >= 60:
            self._sent.popleft()
        if len(self._sent) >= self._per_minute:
            await self._sleep(60 - (self._clock() - self._sent[0]))
            self._sent.popleft()
        self._sent.append(self._clock())

    def _headers(self) -> dict[str, str]:
        if self.__token is None:
            self.__token = self._token_source()
        return {"Authorization": f"Bearer {self.__token}", "Content-Type": "application/json"}

    async def request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                      body: dict[str, Any] | None = None, guard: object = None) -> dict[str, Any]:
        if method == "DELETE":
            from . import scope

            if guard is not scope._DELETE_TOKEN or params or not re.fullmatch(DELETE_PATH, path):
                raise ScopeError("DELETE is only possible through scope.guarded_delete")
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        for attempt in range(len(BACKOFF) + 1):
            await self._throttle()
            wait, payload = await self._attempt(method, path, params, body)
            if wait is None:
                return payload
            if attempt < len(BACKOFF):
                await self._sleep(wait if wait > 0 else BACKOFF[attempt])
        raise ApiError(method, path, None, "retries exhausted")

    async def _attempt(self, method, path, params, body):
        """(None, payload) on success; (seconds or 0 for backoff, None) when worth a retry."""
        try:
            async with self._session.request(method, self._base_url + path, params=params,
                                             json=body, headers=self._headers()) as response:
                remaining = response.headers.get("ratelimit-remaining", "")
                if remaining.isdigit():
                    self.ratelimit_remaining = int(remaining)
                if response.status == 429:
                    return self._retry_after(response.headers), None
                if response.status >= 500:
                    return 0, None
                if response.status >= 400:
                    raise ApiError(method, path, response.status)
                return None, (await response.json() if response.status != 204 else {})
        except (aiohttp.ClientError, TimeoutError):
            return 0, None

    @staticmethod
    def _retry_after(headers) -> float:
        after, reset = headers.get("Retry-After", ""), headers.get("ratelimit-reset", "")
        if after.isdigit():
            return min(float(after), MAX_WAIT)
        if reset.isdigit():
            return min(max(float(reset) - time.time(), 1.0), MAX_WAIT)
        return 0

    async def _pages(self, path: str, key: str, params: dict[str, Any] | None = None) -> list:
        items, page = [], 1
        while True:
            payload = await self.request(
                "GET", path, params={**(params or {}), "page": page, "per_page": PAGE_SIZE}
            )
            batch = payload.get(key, [])
            items.extend(batch)
            if not batch or len(items) >= payload.get("meta", {}).get("total", 0):
                return items
            page += 1

    async def create(self, names: list[str], *, region: str, size: str, image: str,
                     tags: list[str], ssh_keys: list[str], monitoring: bool = False,
                     with_droplet_agent: bool = False, ipv6: bool = False) -> list[Droplet]:
        check_create(names, tags, ssh_keys)
        created = []
        for start in range(0, len(names), CREATE_BATCH):
            body = {"names": names[start:start + CREATE_BATCH], "region": region, "size": size,
                    "image": image, "tags": tags, "ssh_keys": ssh_keys, "monitoring": monitoring,
                    "with_droplet_agent": with_droplet_agent, "ipv6": ipv6}
            payload = await self.request("POST", "/v2/droplets", body=body)
            created.extend(parse_droplet(raw) for raw in payload.get("droplets", []))
        return created

    async def assign_project(self, project_id: str, droplet_ids: list[str]) -> None:
        if not droplet_ids or any(not str(i).isdigit() for i in droplet_ids):
            raise ScopeError("project assignment needs droplet ids")
        urns = [f"do:droplet:{i}" for i in droplet_ids]
        await self.request("POST", f"/v2/projects/{project_id}/resources", body={"resources": urns})

    async def get(self, droplet_id: str) -> Droplet | None:
        found = await self.get_resource(f"/v2/droplets/{droplet_id}", "droplet")
        return None if found is None else parse_droplet(found)

    async def get_resource(self, path: str, key: str) -> dict[str, Any] | None:
        """One object by path, or None when the API answers 404."""
        try:
            return (await self.request("GET", path))[key]
        except ApiError as error:
            if error.status == 404:
                return None
            raise

    async def list_tag(self, tag: str) -> list[Droplet]:
        if not tag.startswith(RESOURCE_PREFIX):
            raise ScopeError("only pi-camp- tags may be listed")
        found = await self._pages("/v2/droplets", "droplets", {"tag_name": tag})
        return [parse_droplet(raw) for raw in found]

    async def project_droplet_ids(self, project_id: str) -> frozenset[str]:
        found = await self._pages(f"/v2/projects/{project_id}/resources", "resources")
        return frozenset(item["urn"].removeprefix("do:droplet:") for item in found
                         if item.get("urn", "").startswith("do:droplet:"))

    async def project_id(self, name: str) -> str:
        matches = [p["id"] for p in await self._pages("/v2/projects", "projects")
                   if p.get("name") == name]
        if len(matches) != 1:
            raise ScopeError(f"expected exactly one project named {name!r}")
        return matches[0]

    async def account(self) -> dict[str, Any]:
        raw = (await self.request("GET", "/v2/account"))["account"]
        return {"droplet_limit": raw.get("droplet_limit"), "status": raw.get("status")}

    async def sizes(self) -> dict[str, list[str]]:
        found = await self._pages("/v2/sizes", "sizes")
        return {s["slug"]: list(s.get("regions", [])) for s in found if s.get("available")}
