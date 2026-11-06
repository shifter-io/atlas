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
from types import MappingProxyType
from typing import Literal

from ..onboarding.files import private_dir
from .contracts import (
    ALL_ROLE_TAGS,
    PROJECT_NAME,
    PULSE_PREFIX,
    PULSE_PROJECT_NAME,
    RESOURCE_PREFIX,
    ROLES,
    CampaignSpec,
    Droplet,
    DropletRecord,
    Scope,
    environment_of,
    roles,
    short_id,
)
from .do_api import DigitalOcean, ScopeError, parse_droplet

# Only this module holds the token `DigitalOcean.request` needs for DELETE and assignment.
_GUARD = object()
PROJECT_CACHE_SECONDS = 60.0
OWN_UNASSIGNED_SECONDS = 30 * 60
# The only statuses in which a later process may hand a worker with the ledger mark to the guard.
UNASSIGNED_REMOVABLE = ("assign_failed", "destroy_failed", "destroyed")
# Just after a create, DigitalOcean may answer a GET without the droplet, its tags, its name or
# its creation time. Our own create is read again over about 30 s; nobody else's ever is.
SETTLE_DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0)
SETTLING = frozenset({"readable", "exists", "tags", "name", "created_after"})
# Removed from a stored error message: tokens, IPv4 and IPv6 addresses, long opaque strings.
SECRETS = re.compile(
    r"(?i)bearer\s+\S+|\b(?:\d{1,3}\.){3}\d{1,3}\b"
    r"|\b(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}\b|[\w-]{32,}"
)
MESSAGE_LIMIT = 300
PATHS = MappingProxyType(
    {
        "droplet": "/v2/droplets/{}",
        "ssh_key": "/v2/account/keys/{}",
        "firewall": "/v2/firewalls/{}",
        "snapshot": "/v2/snapshots/{}",
    }
)
Kind = Literal["droplet", "ssh_key", "firewall", "snapshot"]
Role = Literal["worker", "manager", "builder", "pulse"]
# The scope of the always-on pulse nodes: no campaign's short id (always 8 hex digits) equals it.
PULSE_SID = "pulse"
# Per client: project membership cache, and the (project id, name) pairs whose name was checked.
_projects: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_named: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
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


def _unsaved() -> None:
    """The default `save`: a caller that keeps no ledger file."""


@dataclass(frozen=True)
class Retry:
    """The assignment's tries (`attempts`, `delay` apart), the waits between the fresh reads
    of our own new droplets (`settle`) and the caller's ledger `save`, which
    `remove_unassigned` calls between the mark and the delete."""

    attempts: int = 3
    delay: float = 5.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    settle: tuple[float, ...] = SETTLE_DELAYS
    save: Callable[[], None] = _unsaved


ASSIGN_RETRY = Retry()  # 3 tries, 5 s apart (plan sections 2 and 9)


def require_guard(token: object) -> None:
    """`DigitalOcean.request` calls this for every DELETE and project assignment."""
    if token is not _GUARD:
        raise ScopeError("only scope.py may delete or assign to a project")


def error_detail(
    cloud: DigitalOcean,
    droplet_ids: Collection[str],
    error: BaseException | None = None,
    failed: Collection[str] = (),
) -> dict:
    """Why droplets failed, for the ledger and the audit: the exception class (`Unconfirmed`
    when nothing was raised), its message without tokens or addresses, the failed checks and
    the HTTP statuses of the latest calls about these droplets."""
    failed = list(getattr(error, "failed", failed))
    message = str(error) if error else f"not confirmed: failed {', '.join(failed)}"
    return {
        "error": type(error).__name__ if error else "Unconfirmed",
        "message": SECRETS.sub("[removed]", message)[:MESSAGE_LIMIT],
        "failed": failed,
        "http": cloud.http_trace(droplet_ids),
    }


def scope_for(spec: CampaignSpec) -> Scope:
    return Scope(
        spec.campaign_id,
        short_id(spec.campaign_id),
        spec.project_id,
        str(spec.manager_droplet_id),
        datetime.fromisoformat(spec.created_at),
        spec.environment,
    )


def pulse_scope(project_id: str) -> Scope:
    """The scope of the pulse nodes in project `project_id` (which must be Dev Resources)."""
    return Scope(PULSE_SID, PULSE_SID, project_id, "", datetime.min.replace(tzinfo=UTC))


def _same_kind(scope: Scope, role: Role) -> None:
    """The pulse role goes only with the pulse scope, and the pulse scope only with it."""
    if (role == "pulse") != (scope.sid == PULSE_SID):
        raise ValueError("the pulse role needs the pulse scope, and only the pulse role may use it")


def role_checks(droplet: Droplet, scope: Scope, role: Role) -> dict[str, bool]:
    """Section 2 checks 2, 3, 5 and 6 on one droplet, by the role the caller intends in the
    scope's environment. A droplet with any other role tag, of any environment, fails."""
    tags, (role_tag, pattern) = set(droplet.tags), roles(scope.environment)[role]
    if role == "pulse":  # its own rule: the pulse tag, only pi-pulse- tags, its name pattern
        only_pulse = all(tag.startswith(PULSE_PREFIX) for tag in tags)
        named = re.fullmatch(pattern, droplet.name) is not None
        return {"tags": role_tag in tags and only_pulse, "name": named}
    worker, is_manager = role == "worker", droplet.id == scope.manager_droplet_id
    required = {role_tag, f"{RESOURCE_PREFIX}{scope.sid}"} if worker else {role_tag}
    others = ALL_ROLE_TAGS - {role_tag}
    named = re.fullmatch(pattern, droplet.name) is not None
    return {
        "tags": required <= tags and not tags & others,
        "name": named and (not worker or droplet.name.split("-")[1] == scope.sid),
        "not_manager": is_manager if role == "manager" else not is_manager,
        "created_after": droplet.created_at >= scope.created_at,
    }


async def _project_named(cloud: DigitalOcean, project_id: str, name: str = PROJECT_NAME) -> bool:
    """The scope's project id really is project `name` (checked once per client)."""
    named = _named.setdefault(cloud, set())
    if (project_id, name) not in named and await cloud.project_name(project_id) == name:
        named.add((project_id, name))
    return (project_id, name) in named


async def _in_project(cloud: DigitalOcean, project_id: str, droplet_id: str) -> bool:
    """Project membership, cached for at most 60 s and always refreshed on a miss."""
    cached = _projects.setdefault(cloud, {}).get(project_id)
    if cached and monotonic() - cached[0] < PROJECT_CACHE_SECONDS and droplet_id in cached[1]:
        return True
    members = await cloud.project_droplet_ids(project_id)
    _projects[cloud][project_id] = (monotonic(), members)
    return droplet_id in members


async def _droplet_checks(
    cloud: DigitalOcean, droplet: Droplet, scope: Scope, ledger: Ledger, role: Role, entry: dict
) -> dict[str, bool]:
    """Checks 2-6; check 4 may be replaced only by the own-unassigned path (section 2)."""
    checks = role_checks(droplet, scope, role)
    pulse = role == "pulse"
    project_name = PULSE_PROJECT_NAME if pulse else PROJECT_NAME
    checks["project_id"] = await _project_named(cloud, scope.project_id, project_name)
    in_project = await _in_project(cloud, scope.project_id, droplet.id)
    if in_project or pulse:  # a pulse node has no own-unassigned path
        return checks | {"project": in_project}
    record = ledger.records.get(droplet.id)
    # The ledger mark: only assign_and_verify_project writes it, for an id it had checked
    # against its own client's create. A restarted process has no other proof of that create.
    marked = (
        record is not None
        and record.droplet_id == droplet.id
        and record.assign_failed_at is not None
    )
    exception = {
        "assign_failed": marked,
        "own_create": droplet.id in cloud.created_ids or marked,
        "exact_name": marked and record.name == droplet.name,
        "fresh": (utcnow() - droplet.created_at).total_seconds() <= OWN_UNASSIGNED_SECONDS,
    }
    if all(exception.values()):
        entry["path"] = "own_unassigned"
        return checks | exception
    return checks | {"project": False}


def _other_checks(kind: str, raw: dict, ledger: Ledger, environment: str) -> dict[str, bool]:
    """A key, firewall or snapshot named, and a firewall targeting only tags, of the scope's
    environment (a staging scope never removes production's, nor production staging's)."""

    def ours(name: object) -> bool:
        return environment_of(str(name)) == environment

    checks = {"name": ours(raw.get("name", ""))}
    if kind == "firewall":
        checks["targets"] = all(ours(tag) for tag in raw.get("tags", [])) and all(
            ledger.has("droplet", str(i)) for i in raw.get("droplet_ids", [])
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
    cloud: DigitalOcean,
    kind: Kind,
    resource_id: str | int,
    scope: Scope,
    ledger: Ledger,
    audit_path: Path,
    role: Role = "worker",
) -> str:
    """Delete one recorded resource of this campaign, or raise `OwnershipError`.

    `role` is the droplet the caller means: a worker of `scope`, the recorded manager or a
    builder (for these two, `scope.created_at` is the inventory time). Returns "deleted", or
    "absent" when the fresh GET finds nothing. Any other error is audited and re-raised.
    """
    if kind not in PATHS or role not in ROLES:
        raise ValueError("unknown resource kind or role")
    if (role == "pulse") != (scope.sid == PULSE_SID) or (role == "pulse" and kind != "droplet"):
        raise ValueError("a pulse node is removed only by retire_pulse_node")
    rid = str(resource_id)
    path = PATHS[kind].format(rid)
    entry = {
        "time": utcnow().isoformat(),
        "kind": kind,
        "id": rid,
        "role": role,
        "campaign": scope.sid,
        "path": "normal",
    }
    record = ledger.records.get(rid) if kind == "droplet" else None
    if record is not None and record.error_detail:  # why this removal happens
        entry["cause"] = record.error_detail
    try:
        raw = await cloud.get_resource(path, kind)
        if raw is None:
            _audit(audit_path, entry | {"result": "absent", "http": 404})
            return "absent"
        entry["name"] = raw.get("name")
        checks = {"recorded": ledger.has(kind, rid)} | (
            await _droplet_checks(cloud, parse_droplet(raw), scope, ledger, role, entry)
            if kind == "droplet"
            else _other_checks(kind, raw, ledger, scope.environment)
        )
        entry["checks"] = checks
        failed = _failed(checks)
        if failed:
            _audit(audit_path, entry | {"result": "refused"})
            raise OwnershipError(kind, rid, failed)
        _audit(audit_path, entry | {"result": "attempt"})
        await cloud.request("DELETE", path, guard=_GUARD)
    except OwnershipError:
        raise
    except Exception as error:
        _audit(
            audit_path,
            entry
            | {
                "result": "failed",
                "error": type(error).__name__,
                "http": getattr(error, "status", None),
            },
        )
        raise
    _audit(audit_path, entry | {"result": "deleted", "http": 204})
    return "deleted"


async def retire_pulse_node(
    cloud: DigitalOcean, droplet_id: str | int, project_id: str, ledger: Ledger, audit_path: Path
) -> str:
    """The only path that deletes an always-on pulse node: `guarded_delete` with role "pulse",
    which needs its recorded id, the pi-pulse-node tag, its name and project Dev Resources."""
    return await guarded_delete(
        cloud, "droplet", droplet_id, pulse_scope(project_id), ledger, audit_path, "pulse"
    )


async def _fresh_owned(
    cloud: DigitalOcean, scope: Scope, rid: str, records: Mapping[str, DropletRecord], role: Role
) -> list[str]:
    """The failed checks of one id on a fresh GET ([] when it shows every marker).

    An id that is not recorded or not from this client's create raises `OwnershipError` on any
    failure. Our own create may also fail only checks that settle (`SETTLING`: unreadable, not
    shown yet, tags, name, creation time); the caller reads it again. Anything else raises.
    """
    record = records.get(rid)
    checks = {
        "recorded": record is not None and record.droplet_id == rid,
        "own_create": rid in cloud.created_ids,
    }
    try:
        droplet = await cloud.get(rid)
    except Exception:  # unreadable: read again for our own ids only
        if not all(checks.values()):
            raise OwnershipError("droplet", rid, _failed(checks)) from None
        return ["readable"]
    checks["exists"] = droplet is not None
    if droplet is not None:
        checks |= role_checks(droplet, scope, role)
    failed = _failed(checks)
    if not SETTLING.issuperset(failed):
        raise OwnershipError("droplet", rid, failed)
    return failed


def _failed(checks: Mapping[str, bool]) -> list[str]:
    return [name for name, passed in checks.items() if not passed]


def settling_refusal(audit_path: Path, droplet_id: str) -> bool:
    """Whether the latest audited delete of this droplet was refused only because DigitalOcean
    had not caught up with its create: a check in `SETTLING`, plus `project`, which a name not
    shown yet also fails on the own-unassigned path. Reconcile then hands our own marked worker
    to the guard again, which checks everything afresh. No audit line is False."""
    latest = None
    try:
        lines = audit_path.read_text().splitlines()
    except FileNotFoundError:
        return False
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:  # a line cut short by a crash
            continue
        if entry.get("kind") == "droplet" and entry.get("id") == droplet_id:
            latest = entry
    if latest is None or latest.get("result") != "refused":
        return False
    failed = set(_failed(latest["checks"]))
    return bool(failed & SETTLING) and failed <= SETTLING | {"project"}


async def _retrying(retry: Retry, step: Callable[[], Awaitable[bool]]) -> None:
    """Run `step` until it returns True, at most `retry.attempts` times, `retry.delay` apart."""
    for attempt in range(retry.attempts):
        if await step():
            return
        if attempt + 1 < retry.attempts:
            await retry.sleep(retry.delay)


async def _shown(
    cloud: DigitalOcean,
    scope: Scope,
    ids: list[str],
    records: Mapping[str, DropletRecord],
    role: Role,
    retry: Retry,
) -> dict[str, list[str]]:
    """Before any POST: each id's failed checks on its latest fresh GET ([] when shown). Our
    own creates are read again, `retry.settle` apart, until shown. An id that is not provably
    ours raises `OwnershipError` at once."""
    reads: dict[str, list[str]] = {}
    delays = iter(retry.settle)
    while True:
        for rid in ids:
            if reads.get(rid) != []:
                reads[rid] = await _fresh_owned(cloud, scope, rid, records, role)
        delay = next(delays, None)
        if delay is None or not any(reads.values()):
            return reads
        await retry.sleep(delay)


async def _assign(
    cloud: DigitalOcean, project_id: str, missing: set[str], retry: Retry, name: str
) -> None:
    """Assign and re-read the project until `missing` is empty or the attempts run out. A failed
    call leaves its ids unconfirmed; only a project that is not `name` raises."""

    async def attempt() -> bool:
        try:
            named = await _project_named(cloud, project_id, name)
        except Exception:  # unconfirmed this time: retried, then fail closed
            return False
        if not named:
            raise OwnershipError("project", project_id, ["project_id"])
        try:
            await cloud.assign_project(project_id, sorted(missing), guard=_GUARD)
            members = await cloud.project_droplet_ids(project_id)
        except Exception:  # unconfirmed this time: retried, then fail closed
            return False
        _projects.setdefault(cloud, {})[project_id] = (monotonic(), members)
        missing.difference_update(members)
        return not missing

    await _retrying(retry, attempt)


async def assign_and_verify_project(
    cloud: DigitalOcean,
    scope: Scope,
    droplet_ids: Collection[str],
    records: Mapping[str, DropletRecord],
    audit_path: Path,
    role: Role = "worker",
    retry: Retry = ASSIGN_RETRY,
) -> tuple[frozenset[str], frozenset[str]]:
    """The only assignment path: move droplets we just created into project Proxy Index (Dev
    Resources for role "pulse", whose scope must be `pulse_scope`; a mismatch is a ValueError).

    Before any POST, every id must be recorded, come from this client's create, and show every
    marker on a fresh GET; an id that is not ours and fails a check raises `OwnershipError` at
    once, and so does a project that is not Proxy Index. Our own new droplet is read again
    while DigitalOcean catches up (`retry.settle`, about 30 s), and the assignment and its
    re-read are retried (`retry`). Returns (confirmed, failed). Fail closed: an id that is not
    confirmed (still not shown, an error, a project that never lists it) goes to
    `remove_unassigned`.
    """
    _same_kind(scope, role)
    ids = sorted({str(i) for i in droplet_ids})
    if not ids:
        return frozenset(), frozenset()
    reads = await _shown(cloud, scope, ids, records, role, retry)
    missing = {rid for rid in ids if not reads[rid]}
    if missing:
        name = PULSE_PROJECT_NAME if role == "pulse" else PROJECT_NAME
        await _assign(cloud, scope.project_id, missing, retry, name)
    unconfirmed = sorted(missing | {rid for rid in ids if reads[rid]})
    _require_own(cloud, unconfirmed, records)
    now = utcnow().isoformat()
    for rid in set(ids) - set(unconfirmed):
        records[rid].status, records[rid].assigned_at = "assigned", now
    await remove_unassigned(
        cloud,
        scope,
        records,
        audit_path,
        {rid: error_detail(cloud, [rid], failed=reads[rid] or ["project"]) for rid in unconfirmed},
        role,
        retry.save,
    )
    return frozenset(ids) - set(unconfirmed), frozenset(unconfirmed)


def _require_own(
    cloud: DigitalOcean, ids: Collection[str], records: Mapping[str, DropletRecord]
) -> None:
    """The mark's own precondition (m1): only ids from this client's own create, recorded."""
    for rid in sorted(ids):
        record = records.get(rid)
        if rid not in cloud.created_ids or record is None or record.droplet_id != rid:
            raise OwnershipError("droplet", rid, ["own_create"])


async def remove_unassigned(
    cloud: DigitalOcean,
    scope: Scope,
    records: Mapping[str, DropletRecord],
    audit_path: Path,
    causes: Mapping[str, dict],
    role: Role = "worker",
    save: Callable[[], None] = _unsaved,
) -> None:
    """Fail closed for droplets this client just created whose assignment was never confirmed,
    for any reason (`causes`: id -> `error_detail`). Each is set to `assign_failed` with the
    ledger mark `assign_failed_at` and its error, must never be started, and is deleted through
    the guard (the own-unassigned path; the mark lets a later process retry that delete). The
    caller's `save` writes the mark to its ledger before any delete, so a crash in between
    cannot leave an unmarked worker. Its record ends `destroyed`, or `destroy_refused` /
    `destroy_failed` for the caller to flag (and save). An id that is not from this client's
    create raises `OwnershipError` and nothing is marked. A pulse node (role "pulse", pulse scope)
    has no own-unassigned path: no delete is attempted, even when its membership shows late,
    and its record ends `destroy_refused` for the owner.
    """
    _same_kind(scope, role)
    ids = sorted(causes)
    _require_own(cloud, ids, records)
    now = utcnow().isoformat()
    for rid in ids:
        record = records[rid]
        record.status, record.assign_failed_at = "assign_failed", now
        record.error_class = record.error_class or "project_assign_failed"
        record.error_detail = causes[rid]
    try:
        save()
    except Exception:  # never keep the delete from running; the caller's own save re-raises
        pass
    if role == "pulse":  # never deleted here, whatever its membership shows by now
        for rid in ids:
            records[rid].status = "destroy_refused"
        return
    ledger = Ledger({"droplet": ids}, records)
    for rid in ids:
        try:
            await guarded_delete(cloud, "droplet", rid, scope, ledger, audit_path, role)
            records[rid].status, records[rid].destroyed_at = "destroyed", utcnow().isoformat()
        except OwnershipError:
            records[rid].status = "destroy_refused"
        except Exception:  # audited by guarded_delete; never stop before the other ids
            records[rid].status = "destroy_failed"
