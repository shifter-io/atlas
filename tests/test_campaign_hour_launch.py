"""The hour engine, launch: create -> armed, retries, replacements, resume and the guard.

Everything runs against the in-process fake DigitalOcean and a fake `Remote`; no request or
SSH connection leaves the machine. Addresses are RFC 5737 documentation addresses.
"""

from __future__ import annotations

import asyncio
import io
import json
import stat
import tarfile
from datetime import datetime

import pytest
from aiohttp import web
from campaign_fakes import CAMPAIGN_PROJECT, CID, FakeDigitalOcean, Fault
from campaign_hour_cases import (
    HOUR,
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

from proxy_index.campaign import fanout, launch
from proxy_index.campaign.contracts import (
    WORKER_TAG,
    HourRecord,
    audit_path,
    campaign_tag,
    droplet_name,
    hour_path,
    hour_tag,
)


async def test_a_full_hour_launches_16_networks_in_8_regions(tmp_path):
    """1: 16 creates and one confirmed assignment per region; all armed before HH:00."""
    fake = FakeDigitalOcean()
    fake.seed_production(40)
    spec = make_spec(NETWORKS, REGIONS)
    record, root, remote, _ = await launched(tmp_path, fake, spec)
    assert len(posts(fake, "/v2/droplets")) == 16
    assert len(posts(fake, f"/v2/projects/{CAMPAIGN_PROJECT}/resources")) == 8
    assert len(record.droplets) == 128 and len(remote.pushes) == 128
    assert {d.status for d in record.droplets} == {"armed"}
    assert all(
        d.armed_at < SCHEDULED and d.droplet_id in fake.projects[CAMPAIGN_PROJECT]
        for d in record.droplets
    )
    assert record.state == "running" and set(record.cells.values()) == {"running"}
    assert record.traffic["net00"]["countries_total"] == 16
    text = hour_path(root, 0).read_text()
    assert "203.0.113" not in text and "192.0.2" not in text
    hosts = launch.hosts_path(root, 0)
    assert stat.S_IMODE(hosts.stat().st_mode) == 0o600 and "203.0.113.10" in hosts.read_text()
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
            "wheel/proxy_index-1.0-py3-none-any.whl",
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
