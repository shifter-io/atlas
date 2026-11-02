"""Asynchronous DigitalOcean client limited to an explicit allowlist of calls.

`request()` sends only the method, path and query shapes in `ALLOWED`; anything else is refused
before the network. DELETE and project assignment also need the token only `scope.py` holds.
The API token lives in memory only and never appears in `repr`, exceptions or logs.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
import subprocess
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Mapping
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import aiohttp

from ..regions import REGION_IDS
from .contracts import (
    BUILDER_NAME_RE,
    BUILDER_TAG,
    CAMPAIGN_ROLES,
    PROJECT_NAME,
    PULSE_PREFIX,
    PULSE_TAG,
    RESOURCE_PREFIX,
    ROLES,
    WORKER_TAG,
    Account,
    Droplet,
)

API = "https://api.digitalocean.com"
PAGE_SIZE = 200
CREATE_BATCH = 10
CREATE_FLAGS = ("monitoring", "with_droplet_agent", "ipv6")
PER_MINUTE = 200
BACKOFF = (1, 2, 4, 8)
MAX_WAIT = 60.0
HTTP_LOG = 200  # recent calls kept for diagnosis: method, path, status
HTTP_TRACE = 30
ID, UUID = r"[0-9]+", r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
PAGE = frozenset({"page", "per_page"})
TAG = re.escape(RESOURCE_PREFIX) + r"[a-z0-9-]+"
NONE: frozenset[str] = frozenset()
# (method, path pattern) -> (allowed query keys, needs the scope guard). A delete names one
# resource. Later tasks add endpoints here under scope review; nothing else can be sent.
# Read-only at run time (MappingProxyType); the scans forbid patching it from elsewhere.
ALLOWED = MappingProxyType(
    {
        ("GET", "/v2/account"): (NONE, False),
        ("GET", "/v2/droplets"): (PAGE | {"tag_name"}, False),
        ("GET", "/v2/projects"): (PAGE, False),
        ("GET", f"/v2/projects/{UUID}"): (NONE, False),
        ("GET", f"/v2/projects/{UUID}/resources"): (PAGE, False),
        ("POST", "/v2/droplets"): (NONE, False),
        ("POST", f"/v2/projects/{UUID}/resources"): (NONE, True),
        **{
            (method, path): (NONE, method == "DELETE")
            for method in ("GET", "DELETE")
            for path in (
                f"/v2/droplets/{ID}",
                f"/v2/account/keys/{ID}",
                f"/v2/firewalls/{UUID}",
                f"/v2/snapshots/{ID}",
            )
        },
        # CAMP-1 limits and cloud operations (keys, firewalls, the worker snapshot).
        ("GET", "/v2/sizes"): (PAGE, False),
        ("GET", "/v2/account/keys"): (PAGE, False),
        ("POST", "/v2/account/keys"): (NONE, False),
        ("GET", f"/v2/actions/{ID}"): (NONE, False),
        ("GET", f"/v2/droplets/{ID}/snapshots"): (PAGE, False),
        ("GET", f"/v2/images/{ID}"): (NONE, False),
        ("POST", f"/v2/droplets/{ID}/actions"): (NONE, False),
        ("POST", f"/v2/images/{ID}/actions"): (NONE, False),
        ("POST", "/v2/firewalls"): (NONE, False),
        ("PUT", f"/v2/firewalls/{UUID}"): (NONE, False),
        # CAMP-1h: a firewall may only name a tag that exists. Read and create a pi-camp- tag;
        # never list tags or tag/untag a resource.
        ("GET", f"/v2/tags/{TAG}"): (NONE, False),
        ("POST", "/v2/tags"): (NONE, False),
    }
)
ROLE_TAGS = frozenset(tag for tag, _ in CAMPAIGN_ROLES.values())
SNAPSHOT_NAME_RE = r"^pi-camp-worker-[0-9A-Za-z.+]+-[0-9a-f]{12}-[0-9]{8}$"
FIREWALL_KEYS = frozenset({"name", "inbound_rules", "outbound_rules", "tags", "droplet_ids"})


def _droplet_action(body: dict) -> bool:
    if body.get("type") == "snapshot":
        return set(body) == {"type", "name"} and bool(
            re.fullmatch(SNAPSHOT_NAME_RE, str(body["name"]))
        )
    return set(body) == {"type"} and body["type"] in ("shutdown", "power_off")


def _firewall(body: dict) -> bool:
    return (
        set(body) <= FIREWALL_KEYS
        and str(body.get("name", "")).startswith(RESOURCE_PREFIX)
        and set(body.get("tags") or []) <= ROLE_TAGS
        and all(str(i).isdigit() for i in body.get("droplet_ids") or [])
    )


# Writes to a droplet or firewall that may not be ours, and the tag calls. They need `_OWNED`,
# which only this client's checking methods pass, after a fresh GET (as DELETE needs scope's
# token); the tag calls only from `_ensure_tag`, before a firewall write.
OWNED_WRITES = MappingProxyType(
    {
        key: True
        for key in (
            ("POST", f"/v2/droplets/{ID}/actions"),
            ("POST", "/v2/firewalls"),
            ("PUT", f"/v2/firewalls/{UUID}"),
            ("GET", f"/v2/tags/{TAG}"),
            ("POST", "/v2/tags"),
        )
    }
)
_OWNED = object()
# Bodies the operations calls may carry; checked with the allowlist, before anything is sent.
BODIES = MappingProxyType(
    {
        ("POST", "/v2/account/keys"): lambda body: (
            set(body) == {"name", "public_key"} and str(body["name"]).startswith(RESOURCE_PREFIX)
        ),
        ("POST", f"/v2/droplets/{ID}/actions"): _droplet_action,
        ("POST", f"/v2/images/{ID}/actions"): lambda body: (
            set(body) == {"type", "region"}
            and (body["type"] == "transfer" and body["region"] in REGION_IDS)
        ),
        ("POST", "/v2/firewalls"): _firewall,
        ("PUT", f"/v2/firewalls/{UUID}"): _firewall,
        ("POST", "/v2/tags"): lambda body: (
            set(body) == {"name"} and bool(re.fullmatch(TAG, str(body["name"])))
        ),
    }
)
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


class Uncertain(ApiError):
    """A non-idempotent call whose outcome is unknown (5xx or lost response); never retried."""


class CreateError(ApiError):
    """A create that stopped part-way: `created` exist (ours, recorded); `pending` did not, or
    are unknown when `uncertain` (resolve with `adopt_uncertain` before any re-create)."""

    def __init__(self, error: ApiError, created: list[Droplet], pending: list[str]):
        super().__init__(error.method, error.path, error.status, "create stopped")
        self.created, self.pending = created, pending
        self.uncertain = isinstance(error, Uncertain)


def _lookup(table: Mapping, method: str, path: str) -> Any:
    return next(
        (
            value
            for (m, pattern), value in table.items()
            if m == method and re.fullmatch(pattern, path)
        ),
        None,
    )


def check_allowed(
    method: str,
    path: str,
    params: dict[str, Any] | None,
    guard: object,
    body: dict[str, Any] | None = None,
) -> None:
    """Refuse every call that is not on the allowlist, before anything is sent."""
    entry, shape = _lookup(ALLOWED, method, path), _lookup(BODIES, method, path)
    if entry is None or not set(params or {}) <= entry[0]:
        raise ScopeError(f"{method} {path} is not on the client allowlist")
    if shape is not None and not (isinstance(body, dict) and shape(body)):
        raise ScopeError(f"{method} {path} body is outside the campaign's scope")
    if _lookup(OWNED_WRITES, method, path) and guard is not _OWNED:
        raise ScopeError(f"{method} {path} is sent only after an ownership check")
    tag_name = str((params or {}).get("tag_name"))
    if "tag_name" in entry[0] and not (
        tag_name.startswith(RESOURCE_PREFIX) or tag_name == PULSE_TAG
    ):
        raise ScopeError("droplets are listed only by a pi-camp- tag or pi-pulse-node")
    if entry[1]:
        from .scope import require_guard

        require_guard(guard)


def keychain_token(account: Account) -> TokenSource:
    def read() -> str:
        command = [
            "security",
            "find-generic-password",
            "-s",
            account.keychain_service,
            "-a",
            account.keychain_account,
            "-w",
        ]
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
    public = [
        n["ip_address"] for n in raw.get("networks", {}).get("v4", []) if n.get("type") == "public"
    ]
    return Droplet(
        id=str(raw["id"]),
        name=raw["name"],
        region=raw.get("region", {}).get("slug", ""),
        size=raw.get("size_slug", ""),
        status=raw.get("status", ""),
        created_at=datetime.fromisoformat(raw["created_at"]),
        tags=tuple(raw.get("tags", [])),
        public_ipv4=public[0] if public else None,
    )


def check_create(names: list[str], tags: list[str], ssh_keys: list[str]) -> None:
    """Refuse a create whose names, tags or keys do not carry all ownership markers."""
    roles = [(tag, pattern) for tag, pattern in ROLES.values() if tag in tags]
    prefix = PULSE_PREFIX if [tag for tag, _ in roles] == [PULSE_TAG] else RESOURCE_PREFIX
    if len(roles) != 1 or any(not tag.startswith(prefix) for tag in tags):
        raise ScopeError("create needs exactly one role tag and only that role's tag prefix")
    if not names or not ssh_keys:
        raise ScopeError("create needs names and ssh_keys")
    [(role_tag, pattern)] = roles
    if any(not re.fullmatch(pattern, name) for name in names):
        raise ScopeError("create name does not match its role's pattern")
    if role_tag == WORKER_TAG:
        sids = {name.split("-")[1] for name in names}
        if len(sids) != 1 or f"{RESOURCE_PREFIX}{sids.pop()}" not in tags:
            raise ScopeError("worker create needs the campaign tag of its names")
    elif role_tag != PULSE_TAG and len(names) != 1:
        raise ScopeError("manager and builder are created one at a time")


class DigitalOcean:
    """`async with DigitalOcean(keychain_token(ACCOUNTS["leftclick"])) as cloud: ...`"""

    def __init__(
        self,
        token_source: TokenSource,
        *,
        base_url: str = API,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        per_minute: int = PER_MINUTE,
    ):
        self._token_source, self._base_url = token_source, base_url.rstrip("/")
        self._sleep, self._clock, self._per_minute = sleep, clock, per_minute
        self.__token: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self._sent: deque[float] = deque()
        self._created: set[str] = set()
        self.ratelimit_remaining: int | None = None
        self.http_log: deque[tuple[str, str, int | None]] = deque(maxlen=HTTP_LOG)

    @property
    def created_ids(self) -> frozenset[str]:
        """Ids this client got back from its own creates (or adopted after an uncertain one)."""
        return frozenset(self._created)

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

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        guard: object = None,
    ) -> dict[str, Any]:
        method = method.upper()
        check_allowed(method, path, params, guard, body)
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        # A droplet create is not idempotent: a 5xx or lost answer may still have created it.
        # A retried DELETE needs no new GET: DigitalOcean never reuses ids.
        once = (method, path) == ("POST", "/v2/droplets")
        for attempt in range(len(BACKOFF) + 1):
            await self._throttle()
            wait, payload = await self._attempt(method, path, params, body)
            if wait is None:
                return payload
            if once and wait == 0:
                raise Uncertain(method, path, None, "outcome unknown")
            if attempt < len(BACKOFF):
                await self._sleep(wait if wait > 0 else BACKOFF[attempt])
        raise ApiError(method, path, None, "retries exhausted")

    async def _attempt(self, method, path, params, body):
        """(None, payload) on success; (seconds or 0 for backoff, None) when worth a retry."""
        try:
            async with self._session.request(
                method, self._base_url + path, params=params, json=body, headers=self._headers()
            ) as response:
                self.http_log.append((method, path, response.status))
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
            self.http_log.append((method, path, None))
            return 0, None

    def http_trace(self, droplet_ids: Collection[str]) -> list[str]:
        """The latest calls about these droplets: creates, project assignments and their own
        paths, as "METHOD path status" ("lost" for no answer); never a header, body or address."""
        ids = {str(i) for i in droplet_ids}
        return [
            f"{method} {path} {status or 'lost'}"
            for method, path, status in self.http_log
            if (method == "POST" and (path == "/v2/droplets" or path.endswith("/resources")))
            or path.rsplit("/", 1)[-1] in ids
        ][-HTTP_TRACE:]

    @staticmethod
    def _retry_after(headers) -> float:
        after, reset = headers.get("Retry-After", ""), headers.get("ratelimit-reset", "")
        if after.isdigit():
            return min(float(after), MAX_WAIT)
        if reset.isdigit():
            return min(max(float(reset) - time.time(), 1.0), MAX_WAIT)
        return float(BACKOFF[0])

    async def _pages(self, path: str, key: str, params: dict[str, Any] | None = None) -> list:
        items, page = [], 1
        while True:
            payload = await self.request(
                "GET", path, params={**(params or {}), "page": page, "per_page": PAGE_SIZE}
            )
            batch, meta = payload.get(key, []), payload.get("meta")
            if not isinstance(meta, dict) or not isinstance(meta.get("total"), int):
                raise ApiError("GET", path, None, "no pagination total")
            items.extend(batch)
            if not batch or len(items) >= meta["total"]:
                return items
            page += 1

    async def create(
        self,
        names: list[str],
        *,
        region: str,
        size: str,
        image: str,
        tags: list[str],
        ssh_keys: list[str],
        **flags: bool,
    ) -> list[Droplet]:
        """Create droplets; `flags` sets monitoring, with_droplet_agent and ipv6 (default off)."""
        check_create(names, tags, ssh_keys)
        if set(flags) - set(CREATE_FLAGS):
            raise ScopeError("unknown create flag")
        created = []
        for start in range(0, len(names), CREATE_BATCH):
            body = {
                "names": names[start : start + CREATE_BATCH],
                "region": region,
                "size": size,
                "image": image,
                "tags": tags,
                "ssh_keys": ssh_keys,
                **{flag: bool(flags.get(flag, False)) for flag in CREATE_FLAGS},
            }
            try:
                payload = await self.request("POST", "/v2/droplets", body=body)
            except ApiError as error:
                raise CreateError(error, created, names[start:]) from None
            batch = [parse_droplet(raw) for raw in payload.get("droplets", [])]
            self._created.update(droplet.id for droplet in batch)
            created.extend(batch)
        return created

    async def adopt_uncertain(self, names: list[str], tag: str) -> list[Droplet]:
        """After an uncertain create: the droplets under our tag with exactly these names."""
        found = [d for d in await self.list_tag(tag) if d.name in set(names)]
        self._created.update(droplet.id for droplet in found)
        return found

    async def assign_project(
        self, project_id: str, droplet_ids: list[str], *, guard: object
    ) -> None:
        """Only `scope.assign_and_verify_project` holds the guard this needs."""
        if not droplet_ids or any(not str(i).isdigit() for i in droplet_ids):
            raise ScopeError("project assignment needs droplet ids")
        urns = [f"do:droplet:{i}" for i in droplet_ids]
        await self.request(
            "POST", f"/v2/projects/{project_id}/resources", body={"resources": urns}, guard=guard
        )

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
        found = await self._pages("/v2/droplets", "droplets", {"tag_name": tag})
        return [parse_droplet(raw) for raw in found]

    async def project_droplet_ids(self, project_id: str) -> frozenset[str]:
        found = await self._pages(f"/v2/projects/{project_id}/resources", "resources")
        return frozenset(
            item["urn"].removeprefix("do:droplet:")
            for item in found
            if item.get("urn", "").startswith("do:droplet:")
        )

    async def project_name(self, project_id: str) -> str | None:
        found = await self.get_resource(f"/v2/projects/{project_id}", "project")
        return None if found is None else found.get("name")

    async def project_id(self, name: str) -> str:
        matches = [
            p["id"] for p in await self._pages("/v2/projects", "projects") if p.get("name") == name
        ]
        if len(matches) != 1:
            raise ScopeError(f"expected exactly one project named {name!r}")
        return matches[0]

    async def account(self) -> dict[str, Any]:
        raw = (await self.request("GET", "/v2/account"))["account"]
        return {"droplet_limit": raw.get("droplet_limit"), "status": raw.get("status")}

    async def sizes(self) -> dict[str, list[str]]:
        found = await self._pages("/v2/sizes", "sizes")
        return {s["slug"]: list(s.get("regions", [])) for s in found if s.get("available")}

    async def ssh_keys(self) -> list[dict[str, Any]]:
        """Registered keys with a pi-camp- name only."""
        found = await self._pages("/v2/account/keys", "ssh_keys")
        return [key for key in found if str(key.get("name", "")).startswith(RESOURCE_PREFIX)]

    async def create_ssh_key(self, name: str, public_key: str) -> dict[str, Any]:
        body = {"name": name, "public_key": public_key}
        return (await self.request("POST", "/v2/account/keys", body=body))["ssh_key"]

    async def droplet_action(
        self, droplet_id: str, kind: str, project_id: str, name: str | None = None
    ) -> dict:
        """Shut down, power off or snapshot a builder this client created, in Proxy Index."""
        droplet = await self.get(droplet_id) if droplet_id in self._created else None
        if (
            droplet is None
            or BUILDER_TAG not in droplet.tags
            or not re.fullmatch(BUILDER_NAME_RE, droplet.name)
            or not await self._in_project(project_id, [droplet_id])
        ):
            raise ScopeError("droplet actions run only on a builder this client created")
        body = {"type": kind, **({"name": name} if name else {})}
        return (
            await self.request(
                "POST", f"/v2/droplets/{droplet_id}/actions", body=body, guard=_OWNED
            )
        )["action"]

    async def action(self, action_id: str | int) -> dict[str, Any]:
        return (await self.request("GET", f"/v2/actions/{action_id}"))["action"]

    async def image(self, image_id: str | int) -> dict[str, Any] | None:
        return await self.get_resource(f"/v2/images/{image_id}", "image")

    async def droplet_snapshots(self, droplet_id: str) -> list[dict[str, Any]]:
        return await self._pages(f"/v2/droplets/{droplet_id}/snapshots", "snapshots")

    async def transfer_image(self, image_id: str | int, region: str) -> dict[str, Any]:
        """Copy a campaign worker snapshot (by its name, on a fresh GET) to one more region."""
        found = await self.image(image_id)
        if found is None or not re.fullmatch(SNAPSHOT_NAME_RE, str(found.get("name"))):
            raise ScopeError("only campaign worker snapshots are transferred")
        return (
            await self.request(
                "POST",
                f"/v2/images/{image_id}/actions",
                body={"type": "transfer", "region": region},
            )
        )["action"]

    async def create_firewall(self, rules: dict[str, Any], project_id: str) -> dict[str, Any]:
        await self._own_firewall(rules, project_id)
        await self._ensure_tags(rules)
        return (await self.request("POST", "/v2/firewalls", body=rules, guard=_OWNED))["firewall"]

    async def update_firewall(
        self, firewall_id: str, rules: dict[str, Any], project_id: str
    ) -> dict[str, Any]:
        """Replace a campaign firewall; the current one must be ours on a fresh GET."""
        current = await self.get_resource(f"/v2/firewalls/{firewall_id}", "firewall")
        if current is None:
            raise ScopeError("firewall not found")
        await self._own_firewall(current, project_id)
        await self._own_firewall(rules, project_id)
        await self._ensure_tags(rules)
        return (
            await self.request("PUT", f"/v2/firewalls/{firewall_id}", body=rules, guard=_OWNED)
        )["firewall"]

    async def _own_firewall(self, rules: dict[str, Any], project_id: str) -> None:
        """A pi-camp- name, only role tags, and droplets that each show a role's markers on a
        fresh GET and are in project Proxy Index."""
        if not _firewall({key: rules[key] for key in FIREWALL_KEYS if key in rules}):
            raise ScopeError("firewall is outside the campaign's scope")
        ids = [str(i) for i in rules.get("droplet_ids") or []]
        for droplet_id in ids:
            found = await self.get(droplet_id)
            if found is None or not any(
                tag in found.tags and re.fullmatch(pattern, found.name)
                for tag, pattern in CAMPAIGN_ROLES.values()
            ):
                raise ScopeError("firewall targets a droplet that is not the campaign's")
        if not await self._in_project(project_id, ids):
            raise ScopeError("firewall targets a droplet outside project Proxy Index")

    async def _ensure_tags(self, rules: dict[str, Any]) -> None:
        """DigitalOcean refuses (422) a firewall naming a tag that does not exist yet, as
        pi-camp-worker does before the first worker: create each missing one."""
        for tag in rules.get("tags") or []:
            await self._ensure_tag(str(tag))

    async def _ensure_tag(self, name: str) -> None:
        """Create the pi-camp- tag `name` unless it exists; it is never put on a resource."""
        if not re.fullmatch(TAG, name):
            raise ScopeError("only pi-camp- tags are created")
        try:
            await self.request("GET", f"/v2/tags/{name}", guard=_OWNED)
        except ApiError as error:
            if error.status != 404:
                raise
            await self.request("POST", "/v2/tags", body={"name": name}, guard=_OWNED)

    async def _in_project(self, project_id: str, droplet_ids: list[str]) -> bool:
        """Every id is in `project_id`, and that project is the one named Proxy Index."""
        if not droplet_ids:
            return True
        if await self.project_name(project_id) != PROJECT_NAME:
            return False
        return set(droplet_ids) <= await self.project_droplet_ids(project_id)
