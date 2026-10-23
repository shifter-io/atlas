"""The scope guard: the only delete path and the only project assignment path.

The account also runs production droplets of other projects. A resource is deleted only when a
fresh GET, taken immediately before the `DELETE`, shows every ownership marker of this campaign.
Every attempt is audited; a refusal raises `OwnershipError` and never falls back to another path.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import weakref
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ..onboarding.files import private_dir
from .contracts import (
    PROJECT_NAME,
    RESOURCE_PREFIX,
    ROLES,
    CampaignSpec,
    Droplet,
    DropletRecord,
    Scope,
    short_id,
)
from .do_api import ApiError, DigitalOcean, ScopeError, parse_droplet

# Only this module holds the token `DigitalOcean.request` needs for DELETE and assignment.
_GUARD = object()
PROJECT_CACHE_SECONDS = 60.0
OWN_UNASSIGNED_SECONDS = 30 * 60
PATHS = {"droplet": "/v2/droplets/{}", "ssh_key": "/v2/account/keys/{}",
         "firewall": "/v2/firewalls/{}", "snapshot": "/v2/snapshots/{}"}
Kind = Literal["droplet", "ssh_key", "firewall", "snapshot"]
Role = Literal["worker", "manager", "builder"]
# Per client: project membership cache, project ids whose name was checked, and the ids that
# assign_and_verify_project could not confirm (the only way into the own-unassigned path).
_projects: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_named: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_assign_failed: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
monotonic = time.monotonic  # the project cache's clock; tests replace it


def utcnow() -> datetime:
    return datetime.now(UTC)


class OwnershipError(Exception):
    """A delete or assignment refused because the resource is not provably ours."""

    def __init__(self, kind: str, resource_id: str, failed: list[str]):
        super().__init__(f"refused {kind} {resource_id}: failed {', '.join(failed)}")
        self.kind, self.resource_id, self.failed = kind, resource_id, failed


@dataclass(frozen=True)
class Ledger:
    """What the campaign recorded: ids per kind, and the droplet records by droplet id."""

    ids: Mapping[str, Collection[str | int]]
    records: Mapping[str, DropletRecord] = field(default_factory=dict)

    def has(self, kind: str, resource_id: str) -> bool:
        return resource_id in {str(i) for i in self.ids.get(kind, ())}


@dataclass(frozen=True)
class Retry:
    attempts: int = 3
    delay: float = 5.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


ASSIGN_RETRY = Retry()  # 3 tries, 5 s apart (plan sections 2 and 9)


def require_guard(token: object) -> None:
    """`DigitalOcean.request` calls this for every DELETE and project assignment."""
    if token is not _GUARD:
        raise ScopeError("only scope.py may delete or assign to a project")


def scope_for(spec: CampaignSpec) -> Scope:
    return Scope(spec.campaign_id, short_id(spec.campaign_id), spec.project_id,
                 str(spec.manager_droplet_id), datetime.fromisoformat(spec.created_at))


def role_checks(droplet: Droplet, scope: Scope, role: Role) -> dict[str, bool]:
    """Section 2 checks 2, 3, 5 and 6 on one droplet, by the role the caller intends."""
    tags, (role_tag, pattern) = set(droplet.tags), ROLES[role]
    worker, is_manager = role == "worker", droplet.id == scope.manager_droplet_id
    required = {role_tag, f"{RESOURCE_PREFIX}{scope.sid}"} if worker else {role_tag}
    others = {tag for tag, _ in ROLES.values()} - {role_tag}
    named = re.fullmatch(pattern, droplet.name) is not None
    return {
        "tags": required <= tags and not tags & others,
        "name": named and (not worker or droplet.name.split("-")[1] == scope.sid),
        "not_manager": is_manager if role == "manager" else not is_manager,
        "created_after": droplet.created_at >= scope.created_at,
    }


async def _project_named(cloud: DigitalOcean, project_id: str) -> bool:
    """The scope's project id really is project Proxy Index (checked once per client)."""
    named = _named.setdefault(cloud, set())
    if project_id not in named and await cloud.project_name(project_id) == PROJECT_NAME:
        named.add(project_id)
    return project_id in named


async def _in_project(cloud: DigitalOcean, project_id: str, droplet_id: str) -> bool:
    """Project membership, cached for at most 60 s and always refreshed on a miss."""
    cached = _projects.setdefault(cloud, {}).get(project_id)
    if cached and monotonic() - cached[0] < PROJECT_CACHE_SECONDS and droplet_id in cached[1]:
        return True
    members = await cloud.project_droplet_ids(project_id)
    _projects[cloud][project_id] = (monotonic(), members)
    return droplet_id in members


async def _droplet_checks(cloud: DigitalOcean, droplet: Droplet, scope: Scope, ledger: Ledger,
                          role: Role, entry: dict) -> dict[str, bool]:
    """Checks 2-6; check 4 may be replaced only by the own-unassigned path (section 2)."""
    checks = role_checks(droplet, scope, role)
    checks["project_id"] = await _project_named(cloud, scope.project_id)
    if await _in_project(cloud, scope.project_id, droplet.id):
        return checks | {"project": True}
    record = ledger.records.get(droplet.id)
    exception = {
        "assign_failed": record is not None and record.status == "assign_failed"
        and droplet.id in _assign_failed.get(cloud, ()),
        "own_create": droplet.id in cloud.created_ids,
        "fresh": (utcnow() - droplet.created_at).total_seconds() <= OWN_UNASSIGNED_SECONDS,
    }
    if all(exception.values()):
        entry["path"] = "own_unassigned"
        return checks | exception
    return checks | {"project": False}


def _other_checks(kind: str, raw: dict, ledger: Ledger) -> dict[str, bool]:
    checks = {"name": str(raw.get("name", "")).startswith(RESOURCE_PREFIX)}
    if kind == "firewall":
        checks["targets"] = (
            all(str(tag).startswith(RESOURCE_PREFIX) for tag in raw.get("tags", []))
            and all(ledger.has("droplet", str(i)) for i in raw.get("droplet_ids", []))
        )
    return checks


def _audit(path: Path, entry: dict) -> None:
    private_dir(path.parent)
    line = (json.dumps(entry, sort_keys=True, allow_nan=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


async def guarded_delete(
    cloud: DigitalOcean, kind: Kind, resource_id: str | int, scope: Scope, ledger: Ledger,
    audit_path: Path, role: Role = "worker",
) -> str:
    """Delete one recorded resource of this campaign, or raise `OwnershipError`.

    `role` is the droplet the caller means: a worker of `scope`, the recorded manager or a
    builder (for these two, `scope.created_at` is the inventory time). Returns "deleted", or
    "absent" when the fresh GET finds nothing. Any other error is audited and re-raised.
    """
    if kind not in PATHS or role not in ROLES:
        raise ValueError("unknown resource kind or role")
    rid = str(resource_id)
    path = PATHS[kind].format(rid)
    entry = {"time": utcnow().isoformat(), "kind": kind, "id": rid, "role": role,
             "campaign": scope.sid, "path": "normal"}
    try:
        raw = await cloud.get_resource(path, kind)
        if raw is None:
            _audit(audit_path, entry | {"result": "absent", "http": 404})
            return "absent"
        entry["name"] = raw.get("name")
        checks = {"recorded": ledger.has(kind, rid)} | (
            await _droplet_checks(cloud, parse_droplet(raw), scope, ledger, role, entry)
            if kind == "droplet" else _other_checks(kind, raw, ledger))
        entry["checks"] = checks
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            _audit(audit_path, entry | {"result": "refused"})
            raise OwnershipError(kind, rid, failed)
        _audit(audit_path, entry | {"result": "attempt"})
        await cloud.request("DELETE", path, guard=_GUARD)
    except OwnershipError:
        raise
    except Exception as error:
        _audit(audit_path, entry | {"result": "failed", "error": type(error).__name__,
                                    "http": getattr(error, "status", None)})
        raise
    _audit(audit_path, entry | {"result": "deleted", "http": 204})
    return "deleted"


async def _fresh_owned(cloud: DigitalOcean, scope: Scope, rid: str,
                       records: Mapping[str, DropletRecord], role: Role) -> None:
    """Refuse an id that is not recorded, not from our create, or lacks a marker on a fresh GET."""
    record, droplet = records.get(rid), await cloud.get(rid)
    checks = {"recorded": record is not None and record.droplet_id == rid,
              "own_create": rid in cloud.created_ids, "exists": droplet is not None}
    if droplet is not None:
        checks |= role_checks(droplet, scope, role)
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise OwnershipError("droplet", rid, failed)


async def _assign(cloud: DigitalOcean, project_id: str, missing: set[str], retry: Retry) -> None:
    """Assign and re-read the project until `missing` is empty or the attempts run out."""
    for attempt in range(retry.attempts):
        try:
            await cloud.assign_project(project_id, sorted(missing), guard=_GUARD)
            members = await cloud.project_droplet_ids(project_id)
            _projects.setdefault(cloud, {})[project_id] = (monotonic(), members)
            missing -= members
        except ApiError:
            pass
        if not missing:
            return
        if attempt + 1 < retry.attempts:
            await retry.sleep(retry.delay)


async def assign_and_verify_project(
    cloud: DigitalOcean, scope: Scope, droplet_ids: Collection[str],
    records: Mapping[str, DropletRecord], audit_path: Path, role: Role = "worker",
    retry: Retry = ASSIGN_RETRY,
) -> tuple[frozenset[str], frozenset[str]]:
    """The only assignment path: move droplets we just created into project Proxy Index.

    Before any POST, every id must be recorded, come from this client's create, and show every
    marker on a fresh GET. Returns (confirmed, failed). Fail closed: a failed id is set to
    `assign_failed`, must never be started, and is deleted through the own-unassigned path.
    """
    ids = sorted({str(i) for i in droplet_ids})
    for rid in ids:
        await _fresh_owned(cloud, scope, rid, records, role)
    if not ids:
        return frozenset(), frozenset()
    if not await _project_named(cloud, scope.project_id):
        raise OwnershipError("project", scope.project_id, ["project_id"])
    missing = set(ids)
    await _assign(cloud, scope.project_id, missing, retry)
    for rid in ids:
        record = records[rid]
        if rid in missing:
            record.status, record.error_class = "assign_failed", "project_assign_failed"
            _assign_failed.setdefault(cloud, set()).add(rid)
        else:
            record.status, record.assigned_at = "assigned", utcnow().isoformat()
    ledger = Ledger({"droplet": ids}, records)
    for rid in sorted(missing):
        try:
            await guarded_delete(cloud, "droplet", rid, scope, ledger, audit_path, role)
            records[rid].status, records[rid].destroyed_at = "destroyed", utcnow().isoformat()
        except OwnershipError:
            records[rid].status = "destroy_refused"
        except ApiError:
            records[rid].status = "destroy_failed"
    return frozenset(ids) - missing, frozenset(missing)
