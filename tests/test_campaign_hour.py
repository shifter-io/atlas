"""The hour engine: launch (create -> armed), harvest, OpenSSH fan-out, the worker sampler.

Everything runs against the in-process fake DigitalOcean and a fake `Remote`; no request or
SSH connection leaves the machine. Addresses are RFC 5737 documentation addresses.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

import pytest
from aiohttp import web
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    CID,
    MANAGER_ID,
    SPEC_TIME,
    FakeDigitalOcean,
    Fault,
    running,
)
from test_onboarding_round import make as make_manifest
from test_onboarding_round import run_round

from proxy_index.campaign import fanout, harvest, launch, sampler
from proxy_index.campaign.contracts import (
    WORKER_ROOT,
    WORKER_SECRETS,
    WORKER_TAG,
    CampaignSpec,
    Droplet,
    HourRecord,
    audit_path,
    campaign_tag,
    droplet_name,
    hour_path,
    hour_tag,
)

REGIONS = ("lon1", "fra1", "ams3", "nyc3", "tor1", "sgp1", "blr1", "syd1")
NETWORKS = tuple(f"net{i:02d}" for i in range(16))
SCHEDULED = "2027-01-01T22:00:00Z"
PRE_CREATE = datetime.fromisoformat("2027-01-01T21:58:30+00:00").timestamp()
HOUR = 3600.0


class FakeClock:
    def __init__(self, start: float):
        self.t = start

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += max(seconds, 0.0)
        await asyncio.sleep(0)


class FakeRemote:
    """Answers per droplet name; records every push."""

    def __init__(self, clock: FakeClock):
        self.clock, self.never_ready, self.push_failures = clock, set(), {}
        self.pushes, self.inspected = [], {}

    async def wait_ready(self, droplet, limit_seconds):
        if droplet.name in self.never_ready:
            self.clock.t += limit_seconds
            return False
        return True

    async def push_start(self, droplet, payload, command):
        if self.push_failures.get(droplet.name, 0):
            self.push_failures[droplet.name] -= 1
            raise fanout.RemoteError("push and start failed (exit 1)")
        self.pushes.append((droplet.name, payload, command))

    async def inspect(self, droplet):
        if droplet.name not in self.inspected:
            raise fanout.RemoteError("inspect failed (exit 255)")
        return self.inspected[droplet.name]


def make_spec(networks=NETWORKS[:2], regions=REGIONS[:2], **changes) -> CampaignSpec:
    values = dict(
        campaign_id=CID, created_at=SPEC_TIME, account="leftclick", project_id=CAMPAIGN_PROJECT,
        starts_at=SCHEDULED, grace_hours=48, regions=tuple(regions), networks=tuple(networks),
        manifests={n: f"networks/{n}/residential/m-{n}/campaign.json" for n in networks},
        size_by_region=dict.fromkeys(regions, "c-2"),
        image_by_region={r: f"snap-{r}" for r in regions}, image_name="pi-camp-worker-x",
        wheel_sha256=hashlib.sha256(b"wheel bytes").hexdigest(), framework_version="1.0",
        ssh_key_id="4242", manager_droplet_id=MANAGER_ID)
    return CampaignSpec(**(values | changes)).validate()


def make_root(tmp_path: Path, spec: CampaignSpec) -> Path:
    """The manager layout: campaign root, manifests, run configs, wheel and credentials."""
    base = tmp_path / "manager"
    root = base / "campaigns" / spec.campaign_id
    for network in spec.networks:
        manifest = root / spec.manifests[network]
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"regions": {r: ["US", "GB"] for r in REGIONS},
                                        "settings": {"concurrency": spec.concurrency}}))
        (manifest.parent / "provider.toml").write_text(f"provider = '{network}'\n")
        secrets = base / ".secrets" / "credentials"
        secrets.mkdir(parents=True, exist_ok=True)
        (secrets / f"{network}.toml").write_text(f"password = 'secret-{network}'\n")
    (base / "wheels").mkdir()
    (base / "wheels" / "proxy_index-1.0-py3-none-any.whl").write_bytes(b"wheel bytes")
    (base / "wheels" / "proxy_index-0.9-py3-none-any.whl").write_bytes(b"old wheel")
    return root


async def launched(tmp_path, fake, spec=None, *, start=PRE_CREATE, record=None, setup=None):
    spec = spec or make_spec()
    root = make_root(tmp_path, spec) if not (tmp_path / "manager").exists() else (
        tmp_path / "manager" / "campaigns" / spec.campaign_id)
    clock = FakeClock(start)
    remote = FakeRemote(clock)
    if setup:
        setup(remote)
    record = record or HourRecord(CID, 0, SCHEDULED)
    async with running(fake) as cloud:
        await launch.launch_hour(spec, record, root, cloud=cloud,
                                 remote_factory=lambda hour: remote, clock=clock)
    return record, root, remote, clock


def posts(fake, path):
    return [r for r in fake.requests if r[0] == "POST" and r[1] == path]


def by_name(record, name):
    return [d for d in record.droplets if d.name == name]


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
    assert all(d.armed_at < SCHEDULED and d.droplet_id in fake.projects[CAMPAIGN_PROJECT]
               for d in record.droplets)
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
    """4: replacements before HH:15, none after; all removed through the guard."""
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], REGIONS[:1])
    name = droplet_name(CID, 0, "lon1", "net00")
    record, *_ = await launched(tmp_path, fake, spec,
                                setup=lambda r: r.never_ready.add(name))
    tries = 1 + launch.REPLACEMENTS_PER_CELL
    assert [(d.status, d.error_class) for d in record.droplets] == [
        ("destroyed", "boot_failed")] * tries
    assert record.cells == {"net00/lon1": "failed_ours"} and len(fake.deleted) == tries
    assert [a["kind"] for a in record.alerts] == ["boot_failed", "cell_failed"]

    late = datetime.fromisoformat("2027-01-01T22:16:00+00:00").timestamp()
    fake = FakeDigitalOcean()
    record, *_ = await launched(tmp_path / "late", fake, spec, start=late,
                                setup=lambda r: r.never_ready.add(name))
    assert len(record.droplets) == 1 and record.droplets[0].status == "destroyed"
    assert "cell_failed" in {a["kind"] for a in record.alerts}


async def test_a_push_error_is_retried_once_on_the_same_worker(tmp_path):
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], REGIONS[:1])
    name = droplet_name(CID, 0, "lon1", "net00")
    record, *_ = await launched(tmp_path, fake, spec,
                                setup=lambda r: r.push_failures.update({name: 1}))
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
    record, *_ = await launched(tmp_path, fake, spec,
                                setup=lambda r: r.push_failures.update({name: 2}))
    worker = record.droplets[0]
    assert (worker.status, worker.error_class) == ("armed", "start_unconfirmed")
    assert worker.armed_at and len(record.droplets) == 1 and fake.deleted == []


@pytest.mark.parametrize("seen", [{"service": {"ActiveState": "failed"}},
                                  {"assignment": {}, "service": {"ActiveState": "inactive"}}])
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
    lists = [r for r in fake.requests if r[0] == "GET" and r[2].get("tag_name")]
    assert len(lists) <= 2 * (1 + launch.REPLACEMENTS_PER_CELL) * (
        launch.READY_LIMIT_SECONDS / launch.READY_POLL_SECONDS + 2)


async def test_m1_a_replacement_does_not_delay_the_confirmed_workers(tmp_path):
    """m1: the confirmed worker is pushed while the replacement is still booting."""
    spec = make_spec(NETWORKS[:2], REGIONS[:1])
    late = droplet_name(CID, 0, "lon1", "net01")

    class DropOne(FakeDigitalOcean):
        """The first droplet named `late` never joins the project."""

        async def _assign(self, request):
            response = await super()._assign(request)
            self.first = getattr(self, "first", None) or min(
                i for i, d in self.droplets.items() if d["name"] == late)
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
    manifest.write_text(json.dumps({"regions": {"lon1": ["US"]},
                                    "settings": {"concurrency": 2500}}))
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
            assignment = json.loads(archive.extractfile(
                f"run/m-{network}/assignment.json").read())
            modes = {m.mode for m in archive.getmembers()}
        assert secret == f"password = 'secret-{network}'\n" and modes == {0o600}
        assert names == [f"run/m-{network}/campaign.json", f"run/m-{network}/provider.toml",
                         f"run/m-{network}/assignment.json",
                         "wheel/proxy_index-1.0-py3-none-any.whl", "secrets/credentials.toml"]
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
        record.droplets.append(launch.DropletRecord(network, "lon1", 0, name, attempts=1,
                                                    requested_at="2027-01-01T21:58:30.000Z"))
        fake.add(name, [WORKER_TAG, campaign_tag(CID), hour_tag(CID, 0)])
    fake.add(droplet_name(CID, 0, "lon1", "net00"), ["pi-camp-other"])  # same name, not ours
    for network in spec.networks:
        record.traffic[network] = dict.fromkeys(launch.TRAFFIC_KEYS, 0)
    record, *_ = await launched(tmp_path, fake, spec, record=record)
    assert posts(fake, "/v2/droplets") == []
    assert {d.status for d in record.droplets} == {"armed"}
    assert all("pi-camp-other" not in fake.droplets[d.droplet_id]["tags"]
               for d in record.droplets)


async def test_a_resumed_started_worker_is_not_pushed_again(tmp_path):
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], REGIONS[:1])
    name = droplet_name(CID, 0, "lon1", "net00")
    droplet_id = fake.add(name, [WORKER_TAG, campaign_tag(CID), hour_tag(CID, 0)],
                          project=CAMPAIGN_PROJECT)
    record = HourRecord(CID, 0, SCHEDULED, state="launching")
    record.droplets.append(launch.DropletRecord(
        "net00", "lon1", 0, name, droplet_id=droplet_id, status="starting",
        requested_at="2027-01-01T21:58:30.000Z"))
    record.traffic["net00"] = dict.fromkeys(launch.TRAFFIC_KEYS, 0)
    record, _, remote, _ = await launched(
        tmp_path, fake, spec, record=record,
        setup=lambda r: r.inspected.update({name: {"service": {"ActiveState": "active"}}}))
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
    assert {d.region: d.status for d in record.droplets} == {"lon1": "armed",
                                                             "fra1": "create_failed"}
    assert record.cells == {"net00/lon1": "running", "net00/fra1": "failed_ours"}
    assert record.droplets[1].error_class == "RuntimeError"
    assert not {d.status for d in record.droplets} & set(launch.UP)


class ListFailsAfterCreate(FakeDigitalOcean):
    """Every hour-tag list fails (5xx after the client's retries) once droplets exist."""

    broken = False

    async def _create(self, request):
        response = await super()._create(request)
        self.broken = True
        return response

    async def _list(self, request):
        if self.broken:
            return web.json_response({"id": "server_error"}, status=500)
        return await super()._list(request)


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
        (d.status, d.error_class) for d in bad} == {
        ("destroyed", "FileNotFoundError")}
    assert {i for _, i in fake.deleted} == {d.droplet_id for d in bad}
    assert record.cells == {"net00/lon1": "running", "net00/fra1": "running",
                            "net01/lon1": "failed_ours", "net01/fra1": "failed_ours"}


async def test_b1_an_assignment_error_leaves_no_worker_in_requested(tmp_path, monkeypatch):
    """A created droplet whose assignment raised is never confirmed: CAMP-1l removes it on the
    own-unassigned path (the plain guard refused it, project=false, and left it alive)."""
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], REGIONS[:1])

    async def refuse(*args, **kwargs):
        raise launch.scope.OwnershipError("droplet", "1", ["exists"])

    monkeypatch.setattr(launch.scope, "assign_and_verify_project", refuse)
    record, *_ = await launched(tmp_path, fake, spec)
    assert [d.status for d in record.droplets] == ["destroyed"] * (
        1 + launch.REPLACEMENTS_PER_CELL)
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
        manifest.write_text(json.dumps({"regions": {r: ["US"] for r in REGIONS},
                                        "settings": {"concurrency": 2000}}))
    record = HourRecord(CID, 0, SCHEDULED)
    await launch.launch_hour(spec, record, root, cloud=None, remote_factory=None,
                             clock=FakeClock(PRE_CREATE))
    assert {d.status for d in record.droplets} == {"armed"} and record.state == "running"
    assert launch.load_hour(root, 0).to_dict() == record.to_dict()


async def test_the_launch_refuses_a_wheel_the_spec_does_not_bind(tmp_path):
    spec = make_spec(wheel_sha256="0" * 64)
    with pytest.raises(ValueError, match="wheel"):
        await launched(tmp_path, FakeDigitalOcean(), spec)


# --- OpenSSH fan-out -------------------------------------------------------------------------

DROPLET = Droplet("700001", "pic-01234567-h00-lon1-net00", "lon1", "c-2", "active",
                  datetime.fromisoformat(SPEC_TIME), (), public_ipv4="203.0.113.7")


class FakeRunner:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    async def __call__(self, argv, stdin, seconds, sink, limit):
        self.calls.append((argv, stdin))
        code, out = self.answers.pop(0) if self.answers else (0, b"")
        if len(out) > limit:
            return 254
        sink.write(out)
        return code


def test_the_openssh_options(tmp_path):
    """11: batch mode, per-hour known hosts, accept-new, control master, no secret in argv."""
    runtime = tmp_path / ".runtime"
    argv = fanout.OpenSSHRemote(tmp_path / "key", runtime, 7).argv(DROPLET, ["true"])
    options = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-o")
    assert options == {
        "BatchMode": "yes", "IdentitiesOnly": "yes", "User": "root",
        "UserKnownHostsFile": str(runtime / "known_hosts-07"),
        "StrictHostKeyChecking": "accept-new", "ConnectTimeout": "5",
        "ServerAliveInterval": "10", "ServerAliveCountMax": "3", "ControlMaster": "auto",
        "ControlPath": str(fanout.control_dir(runtime) / "cm-%C"), "ControlPersist": "4h"}
    assert argv[:3] == ["ssh", "-i", str(tmp_path / "key")] and argv[-3:] == [
        "203.0.113.7", "--", "true"]
    assert fanout.control_dir(Path("/r")) == Path("/r")
    long = Path("/opt/proxy-index-campaign/campaigns") / CID / ".runtime"
    assert fanout.control_dir(long) == Path("/tmp") / f"pi-camp-cm-{os.getuid()}"
    with pytest.raises(fanout.RemoteError):
        fanout.OpenSSHRemote(tmp_path, runtime, 0).argv(
            Droplet("1", "n", "lon1", "c-2", "new", DROPLET.created_at, ()), ["true"])


async def test_the_remote_steps_send_fixed_commands(tmp_path):
    runtime = tmp_path / "rt"
    inspect = json.dumps({"assignment": {"unit": "pi-camp-round"}}).encode()
    runner = FakeRunner((0, b""), (0, inspect), (0, b""), (0, b"journal"))
    remote = fanout.OpenSSHRemote(tmp_path / "key", runtime, 0, runner=runner)
    await remote.push_start(DROPLET, b"payload", fanout.start_command("m", "lon1", 0))
    assert (await remote.inspect(DROPLET))["assignment"]["unit"] == "pi-camp-round"
    await remote.stop(DROPLET)
    await remote.journal(DROPLET, tmp_path / "logs" / "net00-lon1.journal.txt")
    commands = [call[0][-1] for call in runner.calls]
    assert runner.calls[0][1] == b"payload" and WORKER_ROOT in commands[0]
    assert commands[2] == "systemctl kill --signal=SIGTERM pi-camp-round"
    assert "tail -c 5000000" in commands[3]
    journal = tmp_path / "logs" / "net00-lon1.journal.txt"
    assert journal.read_bytes() == b"journal" and stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
    with pytest.raises(fanout.RemoteError, match=r"^inspect failed \(exit 255\)$"):
        await fanout.OpenSSHRemote(tmp_path, runtime, 0, runner=FakeRunner(
            (255, b"203.0.113.7 refused"))).inspect(DROPLET)


async def test_wait_ready_retries_until_its_limit(tmp_path):
    clock = FakeClock(0.0)
    runner = FakeRunner((255, b""), (255, b""), (0, b""))
    remote = fanout.OpenSSHRemote(tmp_path, tmp_path / "rt", 0, runner=runner,
                                  sleep=clock.sleep, clock=clock.now)
    assert await remote.wait_ready(DROPLET, 30) and clock.t == 4.0
    never = fanout.OpenSSHRemote(tmp_path, tmp_path / "rt", 0, runner=FakeRunner(
        *[(255, b"")] * 100), sleep=clock.sleep, clock=clock.now)
    assert not await never.wait_ready(DROPLET, 10)


def tar_bytes(*members) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def member(name, data=b"x", kind=tarfile.REGTYPE, link=""):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, link
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    return info, data if kind == tarfile.REGTYPE else None


async def test_pull_replaces_the_local_round_with_the_remote_one(tmp_path):
    local = tmp_path / "regions" / "lon1" / "rounds" / "00"
    local.mkdir(parents=True)
    (local / "stale.json").write_text("{}")
    archive = tar_bytes(member("00/round.json", b"{}"), member("00/us/summary.json", b"{}"),
                        member("00/host-samples.jsonl", b""))
    runner = FakeRunner((0, archive))
    remote = fanout.OpenSSHRemote(tmp_path, tmp_path / "rt", 0, runner=runner)
    await remote.pull(DROPLET, f"{WORKER_ROOT}/run/m/regions/lon1/rounds/00", local)
    assert sorted(p.name for p in local.rglob("*")) == [
        "host-samples.jsonl", "round.json", "summary.json", "us"]
    assert "mkdir -p /opt/proxy-index-worker/run/m/regions/lon1/rounds/00 && tar -C " \
        "/opt/proxy-index-worker/run/m/regions/lon1/rounds -cf - 00" in runner.calls[0][0][-1]
    assert sorted(p.name for p in local.parent.iterdir()) == ["00"]


@pytest.mark.parametrize("bad", [
    member("/etc/passwd"), member("../escape"), member("00/../../escape"),
    member("00/link", kind=tarfile.SYMTYPE, link="/etc/passwd"),
    member("00/inner", kind=tarfile.SYMTYPE, link="round.json"),
    member("00/hard", kind=tarfile.LNKTYPE, link="00/round.json"),
    member("00/device", kind=tarfile.CHRTYPE),
])
async def test_unsafe_tar_entries_are_rejected(tmp_path, bad):
    """12: absolute paths, `..`, links and devices never land on the manager."""
    local = tmp_path / "rounds" / "00"
    runner = FakeRunner((0, tar_bytes(member("00/round.json", b"{}"), bad)))
    remote = fanout.OpenSSHRemote(tmp_path, tmp_path / "rt", 0, runner=runner)
    with pytest.raises(tarfile.TarError):
        await remote.pull(DROPLET, f"{WORKER_ROOT}/run/m/regions/lon1/rounds/00", local)
    assert not local.exists() and not (tmp_path / "escape").exists()
    assert list(local.parent.iterdir()) == []


async def test_run_process_streams_caps_and_times_out():
    """m8: a worker can never send more than the cap; the process is killed."""
    sink = io.BytesIO()
    echo = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
    assert await fanout.run_process(echo, b"hello", 30, sink, 10) == 0
    assert sink.getvalue() == b"hello"
    flood = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1_000_000)"]
    assert await fanout.run_process(flood, None, 30, io.BytesIO(), 1000) == 254
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    assert await fanout.run_process(sleeper, None, 0.2, io.BytesIO(), 10) == 255
    gone = [sys.executable, "-c", "pass"]
    assert await fanout.run_process(gone, b"x" * 5_000_000, 30, io.BytesIO(), 10) == 0


async def test_oversized_worker_output_is_refused(tmp_path):
    runner = FakeRunner((0, b"x" * (fanout.OUTPUT_BYTES + 1)))
    remote = fanout.OpenSSHRemote(tmp_path, tmp_path / "rt", 0, runner=runner)
    with pytest.raises(fanout.RemoteError, match="exit 254"):
        await remote.inspect(DROPLET)


def test_a_control_directory_of_another_user_is_refused(tmp_path, monkeypatch):
    """m4: the multiplexed channel never uses a directory another user planted."""
    planted = tmp_path / "planted"
    planted.mkdir()
    fanout.owned_dir(planted)
    monkeypatch.setattr(fanout.os, "getuid", lambda: planted.stat().st_uid + 1)
    with pytest.raises(fanout.RemoteError, match="another user"):
        fanout.owned_dir(planted)


def stub_systemctl(tmp_path: Path, state: str | None) -> dict:
    """A `systemctl` that prints `state` (or fails when it is None); returns the env."""
    bin_dir = tmp_path / "bin-systemctl"
    bin_dir.mkdir(exist_ok=True)
    body = f"printf '{state}\\n'" if state is not None else "exit 1"
    (bin_dir / "systemctl").write_text(f"#!/bin/sh\n{body}\n")
    (bin_dir / "systemctl").chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


@pytest.mark.parametrize("checkpoint,unit,allowed", [
    ("running", "ActiveState=active\nSubState=running", False),
    ("armed", "ActiveState=activating\nSubState=start", False),
    ("running", None, False),  # systemctl itself failed: keep refusing (data first)
    ("running", "ActiveState=failed\nSubState=failed", True),  # killed round (B2)
    ("running", "ActiveState=inactive\nSubState=dead", True),  # unit gone after a reboot
    ("running", "ActiveState=active\nSubState=exited", True),
    ("completed", "ActiveState=active\nSubState=running", True),
    (None, "ActiveState=failed\nSubState=failed", True),
])
def test_b2_the_pull_check_allows_a_round_whose_unit_ended(tmp_path, checkpoint, unit, allowed):
    """B2: a hard-killed round (checkpoint never terminal) can still be pulled."""
    folder = tmp_path / "00"
    folder.mkdir()
    if checkpoint:
        (folder / "round.json").write_text(json.dumps({"status": checkpoint}))
    reply = subprocess.run([sys.executable, "-c", fanout.TERMINAL_CHECK, str(folder)],
                           env=stub_systemctl(tmp_path, unit), check=False)
    assert (reply.returncode == 0) is allowed


def test_the_start_script_installs_arms_and_keeps_secrets_off_argv(tmp_path):
    """9: secrets only from stdin to a 0600 file; wheel installed only when its SHA differs."""
    bin_dir, log = tmp_path / "bin", tmp_path / "calls.log"
    bin_dir.mkdir()
    for tool, out in (("uv", ""), ("systemd-run", ""), ("timedatectl", "yes")):
        (bin_dir / tool).write_text(f"#!/bin/sh\necho \"{tool} $*\" >> {log}\necho {out}\n")
        (bin_dir / tool).chmod(0o755)
    payload = tar_bytes(member("run/m-net00/campaign.json", b"{}"),
                        member("run/m-net00/provider.toml", b""),
                        member("run/m-net00/assignment.json", b"{}"),
                        member("wheel/proxy_index-1.0-py3-none-any.whl", b"wheel"),
                        member("secrets/credentials.toml", b"password = 'secret-x'\n"))
    worker, secrets = tmp_path / "worker", tmp_path / "secrets"
    command = fanout.start_command("m-net00", "lon1", 3)
    assert command[3:5] == [WORKER_ROOT, WORKER_SECRETS]
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    for _ in range(2):
        subprocess.run([sys.executable, "-c", command[2], str(worker), str(secrets),
                        *command[5:]], input=payload, env=env, check=True)
    calls = log.read_text().splitlines()
    assert [c.split()[0] for c in calls] == [
        "uv", "timedatectl", "systemd-run", "systemd-run", "timedatectl", "systemd-run",
        "systemd-run"]
    assert "--property=LimitNOFILE=65536" in calls[2] and "--round 3" in calls[2]
    assert "proxy_index.campaign.sampler" in calls[3] and "secret-x" not in log.read_text()
    credentials = secrets / "credentials.toml"
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert not list(worker.rglob("credentials.toml"))
    marker = json.loads((worker / "active-run.json").read_text())
    assert marker == {"kind": "round", "region": "lon1", "unit": "pi-camp-round",
                      "state_path": "run/m-net00/regions/lon1/rounds/03/round.json"}
    start = json.loads((worker / "run/m-net00/regions/lon1/rounds/03/worker-start.json")
                       .read_text())
    assert start["ntp_synchronized"] is True and start["wheel_installed"] is False


# --- worker sampler --------------------------------------------------------------------------

STAT_1 = "cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 50 0 50 350 50 0 0 0\ncpu1 50 0 50 350 50\n"
STAT_2 = "cpu  200 0 200 800 100 0 0 100 0 0\ncpu0 150 0 150 350 50 0 0 0\ncpu1 50 0 50 400 50\n"


def proc_tree(tmp_path: Path, stat_text: str, pids=("41",)) -> tuple[Path, Path]:
    proc, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    proc.mkdir(exist_ok=True)
    cgroup.mkdir(exist_ok=True)
    (proc / "stat").write_text(stat_text)
    (proc / "loadavg").write_text("1.50 1.00 0.50 2/100 41\n")
    (proc / "meminfo").write_text("MemTotal: 4000 kB\nMemAvailable: 1000 kB\nShmem:\n")
    (cgroup / "cgroup.procs").write_text("\n".join(pids) + "\n")
    for pid in pids:
        (proc / pid).mkdir(exist_ok=True)
        (proc / pid / "statm").write_text("1000 256 0 0 0 0 0\n")
    return proc, cgroup


def test_sampler_parses_proc_text(tmp_path):
    ticks = sampler.cpu_ticks(STAT_1)
    assert ticks["cpu"] == (1000, 800, 0) and ticks["cpu1"] == (500, 400, 0)
    assert sampler.busy(sampler.cpu_ticks(STAT_2)["cpu"], ticks["cpu"]) == (75.0, 25.0)
    assert sampler.busy(ticks["cpu"], ticks["cpu"]) == (0.0, 0.0)
    assert sampler.memory_used_pct("MemTotal: 4000 kB\nMemAvailable: 1000 kB\n") == 75.0
    proc, cgroup = proc_tree(tmp_path, STAT_1, pids=("41", "42"))
    (proc / "42" / "statm").unlink()
    first, ticks = sampler.sample(proc, cgroup, {})
    assert "cpu_pct" not in first and first["load1"] == 1.5 and first["mem_used_pct"] == 75.0
    assert first["rss_mb"] == round(256 * os.sysconf("SC_PAGE_SIZE") / 1024**2, 1)
    (proc / "stat").write_text(STAT_2)
    second, _ = sampler.sample(proc, cgroup, ticks)
    assert (second["cpu_pct"], second["steal_pct"], second["core_max_pct"]) == (
        75.0, 25.0, 100.0)
    (cgroup / "cgroup.procs").write_text("")
    assert sampler.sample(proc, cgroup, ticks)[0] is None
    assert sampler.round_rss_mb(proc, tmp_path / "gone") is None


def test_sampler_runs_until_the_round_unit_is_empty(tmp_path):
    proc, cgroup = proc_tree(tmp_path, STAT_1)
    output = tmp_path / "host-samples.jsonl"
    texts = iter([STAT_2, STAT_2])

    def sleep(_seconds):
        text = next(texts, None)
        if text is None:
            (cgroup / "cgroup.procs").write_text("")
        else:
            (proc / "stat").write_text(text)

    assert sampler.main(["--output", str(output), "--proc", str(proc), "--cgroup",
                         str(cgroup)], sleep=sleep) == 0
    lines = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(lines) == 3 and lines[1]["cpu_pct"] == 75.0 and lines[2]["cpu_pct"] == 0.0


def test_sampler_summary_percentiles(tmp_path):
    path = tmp_path / "host-samples.jsonl"
    rows = [{"cpu_pct": float(v), "core_max_pct": v + 1.0, "steal_pct": v / 10,
             "load1": v / 50, "mem_used_pct": 40.0 + v / 10, "rss_mb": 100.0 + v}
            for v in range(1, 101)]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n{}\n")
    summary = sampler.summarize(path)
    assert summary == {"cpu_p50_pct": 50.0, "cpu_p95_pct": 95.0, "cpu_max_pct": 100.0,
                       "cpu_peak_pct": 100.0, "cpu_core_max_pct": 101.0, "steal_p95_pct": 9.5,
                       "load1_p95": 1.9, "mem_used_max_pct": 50.0, "rss_peak_mb": 200.0}
    assert set(sampler.summarize(tmp_path / "missing.jsonl").values()) == {None}
    timed = tmp_path / "timed.jsonl"
    timed.write_text("\n".join(json.dumps({"time": t, "cpu_pct": c}) for t, c in (
        (10.0, 1.0), (20.0, 2.0), (30.0, 80.0), (40.0, 90.0))) + "\n")
    assert sampler.summarize(timed, since=30.0)["cpu_p50_pct"] == 80.0  # m6: no idle lead-in
    assert sampler.summarize(timed)["cpu_p50_pct"] == 2.0
    assert sampler.percentile([], 0.5) is None and sampler.percentile([3.0], 0.95) == 3.0


# --- harvest ---------------------------------------------------------------------------------

AT_HH = datetime.fromisoformat("2027-01-01T22:00:00+00:00").timestamp()
RUNNING = {"service": {"ActiveState": "active", "SubState": "running"},
           "state": {"status": "running", "jobs": [{"started_at": "2027-01-01T22:00:01.500Z"}]}}


def ended(code=0, status="completed"):
    return {"service": {"ActiveState": "active", "SubState": "exited",
                        "ExecMainStatus": str(code)}, "state": {"status": status, "jobs": []}}


class HarvestRemote(FakeRemote):
    """Pulls copy a real round directory produced by the 1a round runner."""

    def __init__(self, clock, source):
        super().__init__(clock)
        self.source, self.pull_failures, self.pulls, self.stops = source, 0, [], []

    async def pull(self, droplet, remote, local):
        """Applies the real `TERMINAL_CHECK` to the source, with the unit state `inspect` shows."""
        self.pulls.append(remote)
        if self.pull_failures:
            self.pull_failures -= 1
            raise fanout.RemoteError("pull failed (exit 1)")
        service = (self.inspected.get(droplet.name) or {}).get("service") or {
            "ActiveState": "inactive", "SubState": "dead"}  # systemd's answer for no unit
        unit = "\n".join(f"{key}={value}" for key, value in service.items())
        check = await asyncio.to_thread(
            subprocess.run, [sys.executable, "-c", fanout.TERMINAL_CHECK, str(self.source)],
            env=stub_systemctl(self.source.parent, unit), check=False)
        if check.returncode:
            raise fanout.RemoteError(f"pull failed (exit {check.returncode})")
        shutil.rmtree(local, ignore_errors=True)
        shutil.copytree(self.source, local)

    async def journal(self, droplet, local):
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"journal")

    async def stop(self, droplet):
        self.stops.append(droplet.name)


async def harvest_setup(tmp_path, *, samples=True, fake=None):
    """A launched hour (net00 in tor1) and a worker round directory with real data."""
    worker_manifest = make_manifest(tmp_path / "worker")
    await run_round(worker_manifest, 0, rows=2)
    source = worker_manifest.parent / "regions" / "tor1" / "rounds" / "00"
    (source / "worker-start.json").write_text('{"ntp_synchronized": true}')
    if samples:
        (source / "host-samples.jsonl").write_text("\n".join(json.dumps(
            {"cpu_pct": v, "core_max_pct": v + 5, "steal_pct": 1.0, "load1": 1.5,
             "mem_used_pct": 30.0, "rss_mb": 200.0}) for v in (40.0, 60.0, 90.0)) + "\n"
            + json.dumps({"time": AT_HH - 30, "cpu_pct": 1.0}) + "\n")  # idle lead-in (m6)
    spec = make_spec(NETWORKS[:1], ("tor1",))
    root = make_root(tmp_path, spec)
    (root / spec.manifests["net00"]).write_bytes(worker_manifest.read_bytes())
    fake = fake or FakeDigitalOcean()
    record, *_ = await launched(tmp_path, fake, spec)
    assert record.droplets[-1].status == "armed"
    clock = FakeClock(AT_HH + 60)
    return spec, root, fake, record, HarvestRemote(clock, source), clock


async def harvested(spec, record, root, fake, remote, clock):
    async with running(fake) as cloud:
        await harvest.harvest_hour(spec, record, root, cloud=cloud,
                                   remote_factory=lambda hour: remote, clock=clock)
    return record


async def test_a_finished_round_is_pulled_verified_recorded_and_removed(tmp_path):
    """The happy path; verify_round ignores the sampler and start files; idempotent (14)."""
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    remote.inspected[record.droplets[0].name] = RUNNING
    await harvested(spec, record, root, fake, remote, clock)
    worker = record.droplets[0]
    assert worker.status == "running" and worker.start_lateness_seconds == 1.5
    assert record.state == "running" and fake.deleted == []
    remote.inspected[worker.name] = ended()
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "destroyed" and fake.deleted == [("droplet", worker.droplet_id)]
    assert (worker.exit_code, worker.error_class) == (0, None)
    assert (worker.requests, worker.successes, worker.errors) == (9, 6, 3)
    assert (worker.bytes_up, worker.bytes_down) == (600, 1800)
    assert (worker.cpu_p50_pct, worker.cpu_max_pct, worker.cpu_core_max_pct) == (60.0, 90.0, 95.0)
    assert worker.rss_peak_mb == 200.0 and worker.mem_used_max_pct == 30.0
    assert record.cells == {"net00/tor1": "complete"} and record.state == "done"
    traffic = record.traffic["net00"]
    assert (traffic["countries_done"], traffic["countries_total"], traffic["bytes_down"]) == (
        3, 3, 1800)
    assert traffic["worker_hours"] > 0
    local = root / "networks/net00/residential/m-net00/regions/tor1/rounds/00"
    assert (local / "host-samples.jsonl").exists() and (local / "worker-start.json").exists()
    assert (root / "logs/00/net00-tor1.journal.txt").read_bytes() == b"journal"
    assert "203.0.113" not in hour_path(root, 0).read_text()
    before = len(fake.requests)
    again = launch.load_hour(root, 0)
    await harvested(spec, again, root, fake, remote, clock)
    assert len(fake.requests) == before and again.to_dict() == record.to_dict()


async def test_a_long_round_is_never_stopped_only_alerted(tmp_path):
    """5: info at 3 h, high at 12 h; no stop and no removal."""
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    remote.inspected[record.droplets[0].name] = RUNNING
    for hours in (1, 3.1, 12.1):
        clock.t = AT_HH + hours * HOUR
        await harvested(spec, record, root, fake, remote, clock)
    assert [(a["level"], a["kind"]) for a in record.alerts] == [
        ("info", "round_long_3h"), ("high", "round_long_12h")]
    assert remote.stops == [] and fake.deleted == [] and record.droplets[0].status == "running"


@pytest.mark.parametrize("samples", [True, False])
async def test_a_failed_round_unit_is_collected_partially_and_removed(tmp_path, samples):
    """6: exit not 0 or 130 -> pull, journal, failed_ours, removal; no samples is fine."""
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path, samples=samples)
    remote.inspected[record.droplets[0].name] = ended(1, "failed")
    await harvested(spec, record, root, fake, remote, clock)
    worker = record.droplets[0]
    assert (worker.status, worker.exit_code, worker.error_class) == (
        "destroyed", 1, "round_failed")
    assert record.cells == {"net00/tor1": "failed_ours"} and len(remote.pulls) == 1
    assert (worker.cpu_max_pct is None) is (not samples)


async def test_a_unit_that_failed_before_its_checkpoint_is_still_removed(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    (remote.source / "round.json").unlink()
    remote.inspected[record.droplets[0].name] = {"service": {
        "ActiveState": "failed", "SubState": "failed", "ExecMainStatus": "1"}}
    await harvested(spec, record, root, fake, remote, clock)
    assert record.droplets[0].status == "destroyed" and record.droplets[0].requests == 0
    assert record.cells == {"net00/tor1": "failed_ours"}


async def test_an_operator_stop_is_partial(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    remote.inspected[record.droplets[0].name] = ended(130, "stopped")
    await harvested(spec, record, root, fake, remote, clock)
    assert record.cells == {"net00/tor1": "partial"} and record.droplets[0].error_class is None


async def test_a_verify_failure_is_repulled_and_kept_with_an_alert(tmp_path):
    """7: re-pull every 5 min; high alert after 30 min; never removed meanwhile."""
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    worker = record.droplets[0]
    remote.inspected[worker.name] = ended()
    chunk = next((remote.source / "ca").glob("observations-*.jsonl.gz"))
    good = chunk.read_bytes()
    chunk.write_bytes(good + b"\0")
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "collect_failed" and len(remote.pulls) == 1
    clock.t += 60
    await harvested(spec, record, root, fake, remote, clock)
    assert len(remote.pulls) == 1  # not yet 5 minutes
    for _ in range(6):
        clock.t += harvest.PULL_RETRY_SECONDS
        await harvested(spec, record, root, fake, remote, clock)
    assert len(remote.pulls) == 7 and fake.deleted == [] and record.state == "harvesting"
    assert [(a["level"], a["kind"]) for a in record.alerts] == [("high", "collect_failed")]
    chunk.write_bytes(good)
    clock.t += harvest.PULL_RETRY_SECONDS
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "destroyed" and record.cells == {"net00/tor1": "complete"}


async def test_a_pull_error_keeps_the_worker(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    remote.inspected[record.droplets[0].name] = ended()
    remote.pull_failures = 1
    await harvested(spec, record, root, fake, remote, clock)
    assert record.droplets[0].status == "collect_failed" and fake.deleted == []
    clock.t += harvest.PULL_RETRY_SECONDS
    await harvested(spec, record, root, fake, remote, clock)
    assert record.droplets[0].status == "destroyed"


async def test_an_unreachable_worker_is_kept_and_alerted_after_30_minutes(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    for minutes in (0, 20, 31):
        clock.t = AT_HH + 60 + minutes * 60
        await harvested(spec, record, root, fake, remote, clock)
        assert [a["kind"] for a in record.alerts] == (
            ["worker_unreachable"] if minutes > 30 else [])
    assert record.droplets[0].status == "armed" and fake.deleted == []
    memo = json.loads(harvest.harvest_state_path(root, 0).read_text())
    assert memo[record.droplets[0].droplet_id]["unreachable_since"] == AT_HH + 60
    remote.inspected[record.droplets[0].name] = RUNNING
    await harvested(spec, record, root, fake, remote, clock)
    assert "unreachable_since" not in json.loads(
        harvest.harvest_state_path(root, 0).read_text())[record.droplets[0].droplet_id]


async def test_a_guard_refusal_in_harvest_is_final_and_fails_the_hour(tmp_path):
    """8: destroy_refused, a high alert, no other removal path."""
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    worker = record.droplets[0]
    remote.inspected[worker.name] = ended()
    fake.droplets[worker.droplet_id]["tags"] = ["prod"]
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "destroy_refused" and fake.deleted == []
    assert record.state == "failed" and record.finished_at
    assert ("high", "destroy_refused") in [(a["level"], a["kind"]) for a in record.alerts]


async def test_a_failed_removal_is_retried_on_the_next_pass(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    worker = record.droplets[0]
    remote.inspected[worker.name] = ended()
    fake.faults.append(Fault("DELETE", f"/v2/droplets/{worker.droplet_id}", 422))
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "destroy_failed" and record.state == "harvesting"
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "destroyed" and record.state == "done"
    assert len(remote.pulls) == 1


async def test_an_unexpected_error_stays_with_its_worker(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    remote.inspected[record.droplets[0].name] = {"service": {"ActiveState": "inactive",
                                                             "ExecMainStatus": "x"}}
    await harvested(spec, record, root, fake, remote, clock)
    assert record.droplets[0].error_class == "ValueError" and fake.deleted == []
    assert [a["kind"] for a in record.alerts] == ["harvest_error"]


async def test_harvest_leaves_planned_and_finished_hours_alone(tmp_path):
    record = HourRecord(CID, 0, SCHEDULED)
    spec = make_spec(synthetic=True)
    await harvest.harvest_hour(spec, record, tmp_path, cloud=None, remote_factory=None,
                               clock=FakeClock(AT_HH))
    assert record.state == "planned" and not hour_path(tmp_path, 0).exists()


async def test_synthetic_workers_finish_after_synthetic_minutes_without_calls(tmp_path):
    """15: synthetic harvest makes no cloud or SSH calls either."""
    spec = make_spec(synthetic=True)
    root = tmp_path / "campaign"
    for network in spec.networks:
        manifest = root / spec.manifests[network]
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"regions": {r: ["US"] for r in REGIONS},
                                        "settings": {"concurrency": 2000}}))
    record = HourRecord(CID, 0, SCHEDULED)
    clock = FakeClock(PRE_CREATE)
    await launch.launch_hour(spec, record, root, cloud=None, remote_factory=None, clock=clock)
    clock.t = AT_HH + 60
    await harvest.harvest_hour(spec, record, root, cloud=None, remote_factory=None, clock=clock)
    assert record.state == "running"
    clock.t = AT_HH + launch.SYNTHETIC_MINUTES * 60
    await harvest.harvest_hour(spec, record, root, cloud=None, remote_factory=None, clock=clock)
    assert {d.status for d in record.droplets} == {"destroyed"} and record.state == "done"
    assert set(record.cells.values()) == {"complete"}


@pytest.mark.parametrize("info,expected", [
    (RUNNING, (False, None)),
    ({"service": {"ActiveState": "activating"}}, (False, None)),
    (ended(0), (True, 0)),
    (ended(130, "stopped"), (True, 130)),
    ({"service": {"ActiveState": "inactive", "ExecMainStatus": "0"},
      "state": {"status": "running"}}, (True, 1)),
    ({}, (True, 1)),
])
def test_round_ended_reads_the_unit_and_the_checkpoint(info, expected):
    assert harvest.round_ended(info) == expected


async def test_a_missing_checkpoint_after_a_clean_exit_is_repulled_and_a_journal_error_is_info(
        tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    remote.inspected[record.droplets[0].name] = ended()
    checkpoint = (remote.source / "round.json").read_bytes()
    (remote.source / "round.json").unlink()
    await harvested(spec, record, root, fake, remote, clock)
    assert record.droplets[0].status == "collect_failed" and fake.deleted == []

    async def broken(droplet, local):
        raise fanout.RemoteError("journal failed (exit 1)")

    remote.journal = broken
    (remote.source / "round.json").write_bytes(checkpoint)
    clock.t += harvest.PULL_RETRY_SECONDS
    await harvested(spec, record, root, fake, remote, clock)
    assert record.droplets[0].status == "destroyed"
    assert [(a["level"], a["kind"]) for a in record.alerts] == [("info", "journal_failed")]


async def test_b2_a_killed_round_is_collected_partially_and_removed(tmp_path):
    """B2: SIGKILL left the checkpoint `running`; the ended unit still lets it be pulled."""
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    checkpoint = json.loads((remote.source / "round.json").read_text())
    (remote.source / "round.json").write_text(json.dumps(checkpoint | {"status": "running"}))
    worker = record.droplets[0]
    remote.inspected[worker.name] = {
        "service": {"ActiveState": "failed", "SubState": "failed", "ExecMainStatus": "9"},
        "state": {"status": "running", "jobs": [{"started_at": "2027-01-01T22:00:01.500Z"}]}}
    await harvested(spec, record, root, fake, remote, clock)
    assert (worker.status, worker.exit_code, worker.error_class) == (
        "destroyed", 9, "round_failed")
    assert record.cells == {"net00/tor1": "failed_ours"} and record.state == "done"
    local = root / "networks/net00/residential/m-net00/regions/tor1/rounds/00"
    assert json.loads((local / "round.json").read_text())["status"] == "running"
    assert fake.deleted == [("droplet", worker.droplet_id)]


async def test_b2_a_running_round_is_never_pulled_even_if_inspect_misreads_it(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    checkpoint = json.loads((remote.source / "round.json").read_text())
    (remote.source / "round.json").write_text(json.dumps(checkpoint | {"status": "running"}))
    worker = record.droplets[0]
    remote.inspected[worker.name] = {"service": {"ActiveState": "active", "SubState": "running",
                                                 "ExecMainStatus": "0"}}
    worker.status, worker.exit_code = "finished", 0
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "collect_failed" and fake.deleted == []


async def test_b1_after_a_readiness_api_error_the_hour_reaches_done(tmp_path):
    """B1 (a): every created droplet was retired at launch, so one pass finishes the hour."""
    fake = ListFailsAfterCreate()
    spec = make_spec(NETWORKS[:2], REGIONS[:1])
    record, root, remote, clock = await launched(tmp_path, fake, spec)
    await harvested(spec, record, root, fake, remote, clock)
    assert record.state == "done" and {d.status for d in record.droplets} == {
        "destroyed", "create_failed"}


async def test_b1_a_failed_region_does_not_keep_the_hour_open(tmp_path):
    """B1 (c): a region whose create raised ends create_failed; harvest finishes the hour."""
    fake = FakeDigitalOcean()
    spec = make_spec(NETWORKS[:1], ("fra1",))
    original = launch._Launch._create_call

    async def broken(self, workers):
        raise RuntimeError("boom")

    launch._Launch._create_call = broken
    try:
        record, root, remote, clock = await launched(tmp_path, fake, spec)
    finally:
        launch._Launch._create_call = original
    await harvested(spec, record, root, fake, remote, clock)
    assert [d.status for d in record.droplets] == ["create_failed"] * (
        1 + launch.REPLACEMENTS_PER_CELL) and record.state == "done"


async def test_m7_a_collect_failed_worker_without_a_memo_is_repulled(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    worker = record.droplets[0]
    worker.status, worker.exit_code, worker.finished_at = "collect_failed", 0, SCHEDULED
    assert not harvest.harvest_state_path(root, 0).exists()
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "destroyed" and len(remote.pulls) == 1
    assert [a["kind"] for a in record.alerts] == []


async def test_1e_r1_an_unconfirmed_start_that_never_ran_ends_failed_ours(tmp_path):
    """A worker armed as start_unconfirmed whose round never started: removed, never
    start_failed; armed_at stays, so 1e never reads it as a round that provably did not run."""
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    shutil.rmtree(remote.source)
    remote.source.mkdir()
    worker = record.droplets[0]
    worker.error_class = "start_unconfirmed"
    remote.inspected[worker.name] = {"server_time": 0}  # reachable, no fleet marker
    await harvested(spec, record, root, fake, remote, clock)
    assert (worker.status, worker.exit_code, worker.error_class) == (
        "destroyed", 1, "round_failed")
    assert worker.armed_at and record.cells == {"net00/tor1": "failed_ours"}
    assert record.state == "done"
