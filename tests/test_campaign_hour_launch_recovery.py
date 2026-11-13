"""The hour engine, launch: the guard, boot failures, in-flight creates and the bundle.

Everything runs against the in-process fake DigitalOcean and a fake `Remote`; no request or
SSH connection leaves the machine. Addresses are RFC 5737 documentation addresses.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile

import pytest
from aiohttp import web
from campaign_fakes import CAMPAIGN_PROJECT, CID, FakeDigitalOcean, Fault
from campaign_hour_cases import (
    HOUR,
    NETWORKS,
    PRE_CREATE,
    REGIONS,
    SCHEDULED,
    launched,
    make_root,
    make_spec,
    posts,
)

from atlas.campaign import fanout, launch
from atlas.campaign.contracts import HourRecord, audit_path, droplet_name


async def test_a_guard_refusal_is_final_with_no_fallback(tmp_path):
    """8: a worker that lost its markers is refused, never deleted another way."""
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], REGIONS[:1])
    name = droplet_name(CID, 0, "lon1", "net00")

    def strip_tags(remote):
        async def never(droplet, limit):
            fake.droplets[droplet.id]["tags"] = ["prod"]
            return False

        remote.wait_ready = never

    record, root, *_ = await launched(tmp_path, fake, spec, setup=strip_tags)
    assert record.droplets[0].status == "destroy_refused" and fake.deleted == []
    assert {a["kind"] for a in record.alerts} >= {"boot_failed", "destroy_refused"}
    assert record.droplets[1].name == name
    audit = [json.loads(line)["result"] for line in audit_path(root).read_text().splitlines()]
    assert "refused" in audit


async def test_a_guard_api_error_leaves_destroy_failed_for_the_next_pass(tmp_path):
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("DELETE", "/v2/droplets/700001", 422))
    spec = make_spec(NETWORKS[:1], REGIONS[:1])
    name = droplet_name(CID, 0, "lon1", "net00")
    record, *_ = await launched(tmp_path, fake, spec, setup=lambda r: r.never_ready.add(name))
    first = record.droplets[0]
    assert (first.status, first.error_class) == ("destroy_failed", "boot_failed")
    assert "700001" in fake.droplets


class NeverActive(FakeDigitalOcean):
    def add(self, *args, **kwargs):
        droplet_id = super().add(*args, **kwargs)
        self.droplets[droplet_id]["status"] = "new"
        return droplet_id


async def test_a_droplet_never_active_within_180_seconds_is_boot_failed(tmp_path):
    fake = NeverActive()
    spec = make_spec(NETWORKS[:2], REGIONS[:1])
    record, root, remote, clock = await launched(tmp_path, fake, spec)
    assert {(d.status, d.error_class) for d in record.droplets} == {("destroyed", "boot_failed")}
    assert remote.pushes == [] and not launch.hosts_path(root, 0).exists()
    # Confirmed in the project before it failed: the plain guard, never the own-unassigned mark.
    assert all(d.assigned_at and d.assign_failed_at is None for d in record.droplets)
    audit = [json.loads(line) for line in audit_path(root).read_text().splitlines()]
    assert audit and {a["path"] for a in audit} == {"normal"}
    lists = [r for r in fake.requests if r[0] == "GET" and r[2].get("tag_name")]
    assert len(lists) <= 2 * (1 + launch.REPLACEMENTS_PER_CELL) * (
        launch.READY_LIMIT_SECONDS / launch.READY_POLL_SECONDS + 2
    )


async def test_m1_a_replacement_does_not_delay_the_confirmed_workers(tmp_path):
    """m1: the confirmed worker is pushed while the replacement is still booting."""
    spec = make_spec(NETWORKS[:2], REGIONS[:1])
    late = droplet_name(CID, 0, "lon1", "net01")

    class DropOne(FakeDigitalOcean):
        """The first droplet named `late` never joins the project."""

        async def _assign(self, request):
            response = await super()._assign(request)
            self.first = getattr(self, "first", None) or min(
                i for i, d in self.droplets.items() if d["name"] == late
            )
            self.projects[CAMPAIGN_PROJECT].discard(self.first)
            return response

    def setup(remote):
        async def wait_ready(droplet, limit):
            if droplet.name == late:  # booting until the confirmed worker has been pushed
                for _ in range(200):
                    if remote.pushes:
                        return True
                    await asyncio.sleep(0.01)
                return False
            return True

        remote.wait_ready = wait_ready

    fake = DropOne()
    record, _, remote, _ = await launched(tmp_path, fake, spec, setup=setup)
    assert [d.status for d in record.droplets if d.name == late] == ["destroyed", "armed"]
    assert remote.pushes[0][0] == droplet_name(CID, 0, "lon1", "net00")


class HiddenOnce(FakeDigitalOcean):
    """The create answer is lost and the first list does not show the droplet yet."""

    hide = True

    async def _list(self, request):
        if self.hide and request.query.get("tag_name"):
            self.hide = False
            return web.json_response({"droplets": [], "meta": {"total": 0}})
        return await super()._list(request)


async def test_m2_the_retry_adopts_a_create_that_was_still_in_flight(tmp_path):
    fake = HiddenOnce()
    fake.create_then_fail = 1
    spec = make_spec(NETWORKS[:1], REGIONS[:1])
    record, *_ = await launched(tmp_path, fake, spec)
    assert len(fake.droplets) == 1 and len(posts(fake, "/v2/droplets")) == 1
    assert record.droplets[0].status == "armed" and record.droplets[0].attempts == 1


async def test_m3_a_push_that_broke_after_starting_is_not_pushed_again(tmp_path):
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], REGIONS[:1])
    name = droplet_name(CID, 0, "lon1", "net00")

    def setup(remote):
        async def push_start(droplet, payload, command):
            remote.pushes.append((droplet.name, payload, command))
            remote.inspected[droplet.name] = {"service": {"ActiveState": "active"}}
            raise fanout.RemoteError("push and start failed (exit 255)")

        remote.push_start = push_start

    record, _, remote, _ = await launched(tmp_path, fake, spec, setup=setup)
    assert len(record.droplets) == 1 and record.droplets[0].status == "armed"
    assert [p[0] for p in remote.pushes] == [name]


async def test_m5_a_manifest_with_another_concurrency_is_refused(tmp_path):
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], REGIONS[:1])
    root = make_root(tmp_path, spec)
    manifest = root / spec.manifests["net00"]
    manifest.write_text(
        json.dumps({"regions": {"lon1": ["US"]}, "settings": {"concurrency": 2500}})
    )
    with pytest.raises(ValueError, match="concurrency"):
        await launched(tmp_path, fake, spec)
    assert posts(fake, "/v2/droplets") == []


async def test_overlap_counts_live_workers_of_earlier_hours(tmp_path):
    """5: the next hour's worker starts while the earlier round still runs, overlap 1."""
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], REGIONS[:1])
    first, root, *_ = await launched(tmp_path, fake, spec)
    assert first.droplets[0].overlap == 0
    fake2 = FakeDigitalOcean()
    record = HourRecord(CID, 1, "2027-01-01T23:00:00Z")
    second, *_ = await launched(tmp_path, fake2, spec, start=PRE_CREATE + HOUR, record=record)
    assert second.droplets[0].overlap == 1 and second.droplets[0].status == "armed"
    assert first.droplets[0].status == "armed"


async def test_the_bundle_holds_only_its_own_network_and_argv_no_secret(tmp_path):
    """9: one network's credentials per worker, inside the tar only."""
    fake = FakeDigitalOcean()
    record, root, remote, _ = await launched(tmp_path, fake)
    for name, payload, command in remote.pushes:
        network = next(d.network for d in record.droplets if d.name == name)
        region = next(d.region for d in record.droplets if d.name == name)
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            names = archive.getnames()
            secret = archive.extractfile("secrets/credentials.toml").read().decode()
            assignment = json.loads(archive.extractfile(f"run/m-{network}/assignment.json").read())
            modes = {m.mode for m in archive.getmembers()}
        assert secret == f"password = 'secret-{network}'\n" and modes == {0o600}
        assert names == [
            f"run/m-{network}/campaign.json",
            f"run/m-{network}/provider.toml",
            f"run/m-{network}/assignment.json",
            "wheel/atlas-1.0-py3-none-any.whl",
            "secrets/credentials.toml",
        ]
        assert assignment["scheduled_at"] == SCHEDULED and assignment["name"] == name
        assert command == fanout.start_command(f"m-{network}", region, 0)
        assert not any("secret" in part for part in command[3:])


async def test_the_pre_create_waits_for_hh_58_30_and_the_round_for_hh_00(tmp_path):
    """10: an early tick waits for the create lead; the round itself waits for HH:00."""
    fake = FakeDigitalOcean()
    record, *_ = await launched(tmp_path, fake, start=PRE_CREATE - 120)
    assert {d.requested_at for d in record.droplets} == {"2027-01-01T21:58:30.000Z"}
    assert record.scheduled_at == SCHEDULED
