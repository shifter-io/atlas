"""Tear the campaign manager down (section 6 of docs/campaign.html): refuse unless collected
and no worker lives, stop the units, shred the secrets, remove every recorded resource through
the guard, remove the Mac agent and prove it. `teardown --keep-worker-assets` removes only the
manager and its firewall and retires it in the inventory, so the next `deploy` creates a new
manager and reuses the worker snapshot and firewall. A staging inventory (CAMP-STAGING-1) counts
and removes only its own atlas-camp-stg- resources, never a snapshot, and keeps the Mac agent,
which watches production.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..onboarding.files import read_json
from . import host_layout
from .contracts import PRODUCTION, campaign_tag, tags, with_legacy
from .deploy import (
    BASE,
    DeployError,
    Manager,
    audit_path,
    check_account,
    load_inventory,
    manager_scope,
    refuse_pre_rename,
    save,
    unit_names,
)
from .do_api import DigitalOcean
from .hostkeys import known_hosts
from .image import Shell, run_shell
from .ops_api import inventory_environment
from .scope import Ledger, guarded_delete

GONE = ("deleted", "absent")  # guarded_delete results that leave nothing behind
# What `teardown --keep-worker-assets` moves into `retired_managers`; RESET is cleared with it
# so the next `deploy` creates, provisions and records a new manager from scratch.
RETIRED = (
    "manager_id",
    "manager_name",
    "manager_since",
    "manager_size",
    "manager_firewall_id",
    "campaigns",
    "worker_key_id",
    "worker_key_name",
    "framework_version",
)
RESET = RETIRED + (
    "host",
    "mac_ipv4",
    "secrets_shredded",
    "campaign_id",
    "specs",
    "enabled",
    "disabled",
    "credentials",
    "wheel_sha256",
    "deployed_at",
    "teardown",
)
SHRED = (
    f"cd {BASE} && find .secrets -type f -exec shred -u {{}} + && rm -rf .secrets && "
    "test ! -e .secrets && echo shredded"
)
PROBE = f"test -d {BASE} && echo present || echo absent"  # a deploy that stopped early has none


def _remove_agent() -> bool:
    from .guard import remove

    return remove(Path.home())


def check_reports(inventory: dict, reports: list[Path]) -> None:
    """Every recorded campaign needs a clean collect report (0 mismatches, missing, extra),
    and every given report must be clean and for a recorded campaign. A manager that never
    recorded a campaign needs no report."""
    recorded, clean = inventory.get("campaigns", []), set()
    for path in reports:
        report = read_json(path)
        if report.get("clean") is not True or any(
            report.get(key) for key in ("mismatches", "missing", "extra")
        ):
            raise DeployError(f"collect report {path.name} is not clean")
        if report.get("campaign_id") not in recorded:
            raise DeployError(f"collect report {path.name} is for no recorded campaign")
        clean.add(report.get("campaign_id"))
    missing = [cid for cid in recorded if cid not in clean]
    if missing:
        raise DeployError(f"teardown needs a clean collect report for {missing}")


def _owned(
    inventory: dict, images: dict, keep_worker_assets: bool = False
) -> list[tuple[str, str]]:
    """(kind, id) in teardown order: manager, firewalls, the worker keys (the current one and
    those kept from retired managers), the Mac key, snapshots (production only: staging uses
    production's image read only). Keeping the worker assets leaves the manager and its
    firewall."""
    found = [
        ("droplet", inventory.get("manager_id")),
        ("firewall", inventory.get("manager_firewall_id")),
    ]
    if not keep_worker_assets:
        found += [
            ("firewall", inventory.get("worker_firewall_id")),
            ("ssh_key", inventory.get("worker_key_id")),
        ]
        found += [("ssh_key", key) for key in inventory.get("kept_worker_key_ids", [])]
        found += [("ssh_key", inventory.get("mac_key_id"))]
        if inventory_environment(inventory) == PRODUCTION:
            found += [("snapshot", b.get("snapshot_id")) for b in images.get("builds", [])]
    return [(kind, str(rid)) for kind, rid in found if rid]


async def _silence(cloud: DigitalOcean, inventory: dict, path: Path, shell: Shell) -> str:
    """Stop the units and shred the secrets: once, and only while the manager still exists
    (a removed droplet takes its disk, and its secrets, with it). A manager whose deploy
    stopped before it made the base directory holds no secrets; only the manager's own
    `absent` answer skips the shred. Returns what it found, for the teardown record."""
    if inventory.get("secrets_shredded"):
        return "shredded earlier"
    if await cloud.get(str(inventory["manager_id"])) is None:
        return "manager gone"
    remote = Manager(inventory, path, shell)
    units = [unit for cid in inventory.get("campaigns", []) for unit in unit_names(cid)]
    if units:
        await remote.run("systemctl disable --now " + " ".join(units), "stop units")
    if (await remote.run(PROBE, "find the base directory")).strip().splitlines()[-1:] == ["absent"]:
        return f"none: {BASE} does not exist"
    if (await remote.run(SHRED, "shred secrets")).strip().splitlines()[-1:] != ["shredded"]:
        raise DeployError("manager secrets were not shredded")
    save(path, inventory, secrets_shredded=True)
    return "shredded"


async def _release(
    cloud: DigitalOcean, inventory: dict, path: Path, keep_worker_assets: bool
) -> list[dict]:
    """Every recorded resource through the guard, except what an earlier run removed. The
    manager's firewall stays while the manager does, so SSH never opens on a live manager."""
    earlier = {
        (r["kind"], r["id"]): r["result"] for r in inventory.get("teardown", {}).get("released", [])
    }
    owned = _owned(
        inventory, load_inventory(path.with_name("campaign-images.json")), keep_worker_assets
    )
    ledger = Ledger({kind: {rid for k, rid in owned if k == kind} for kind, _ in owned})
    scope, audit = manager_scope(inventory), audit_path(path)

    async def remove(kind: str, rid: str) -> str:
        """The droplet is the recorded manager (other kinds ignore the role). A refusal or
        error is audited by the guard and returned, so the other resources still go."""
        try:
            return await guarded_delete(cloud, kind, rid, scope, ledger, audit, "manager")
        except Exception as error:  # boundary: audited by the guard; the others still go
            return f"failed: {type(error).__name__}"

    released, manager_gone = [], False
    for kind, rid in owned:
        result = earlier.get((kind, rid), "")
        if result in GONE:
            pass
        elif (
            kind == "firewall"
            and rid == str(inventory.get("manager_firewall_id"))
            and not (manager_gone)
        ):
            result = "kept: the manager remains"
        else:
            result = await remove(kind, rid)
        manager_gone = manager_gone or (kind == "droplet" and result in GONE)
        released.append({"kind": kind, "id": rid, "result": result})
    return released


def _retire(path: Path, inventory: dict, done: dict) -> None:
    """Move the removed manager into `retired_managers`, keep its worker key for a later full
    teardown, and forget its host key, so the next `deploy` creates a new manager."""
    retired = {key: inventory.get(key) for key in RETIRED} | {"teardown": done}
    kept = [
        *inventory.get("kept_worker_key_ids", []),
        *filter(None, [inventory.get("worker_key_id")]),
    ]
    known_hosts(path, inventory).unlink(missing_ok=True)  # recorded before any manager
    for key in RESET:
        inventory.pop(key, None)
    save(
        path,
        inventory,
        kept_worker_key_ids=kept,
        retired_managers=[*inventory.get("retired_managers", []), retired],
    )


async def teardown(
    cloud: DigitalOcean,
    inventory_path: Path,
    reports: list[Path],
    *,
    keep_worker_assets: bool = False,
    shell: Shell = run_shell,
    remove_agent: Callable[[], bool] | None = None,
) -> dict:
    """Section 16: refuse unless collected and no worker lives; stop the units; shred the
    secrets; remove every recorded resource (snapshots: from the image inventory beside the
    manager's) through the guard; remove the Mac agent; prove. Rerun it after a partial
    failure: it skips the steps and resources an earlier run finished.

    `keep_worker_assets` removes only the manager and its firewall, keeping the worker
    snapshots, firewall and keys and the Mac key; once clean, the manager is retired."""
    inventory = load_inventory(inventory_path)
    if host_layout.of(inventory):
        raise DeployError(
            "the manager is on an existing server, not a droplet: stop it with "
            "orchestrator-disable --apply; teardown removes droplet managers only"
        )
    if not inventory.get("manager_id"):
        raise DeployError("no manager is recorded")
    refuse_pre_rename(inventory)  # before anything is stopped or removed
    await check_account(cloud, inventory, inventory.get("account", ""))
    check_reports(inventory, reports)
    environment = inventory_environment(inventory)
    roles = tags(environment)
    if any([await cloud.list_tag(tag) for tag in with_legacy([roles["worker"]])]):
        raise DeployError("campaign workers are still alive")
    secrets = await _silence(cloud, inventory, inventory_path, shell)
    released = await _release(cloud, inventory, inventory_path, keep_worker_assets)
    counted = with_legacy(
        [*roles.values(), *(campaign_tag(c) for c in inventory.get("campaigns", []))]
    )
    proof = {tag: len(await cloud.list_tag(tag)) for tag in counted}
    stray = [i for i in inventory.get("unassigned_managers", []) if await cloud.get(str(i))]
    clean = all(r["result"] in GONE for r in released) and not any(proof.values()) and not (stray)
    done = {
        "secrets": secrets,
        "released": released,
        "agent_removed": environment == PRODUCTION and (remove_agent or _remove_agent)(),
        "tag_counts": proof,
        "unassigned_managers": stray,
        "clean": clean,
        "kept_worker_assets": keep_worker_assets,
    }
    save(inventory_path, inventory, teardown=done)
    if keep_worker_assets and clean:
        _retire(inventory_path, inventory, done)
    return done
