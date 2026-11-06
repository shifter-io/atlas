"""The hour engine, launch: create -> armed, create retries, assignment and push failures.

Everything runs against the in-process fake DigitalOcean and a fake `Remote`; no request or
SSH connection leaves the machine. Addresses are RFC 5737 documentation addresses.
"""

from __future__ import annotations

import json
import stat
from datetime import datetime

import pytest
from campaign_fakes import CAMPAIGN_PROJECT, CID, FakeDigitalOcean, Fault
from campaign_hour_cases import (
    NETWORKS,
    PRE_CREATE,
    REGIONS,
    SCHEDULED,
    FakeClock,
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
    hour_path,
    hour_tag,
)


@pytest.mark.parametrize("region", REGIONS)
async def test_a_full_hour_of_16_networks_in_8_regions_launches_region(tmp_path, region):
    """1: two creates and one confirmed assignment per region; all armed before HH:00.

    One part per region (#338): every save rewrites the whole record, so launching all 128
    workers at once took 51.6 s under tier1's load. Each part plans the whole hour on the full
    spec, then launches its region's 16 workers."""
    fake = FakeDigitalOcean()
    fake.seed_production(40)
    spec = make_spec(NETWORKS, REGIONS)
    root = make_root(tmp_path, spec)
    record = HourRecord(CID, 0, SCHEDULED)
    planner = launch._Launch(spec, record, root, None, lambda hour: None, FakeClock(PRE_CREATE))
    planner._plan_workers()
    assert [(d.name, d.size, d.image_id) for d in record.droplets] == [
        (droplet_name(CID, 0, r, n), "c-2", f"snap-{r}") for n in NETWORKS for r in REGIONS
    ]
    assert {n: t["countries_total"] for n, t in record.traffic.items()} == dict.fromkeys(
        NETWORKS, 16
    )
    record.droplets = [d for d in record.droplets if d.region == region]
    record.cells = {c: s for c, s in record.cells.items() if c.endswith(f"/{region}")}
    record, root, remote, _ = await launched(tmp_path, fake, spec, record=record)
    assert len(posts(fake, "/v2/droplets")) == 2
    assert len(posts(fake, f"/v2/projects/{CAMPAIGN_PROJECT}/resources")) == 1
    names = sorted(droplet_name(CID, 0, region, n) for n in NETWORKS)
    assert sorted(d["name"] for d in fake.droplets.values() if d["name"].startswith("pic-")) == (
        names
    )
    assert len(record.droplets) == 16 and sorted(p[0] for p in remote.pushes) == names
    assert {d.status for d in record.droplets} == {"armed"}
    assert all(
        d.armed_at < SCHEDULED and d.droplet_id in fake.projects[CAMPAIGN_PROJECT]
        for d in record.droplets
    )
    assert record.state == "running" and len(record.cells) == 16
    assert set(record.cells.values()) == {"running"}
    text = hour_path(root, 0).read_text()
    assert "203.0.113" not in text and "192.0.2" not in text
    hosts = launch.hosts_path(root, 0)
    assert stat.S_IMODE(hosts.stat().st_mode) == 0o600 and "203.0.113.10" in hosts.read_text()
    assert set(launch.read_hosts(root, 0)) == {d.droplet_id for d in record.droplets}
    assert fake.deleted == [] and fake.unlisted == []
    tags = [d["tags"] for d in fake.droplets.values() if d["name"].startswith("pic-")]
    assert all(t == [WORKER_TAG, campaign_tag(CID), hour_tag(CID, 0)] for t in tags)


async def test_a_failed_create_is_retried_once_after_15_seconds(tmp_path):
    """2: a refused create batch is retried once; a second refusal is create_failed."""
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", "/v2/droplets", 422))
    record, *_, clock = await launched(tmp_path, fake)
    assert {d.attempts for d in record.droplets} == {1, 2}
    assert {d.status for d in record.droplets} == {"armed"}
    assert clock.t >= PRE_CREATE + launch.CREATE_RETRY_SECONDS

    fake = FakeDigitalOcean()  # every create refused: each cell spends its replacements
    fake.faults.append(Fault("POST", "/v2/droplets", 422, times=99))
    record, *_ = await launched(tmp_path / "again", fake)
    assert {d.status for d in record.droplets} == {"create_failed"}
    assert len(record.droplets) == 4 * (1 + launch.REPLACEMENTS_PER_CELL)
    assert set(record.cells.values()) == {"failed_ours"}
    assert {a["kind"] for a in record.alerts} == {"create_failed", "cell_failed"}


async def test_an_uncertain_create_adopts_its_names_and_retries_only_the_rest(tmp_path):
    """2: a lost answer after 10 of 12 names: those 10 are adopted, only 2 are created again."""
    fake = FakeDigitalOcean()
    fake.create_then_fail = 1
    spec = make_spec(NETWORKS[:12], REGIONS[:1])
    record, *_ = await launched(tmp_path, fake, spec)
    sizes = [len(r[2]) for r in posts(fake, "/v2/droplets")]
    assert len(sizes) == 2 and len(fake.droplets) == 12
    assert {d.status for d in record.droplets} == {"armed"}
    assert sorted(d.attempts for d in record.droplets) == [1] * 10 + [2] * 2


async def test_an_unconfirmed_assignment_is_never_pushed_and_is_replaced(tmp_path):
    """3: never confirmed -> never pushed, removed on the own-unassigned path, one replacement."""
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 400, times=3))
    spec = make_spec(NETWORKS[:1], REGIONS[:1])
    record, root, remote, _ = await launched(tmp_path, fake, spec)
    first, second = record.droplets
    assert first.name == second.name and first.error_class == "project_assign_failed"
    assert first.status == "destroyed" and fake.deleted == [("droplet", first.droplet_id)]
    assert [p[0] for p in remote.pushes] == [second.name] and second.status == "armed"
    audit = [json.loads(line) for line in audit_path(root).read_text().splitlines()]
    assert audit[-1]["path"] == "own_unassigned" and audit[-1]["result"] == "deleted"
    assert record.cells == {"net00/lon1": "running"}
    assert [a["kind"] for a in record.alerts] == ["project_assign_failed"]


async def test_ssh_never_ready_gets_its_replacements_before_the_cutoff(tmp_path):
    """4: replacements within 15 minutes of HH:00 or of a later launch (CAMP-1l), none after;
    all removed through the guard."""
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], REGIONS[:1])
    name = droplet_name(CID, 0, "lon1", "net00")
    record, *_ = await launched(tmp_path, fake, spec, setup=lambda r: r.never_ready.add(name))
    tries = 1 + launch.REPLACEMENTS_PER_CELL
    assert [(d.status, d.error_class) for d in record.droplets] == [
        ("destroyed", "boot_failed")
    ] * tries
    assert record.cells == {"net00/lon1": "failed_ours"} and len(fake.deleted) == tries
    assert [a["kind"] for a in record.alerts] == ["boot_failed", "cell_failed"]

    late = datetime.fromisoformat("2027-01-01T22:16:00+00:00").timestamp()
    fake = FakeDigitalOcean()
    record, *_ = await launched(
        tmp_path / "late", fake, spec, start=late, setup=lambda r: r.never_ready.add(name)
    )
    assert len(record.droplets) == tries  # a launch at HH:16 still has its own 15 minutes

    closed = make_spec(NETWORKS[:1], REGIONS[:1], replace_until_minutes=0)
    record, *_ = await launched(
        tmp_path / "closed",
        FakeDigitalOcean(),
        closed,
        start=late,
        setup=lambda r: r.never_ready.add(name),
    )
    assert len(record.droplets) == 1 and record.droplets[0].status == "destroyed"
    assert "cell_failed" in {a["kind"] for a in record.alerts}


async def test_a_push_error_is_retried_once_on_the_same_worker(tmp_path):
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], REGIONS[:1])
    name = droplet_name(CID, 0, "lon1", "net00")
    record, *_ = await launched(
        tmp_path, fake, spec, setup=lambda r: r.push_failures.update({name: 1})
    )
    assert len(record.droplets) == 1 and record.droplets[0].status == "armed"
    fake = FakeDigitalOcean()

    def never_started(remote):
        remote.push_failures[name] = 2
        remote.inspected[name] = {"service": {"ActiveState": "inactive"}}

    record, *_ = await launched(tmp_path / "twice", fake, spec, setup=never_started)
    assert [d.error_class for d in record.droplets] == ["start_failed", None]
    assert record.droplets[0].armed_at is None and record.droplets[1].status == "armed"


async def test_1e_r1_a_failed_push_on_an_unreadable_worker_is_armed_not_start_failed(tmp_path):
    """1e r1: the round may have started, so the worker goes to harvest, never start_failed."""
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], REGIONS[:1])
    name = droplet_name(CID, 0, "lon1", "net00")
    record, *_ = await launched(
        tmp_path, fake, spec, setup=lambda r: r.push_failures.update({name: 2})
    )
    worker = record.droplets[0]
    assert (worker.status, worker.error_class) == ("armed", "start_unconfirmed")
    assert worker.armed_at and len(record.droplets) == 1 and fake.deleted == []


@pytest.mark.parametrize(
    "seen",
    [
        {"service": {"ActiveState": "failed"}},
        {"assignment": {}, "service": {"ActiveState": "inactive"}},
    ],
)
async def test_1e_r1_a_failed_unit_after_a_failed_push_counts_as_started(tmp_path, seen):
    """m9: a unit that ran and failed, or the fleet marker alone, counts as started."""
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], REGIONS[:1])
    name = droplet_name(CID, 0, "lon1", "net00")

    def ran_and_failed(remote):
        remote.push_failures[name] = 1
        remote.inspected[name] = seen

    record, _, remote, _ = await launched(tmp_path, fake, spec, setup=ran_and_failed)
    assert (record.droplets[0].status, record.droplets[0].error_class) == ("armed", None)
    assert remote.pushes == [] and fake.deleted == []


async def test_1e_r1_an_error_after_arming_never_becomes_start_failed(tmp_path, monkeypatch):
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], REGIONS[:1])
    original, raised = launch._Launch.save, []

    def save(self):
        if not raised and any(d.status == "armed" for d in self.record.droplets):
            raised.append(True)
            raise OSError("disk full")
        original(self)

    monkeypatch.setattr(launch._Launch, "save", save)
    record, *_ = await launched(tmp_path, fake, spec)
    assert [d.status for d in record.droplets] == ["armed"] and fake.deleted == []
