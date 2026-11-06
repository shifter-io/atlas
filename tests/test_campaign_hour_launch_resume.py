"""The hour engine, launch: resume, failing regions and networks, synthetic mode.

Everything runs against the in-process fake DigitalOcean and a fake `Remote`; no request or
SSH connection leaves the machine. Addresses are RFC 5737 documentation addresses.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from campaign_fakes import CAMPAIGN_PROJECT, CID, FakeDigitalOcean
from campaign_hour_cases import (
    NETWORKS,
    PRE_CREATE,
    REGIONS,
    SCHEDULED,
    FakeClock,
    ListFailsAfterCreate,
    launched,
    make_root,
    make_spec,
    posts,
)

from proxy_index.campaign import launch
from proxy_index.campaign.contracts import (
    WORKER_TAG,
    HourRecord,
    audit_path,
    campaign_tag,
    droplet_name,
    hour_tag,
)


async def test_a_crashed_launch_resumes_without_creating_existing_names(tmp_path):
    """13: names persisted before a create that answered after a crash are adopted."""
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:2], REGIONS[:1])
    record = HourRecord(CID, 0, SCHEDULED, state="launching")
    for network in spec.networks:
        name = droplet_name(CID, 0, "lon1", network)
        record.droplets.append(
            launch.DropletRecord(
                network, "lon1", 0, name, attempts=1, requested_at="2027-01-01T21:58:30.000Z"
            )
        )
        fake.add(name, [WORKER_TAG, campaign_tag(CID), hour_tag(CID, 0)])
    fake.add(droplet_name(CID, 0, "lon1", "net00"), ["pi-camp-other"])  # same name, not ours
    for network in spec.networks:
        record.traffic[network] = dict.fromkeys(launch.TRAFFIC_KEYS, 0)
    record, *_ = await launched(tmp_path, fake, spec, record=record)
    assert posts(fake, "/v2/droplets") == []
    assert {d.status for d in record.droplets} == {"armed"}
    assert all("pi-camp-other" not in fake.droplets[d.droplet_id]["tags"] for d in record.droplets)


async def test_a_resumed_started_worker_is_not_pushed_again(tmp_path):
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], REGIONS[:1])
    name = droplet_name(CID, 0, "lon1", "net00")
    droplet_id = fake.add(
        name, [WORKER_TAG, campaign_tag(CID), hour_tag(CID, 0)], project=CAMPAIGN_PROJECT
    )
    record = HourRecord(CID, 0, SCHEDULED, state="launching")
    record.droplets.append(
        launch.DropletRecord(
            "net00",
            "lon1",
            0,
            name,
            droplet_id=droplet_id,
            status="starting",
            requested_at="2027-01-01T21:58:30.000Z",
        )
    )
    record.traffic["net00"] = dict.fromkeys(launch.TRAFFIC_KEYS, 0)
    record, _, remote, _ = await launched(
        tmp_path,
        fake,
        spec,
        record=record,
        setup=lambda r: r.inspected.update({name: {"service": {"ActiveState": "active"}}}),
    )
    assert remote.pushes == [] and record.droplets[0].status == "armed"
    again, *_ = await launched(tmp_path, fake, spec, record=record)
    assert again.launched_at == record.launched_at  # a running hour is left alone
    record.state = "launching"  # crashed after arming every worker: nothing to adopt or push
    again, _, remote, _ = await launched(tmp_path, fake, spec, record=record)
    assert again.state == "running" and remote.pushes == []
    record.state, record.droplets[0].status = "launching", "starting"
    again, _, remote, _ = await launched(tmp_path, fake, spec, record=record)
    assert len(remote.pushes) == 1 and again.droplets[0].status == "armed"  # inspect failed


async def test_one_region_failing_does_not_stop_the_others(tmp_path):
    """B1 (c): the failed region's workers end in a final status, not `requested`."""
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], REGIONS[:2], image_by_region={"lon1": "x", "fra1": "y"})
    original = launch._Launch._create_call

    async def broken(self, workers):
        if workers[0].region == "fra1":
            raise RuntimeError("boom")
        return await original(self, workers)

    launch._Launch._create_call = broken
    try:
        record, *_ = await launched(tmp_path, fake, spec)
    finally:
        launch._Launch._create_call = original
    assert {d.region: d.status for d in record.droplets} == {
        "lon1": "armed",
        "fra1": "create_failed",
    }
    assert record.cells == {"net00/lon1": "running", "net00/fra1": "failed_ours"}
    assert record.droplets[1].error_class == "RuntimeError"
    assert not {d.status for d in record.droplets} & set(launch.UP)


async def test_b1_a_readiness_api_error_retires_every_created_droplet(tmp_path):
    """B1 (a): no task outlives the launch; every created droplet goes through the guard."""
    fake = ListFailsAfterCreate()
    spec = make_spec(NETWORKS[:2], REGIONS[:1])
    record, root, remote, _ = await launched(tmp_path, fake, spec)
    created = {d.droplet_id for d in record.droplets if d.droplet_id}
    assert len(created) == 2 and remote.pushes == []  # the two first workers
    # Their replacements cannot list the hour tag, so they never create blind (no duplicate).
    assert {d.status for d in record.droplets if d.droplet_id} == {"destroyed"}
    assert {d.status for d in record.droplets if not d.droplet_id} == {"create_failed"}
    assert len(record.droplets) == 2 * (1 + launch.REPLACEMENTS_PER_CELL)
    assert {i for kind, i in fake.deleted} == created and len(fake.deleted) == 2
    audit = [json.loads(line) for line in audit_path(root).read_text().splitlines()]
    assert sorted(a["id"] for a in audit if a["result"] == "deleted") == sorted(created)
    assert set(record.cells.values()) == {"failed_ours"} and record.state == "running"
    assert {d.error_class for d in record.droplets} == {"ApiError"}
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


async def test_b1_b_a_missing_credentials_file_fails_only_its_network(tmp_path):
    """B1 (b): one network's missing credentials never stop the other networks' workers."""
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:2], REGIONS[:2])
    make_root(tmp_path, spec)
    (tmp_path / "manager" / ".secrets" / "credentials" / "net01.toml").unlink()
    record, *_ = await launched(tmp_path, fake, spec)
    good = [d for d in record.droplets if d.network == "net00"]
    bad = [d for d in record.droplets if d.network == "net01"]
    assert len(good) == 2 and {d.status for d in good} == {"armed"}
    assert len(bad) == 2 * (1 + launch.REPLACEMENTS_PER_CELL) and {
        (d.status, d.error_class) for d in bad
    } == {("destroyed", "FileNotFoundError")}
    assert {i for _, i in fake.deleted} == {d.droplet_id for d in bad}
    assert record.cells == {
        "net00/lon1": "running",
        "net00/fra1": "running",
        "net01/lon1": "failed_ours",
        "net01/fra1": "failed_ours",
    }


async def test_b1_an_assignment_error_leaves_no_worker_in_requested(tmp_path, monkeypatch):
    """A created droplet whose assignment raised is never confirmed: CAMP-1l removes it on the
    own-unassigned path (the plain guard refused it, project=false, and left it alive)."""
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], REGIONS[:1])

    async def refuse(*args, **kwargs):
        raise launch.scope.OwnershipError("droplet", "1", ["exists"])

    monkeypatch.setattr(launch.scope, "assign_and_verify_project", refuse)
    record, *_ = await launched(tmp_path, fake, spec)
    assert [d.status for d in record.droplets] == ["destroyed"] * (1 + launch.REPLACEMENTS_PER_CELL)
    assert len(fake.deleted) == 1 + launch.REPLACEMENTS_PER_CELL and not fake.droplets
    assert record.cells == {"net00/lon1": "failed_ours"}
    assert {d.error_class for d in record.droplets} == {"OwnershipError"}


async def test_synthetic_mode_makes_no_cloud_or_ssh_calls(tmp_path):
    """15: synthetic workers are armed without any client."""
    spec = make_spec(synthetic=True)
    root = tmp_path / "campaign"
    for network in spec.networks:
        manifest = root / spec.manifests[network]
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            json.dumps({"regions": {r: ["US"] for r in REGIONS}, "settings": {"concurrency": 2000}})
        )
    record = HourRecord(CID, 0, SCHEDULED)
    await launch.launch_hour(
        spec, record, root, cloud=None, remote_factory=None, clock=FakeClock(PRE_CREATE)
    )
    assert {d.status for d in record.droplets} == {"armed"} and record.state == "running"
    assert launch.load_hour(root, 0).to_dict() == record.to_dict()


async def test_the_launch_refuses_a_wheel_the_spec_does_not_bind(tmp_path):
    spec = make_spec(wheel_sha256="0" * 64)
    with pytest.raises(ValueError, match="wheel"):
        await launched(tmp_path, FakeDigitalOcean(), spec)
