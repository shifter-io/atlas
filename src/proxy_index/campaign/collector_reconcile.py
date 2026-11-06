"""The collector's section 9 reconcile: adopt unrecorded owned droplets, retire `destroyed` ones
that remain and our own workers whose project assignment was never confirmed (moved out of
`collector.py`, FILE-16)."""

from __future__ import annotations

from ..onboarding.files import atomic_json
from .contracts import CampaignSpec, Droplet, DropletRecord, HourRecord, audit_path, campaign_tag
from .fanout import RemoteError
from .scope import (
    OWN_UNASSIGNED_SECONDS,
    UNASSIGNED_REMOVABLE,
    Ledger,
    OwnershipError,
    guarded_delete,
    role_checks,
    scope_for,
    settling_refusal,
)
from .tick import FINAL, LAUNCHABLE, save_hour


class Reconciler:
    """`Collector`'s reconcile steps; they use its `root`, `cloud`, `remote_factory`, `clock`,
    `log` and `unreachable`."""

    async def reconcile(self, spec: CampaignSpec, hours: dict[int, HourRecord]) -> None:
        """Section 9: adopt unrecorded owned droplets, retire `destroyed` ones that remain, and
        retire our own workers whose project assignment was never confirmed (CAMP-1k, CAMP-1r)."""
        scope = scope_for(spec)
        known = {
            d.droplet_id: (record, d)
            for record in hours.values()
            for d in record.droplets
            if d.droplet_id
        }
        found = await self.cloud.list_tag(campaign_tag(spec.campaign_id))
        members = await self.cloud.project_droplet_ids(spec.project_id)
        self.unreachable = set()
        for droplet in found:
            await self._reconcile_one(scope, hours, known, members, droplet)

    async def _reconcile_one(self, scope, hours, known, members, droplet: Droplet) -> None:
        """One tagged droplet: left to the tick, alerted as foreign, adopted or retired."""
        checks = role_checks(droplet, scope, "worker")
        hour = int(droplet.name.split("-")[2][1:]) if checks["name"] else None
        if hour in hours and hours[hour].state in LAUNCHABLE:
            return  # the tick owns a launching hour; its creates may not be recorded yet
        entry = known.get(droplet.id)
        stray = entry is not None and entry[1].assign_failed_at is not None
        if (
            hour not in hours
            or not all(checks.values())
            or (droplet.id not in members and not stray)
        ):
            self.log.add(
                self.clock(),
                "foreign_tagged",
                key=droplet.id,
                droplet_id=droplet.id,
                name=droplet.name,
            )
        elif entry is None:
            await self._adopt(scope, hours[hour], droplet, hours)
        elif self._retry(*entry) or (stray and self._own_retry(entry[1], droplet)):
            await self._retire(scope, *entry, hours)

    @staticmethod
    def _retry(record: HourRecord, drop: DropletRecord) -> bool:
        """`destroyed` but still listed, or `destroy_failed` in a final hour (S5; before that the
        harvester retries it)."""
        return drop.status == "destroyed" or (
            drop.status == "destroy_failed" and record.state in FINAL
        )

    def _own_retry(self, drop: DropletRecord, droplet: Droplet) -> bool:
        """Our own marked worker goes to the guard again: in a status a later process may hand
        it in, or `destroy_refused` only because DigitalOcean was still catching up with its
        create (its audit), while it is within the 30-minute own window."""
        if drop.status in UNASSIGNED_REMOVABLE:
            return True
        young = (self.clock() - droplet.created_at).total_seconds() <= OWN_UNASSIGNED_SECONDS
        return (
            drop.status == "destroy_refused"
            and young
            and settling_refusal(audit_path(self.root), drop.droplet_id)
        )

    async def _adopt(self, scope, record: HourRecord, droplet: Droplet, hours) -> None:
        _, _, _, region, network = droplet.name.split("-", 4)
        try:
            inspected = await self.remote_factory(record.hour).inspect(droplet)
        except (RemoteError, OSError, ValueError):  # ssh, its process, or a reply not JSON
            inspected = {"error": "inspect failed"}
        active = "assignment" in inspected
        if "error" in inspected or (active and not self._host(record.hour, droplet)):
            self.unreachable.add(droplet.id)  # still alive: the campaign is not finished
            self.log.add(
                self.clock(),
                "orphan_unreachable",
                "info",
                key=droplet.id,
                droplet_id=droplet.id,
                name=droplet.name,
            )
            return
        drop = DropletRecord(
            network,
            region,
            record.hour,
            droplet.name,
            droplet.id,
            size=droplet.size,
            status="running",
            requested_at=droplet.created_at.isoformat(),
        )
        record.droplets.append(drop)
        self.log.add(
            self.clock(),
            "orphan_adopted",
            "info",
            key=droplet.id,
            droplet_id=droplet.id,
            name=droplet.name,
            active=active,
        )
        if active:  # an active-run marker: harvest it normally
            if record.state in FINAL:
                record.state, record.report_path = "harvesting", None
            save_hour(self.root, record)
            return
        drop.error_class = "orphan_not_started"
        save_hour(self.root, record)
        await self._retire(scope, record, drop, hours)

    def _host(self, hour: int, droplet: Droplet) -> bool:
        """S6: add the worker to CAMP-1c's host map so it can be harvested; False (unreachable,
        retried) without an IPv4 (r2) or before CAMP-1c's launcher is installed (r4)."""
        try:
            from .launch import hosts_path, read_hosts
        except ImportError:
            return False
        if not droplet.public_ipv4:
            return False
        hosts = read_hosts(self.root, hour) | {droplet.id: droplet.public_ipv4}
        atomic_json(hosts_path(self.root, hour), hosts)
        return True

    async def _retire(self, scope, record: HourRecord, drop: DropletRecord, hours) -> None:
        known = {d.droplet_id: d for r in hours.values() for d in r.droplets if d.droplet_id}
        ledger = Ledger({"droplet": list(known)}, known)
        try:
            await guarded_delete(
                self.cloud, "droplet", drop.droplet_id, scope, ledger, audit_path(self.root)
            )
            drop.status, drop.destroyed_at = "destroyed", self.clock().isoformat()
        except OwnershipError:
            drop.status = "destroy_refused"
            self.log.add(
                self.clock(),
                "destroy_refused",
                key=drop.droplet_id,
                droplet_id=drop.droplet_id,
                name=drop.name,
            )
        except Exception as error:  # boundary: audited by the guard; the next reconcile retries
            drop.status = "destroy_failed"
            self.log.add(
                self.clock(),
                "destroy_failed",
                "info",
                key=f"{drop.droplet_id}-{type(error).__name__}",
                droplet_id=drop.droplet_id,
            )
        save_hour(self.root, record)
