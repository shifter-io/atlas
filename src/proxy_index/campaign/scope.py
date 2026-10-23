"""The scope guard: the only delete path, and verified project assignment.

The DigitalOcean account also runs production droplets that belong to other projects. A resource
is deleted only when a fresh GET, taken immediately before the `DELETE`, shows every ownership
marker of this campaign. Every attempt, passed or refused, appends one line to the delete audit.
A refusal raises `OwnershipError` and never falls back to another path.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import weakref
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ..onboarding.files import private_dir
from .contracts import (
    BUILDER_NAME_RE,
    BUILDER_TAG,
    MANAGER_NAME_RE,
    MANAGER_TAG,
    RESOURCE_PREFIX,
    WORKER_NAME_RE,
    WORKER_TAG,
    CampaignSpec,
    Droplet,
    Scope,
    short_id,
)
from .do_api import ApiError, DigitalOcean, parse_droplet

# Only this module holds the token that lets `DigitalOcean.request` send a DELETE.
_DELETE_TOKEN = object()
PROJECT_CACHE_SECONDS = 60.0
PATHS = {
    "droplet": "/v2/droplets/{}",
    "ssh_key": "/v2/account/keys/{}",
    "firewall": "/v2/firewalls/{}",
    "snapshot": "/v2/snapshots/{}",
}
Kind = Literal["droplet", "ssh_key", "firewall", "snapshot"]
Role = Literal["worker", "manager", "builder"]
_projects: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class OwnershipError(Exception):
    """A delete or assignment refused because the resource is not provably ours."""

    def __init__(self, kind: str, resource_id: str, failed: list[str]):
        super().__init__(f"refused {kind} {resource_id}: failed {', '.join(failed)}")
        self.kind, self.resource_id, self.failed = kind, resource_id, failed


@dataclass(frozen=True)
class AssignResult:
    assigned: frozenset[str]
    destroyed: frozenset[str]
    refused: frozenset[str]


def scope_for(spec: CampaignSpec) -> Scope:
    return Scope(spec.campaign_id, short_id(spec.campaign_id), spec.project_id,
                 str(spec.manager_droplet_id), datetime.fromisoformat(spec.created_at))


def role_checks(droplet: Droplet, scope: Scope, role: Role) -> dict[str, bool]:
    """Section 2 checks 2, 3, 5 and 6 on one droplet, by the role the caller intends."""
    tags, worker = set(droplet.tags), role == "worker"
    others = {"worker": {MANAGER_TAG, BUILDER_TAG}, "manager": {WORKER_TAG, BUILDER_TAG},
              "builder": {WORKER_TAG, MANAGER_TAG}}[role]
    required = {"worker": {WORKER_TAG, f"{RESOURCE_PREFIX}{scope.sid}"},
                "manager": {MANAGER_TAG}, "builder": {BUILDER_TAG}}[role]
    pattern = {"worker": WORKER_NAME_RE, "manager": MANAGER_NAME_RE,
               "builder": BUILDER_NAME_RE}[role]
    named = re.fullmatch(pattern, droplet.name) is not None
    is_manager = droplet.id == scope.manager_droplet_id
    return {
        "tags": required <= tags and not tags & others,
        "name": named and (not worker or droplet.name.split("-")[1] == scope.sid),
        "not_manager": is_manager if role == "manager" else not is_manager,
        "created_after": droplet.created_at >= scope.created_at,
    }


async def _in_project(cloud: DigitalOcean, project_id: str, droplet_id: str,
                      clock: Callable[[], float]) -> bool:
    """Project membership, cached for at most 60 s and always refreshed on a miss."""
    cached = _projects.setdefault(cloud, {}).get(project_id)
    if cached and clock() - cached[0] < PROJECT_CACHE_SECONDS and droplet_id in cached[1]:
        return True
    members = await cloud.project_droplet_ids(project_id)
    _projects[cloud][project_id] = (clock(), members)
    return droplet_id in members


def _other_checks(kind: str, raw: dict, recorded: set[str]) -> dict[str, bool]:
    checks = {"name": str(raw.get("name", "")).startswith(RESOURCE_PREFIX)}
    if kind == "firewall":
        checks["targets"] = (
            all(str(tag).startswith(RESOURCE_PREFIX) for tag in raw.get("tags", []))
            and {str(i) for i in raw.get("droplet_ids", [])} <= recorded
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
    cloud: DigitalOcean, kind: Kind, resource_id: str | int, scope: Scope,
    recorded_ids: Collection[str | int], audit_path: Path, *, role: Role = "worker",
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Delete one recorded resource of this campaign, or raise `OwnershipError`.

    `role` says which droplet the caller means to delete: a worker of `scope`, the recorded
    manager or a builder. For manager and builder deletes the caller passes a scope whose
    `created_at` is the inventory time. Returns "deleted", or "absent" when the fresh GET finds
    nothing. API errors are audited and re-raised; nothing is retried here.
    """
    if kind not in PATHS or role not in ("worker", "manager", "builder"):
        raise ValueError("unknown resource kind or role")
    rid, recorded = str(resource_id), {str(i) for i in recorded_ids}
    path = PATHS[kind].format(rid)
    entry = {"time": datetime.now(UTC).isoformat(), "kind": kind, "id": rid, "role": role,
             "campaign": scope.sid}
    try:
        raw = await cloud.get_resource(path, kind)
        if raw is None:
            _audit(audit_path, entry | {"result": "absent", "http": 404})
            return "absent"
        entry["name"] = raw.get("name")
        if kind == "droplet":
            checks = role_checks(parse_droplet(raw), scope, role)
            checks["project"] = await _in_project(cloud, scope.project_id, rid, clock)
        else:
            checks = _other_checks(kind, raw, recorded)
        checks = {"recorded": rid in recorded} | checks
        entry["checks"] = checks
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            _audit(audit_path, entry | {"result": "refused"})
            raise OwnershipError(kind, rid, failed)
        await cloud.request("DELETE", path, guard=_DELETE_TOKEN)
    except ApiError as error:
        _audit(audit_path, entry | {"result": "failed", "http": error.status})
        raise
    _audit(audit_path, entry | {"result": "deleted", "http": 204})
    return "deleted"


async def assign_verified(
    cloud: DigitalOcean, scope: Scope, droplets: list[Droplet], recorded_ids: Collection[str],
    *, role: Role = "worker", attempts: int = 5, delay: float = 15.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> frozenset[str]:
    """Move droplets we just created into the campaign project and verify they arrived.

    Only recorded ids whose create response shows every ownership marker are sent; anything
    else raises `OwnershipError` before any request. Returns the ids still outside the project
    after `attempts` tries `delay` seconds apart (empty when all arrived).
    """
    recorded = {str(i) for i in recorded_ids}
    for droplet in droplets:
        checks = {"recorded": droplet.id in recorded} | role_checks(droplet, scope, role)
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise OwnershipError("droplet", droplet.id, failed)
    missing = {droplet.id for droplet in droplets}
    if not missing:
        return frozenset()
    for attempt in range(attempts):
        try:
            await cloud.assign_project(scope.project_id, sorted(missing))
            members = await cloud.project_droplet_ids(scope.project_id)
            _projects.setdefault(cloud, {})[scope.project_id] = (clock(), members)
            missing -= members
        except ApiError:
            pass
        if not missing:
            break
        if attempt + 1 < attempts:
            await sleep(delay)
    return frozenset(missing)


async def assign_or_destroy(
    cloud: DigitalOcean, scope: Scope, droplets: list[Droplet], recorded_ids: Collection[str],
    audit_path: Path, *, role: Role = "worker", attempts: int = 5, delay: float = 15.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> AssignResult:
    """Fail closed: a droplet that never reaches the project is destroyed through the guard.

    The guard re-reads the project before deciding, so a late assignment still deletes; a
    droplet the guard refuses stays up, is reported in `refused` and must be flagged.
    """
    missing = await assign_verified(cloud, scope, droplets, recorded_ids, role=role,
                                    attempts=attempts, delay=delay, sleep=sleep, clock=clock)
    destroyed, refused = set(), set()
    for droplet_id in sorted(missing):
        try:
            await guarded_delete(cloud, "droplet", droplet_id, scope, recorded_ids, audit_path,
                                 role=role, clock=clock)
            destroyed.add(droplet_id)
        except (OwnershipError, ApiError):
            refused.add(droplet_id)
    assigned = frozenset(d.id for d in droplets) - missing
    return AssignResult(assigned, frozenset(destroyed), frozenset(refused))
