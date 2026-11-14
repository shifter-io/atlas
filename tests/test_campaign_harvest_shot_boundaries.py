"""ERR-4: the broad handlers of harvest, pulse_fleet and shot are deliberate boundaries. An
unexpected error (not the remote's or the API's own type) is recorded by its class and never
skips a sibling, a retry or a removal (fake cloud, fake SSH, fake clock; nothing leaves the
machine)."""

from __future__ import annotations

import json

from campaign_fakes import FakeDigitalOcean, Fault
from campaign_hour_cases import FakeClock, ended, harvest_setup, harvested
from campaign_probe_cases import NOW
from campaign_shot_cases import ShotRemote, fired, shot_world
from pulse_fleet_cases import Issuer, Remote, make_root, reconcile, records, world

from atlas.campaign import harvest
from atlas.campaign import shot as shooting
from atlas.campaign.do_api import DigitalOcean


def memo_of(root, worker) -> dict:
    return json.loads(harvest.harvest_state_path(root, 0).read_text())[worker.droplet_id]


async def test_an_unexpected_inspect_error_is_an_unreachable_worker_with_its_class(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    worker = record.droplets[0]

    async def broken(droplet):
        raise TypeError("unexpected")

    remote.inspect = broken
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "armed" and fake.deleted == [] and record.alerts == []
    assert memo_of(root, worker)["unreachable_error"] == "TypeError"
    del remote.inspect
    remote.inspected[worker.name] = ended()
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "destroyed" and "unreachable_error" not in memo_of(root, worker)


async def test_an_unexpected_pull_error_keeps_the_worker_and_is_repulled(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    worker = record.droplets[0]
    remote.inspected[worker.name] = ended()
    pull = remote.pull

    async def broken(droplet, where, local):
        raise KeyError("unexpected")

    remote.pull = broken
    await harvested(spec, record, root, fake, remote, clock)
    assert (worker.status, worker.error_class) == ("collect_failed", "collect_failed")
    assert fake.deleted == [] and memo_of(root, worker)["collect_error"] == "KeyError"
    remote.pull = pull
    clock.t += harvest.PULL_RETRY_SECONDS
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "destroyed" and fake.deleted == [("droplet", worker.droplet_id)]


async def test_an_unexpected_journal_error_never_blocks_the_removal(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    worker = record.droplets[0]
    remote.inspected[worker.name] = ended()

    async def broken(droplet, local):
        raise TypeError("unexpected")

    remote.journal = broken
    await harvested(spec, record, root, fake, remote, clock)
    assert worker.status == "destroyed" and record.cells == {"net00/tor1": "complete"}
    assert [(a["level"], a["kind"]) for a in record.alerts] == [("info", "journal_failed")]
    assert memo_of(root, worker)["journal_error"] == "TypeError"


async def test_an_unexpected_arming_error_fails_only_its_node(tmp_path):
    class Broken(Issuer):
        def __call__(self, database_file, *, provider, **rest):
            if provider == "geonode":
                raise KeyError("unexpected")
            return super().__call__(database_file, provider=provider, **rest)

    root = make_root(tmp_path, networks=("shifter", "geonode"))
    report = await reconcile(world(), root, Remote(), Broken(), None)
    assert report["failed"] == [{"name": "atlas-pulse-lon1-geonode", "status": "start_failed"}]
    nodes = records(root)
    assert nodes["atlas-pulse-lon1-shifter"].status == "armed"
    failed = nodes["atlas-pulse-lon1-geonode"]
    assert failed.error_class == "KeyError" and failed.error_detail["error"] == "KeyError"


async def test_an_unexpected_shot_error_is_reported_scrubbed_and_every_worker_removed(
    tmp_path, monkeypatch
):
    async def broken(self):
        raise LookupError("no answer from 203.0.113.9")

    monkeypatch.setattr(shooting.Shot, "active", broken)
    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
    inputs, _ = shot_world(tmp_path, regions=("lon1",))
    result = await fired(fake, ShotRemote(clock), inputs, clock)
    assert result["error"] == "LookupError: no answer from [removed]"
    assert len(fake.deleted) == 2 and result["workers_left"] == 0


async def test_a_failed_adoption_never_skips_the_removal_of_the_other_workers(
    tmp_path, monkeypatch
):
    async def broken(self, names, tag):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(DigitalOcean, "adopt_uncertain", broken)
    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
    fake.faults.append(Fault("POST", "/v2/droplets", 500))  # one create's answer is lost
    inputs, _ = shot_world(tmp_path, regions=("lon1",))
    result = await fired(fake, ShotRemote(clock), inputs, clock)
    assert result["adopt_error"] == "RuntimeError" and len(fake.deleted) == 1
    [uncertain] = [w for w in result["workers"] if w["error_class"] == "create_uncertain"]
    assert uncertain["droplet_id"] is None
    assert result["workers_left"] == 1 and "recover" in result
