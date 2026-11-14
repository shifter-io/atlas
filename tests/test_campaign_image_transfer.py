"""IMAGE-AUTO-1: the snapshot copies to the other regions run side by side (ops_api.transfer):
every copy starts and is recorded before any is waited to its end, at most TRANSFER_PARALLEL at
once, and one failed copy fails the build only after the others have ended. In-process fake
cloud; no request leaves the test."""

from __future__ import annotations

import asyncio

import pytest
from campaign_fakes import FakeDigitalOcean, running

from atlas.campaign import ops_api
from atlas.regions import REGION_IDS

REGIONS = ["lon1", *(r for r in REGION_IDS if r != "lon1")]  # the snapshot starts in lon1
NAME = "atlas-camp-worker-0.9.5-0123456789ab-20270101"


class Cloud:
    """The snapshot is in lon1; each copy needs two polls; `errored` regions fail."""

    def __init__(self, errored=()):
        self.regions, self.errored = ["lon1"], set(errored)
        self.events, self.polls, self.running, self.most = [], {}, 0, 0

    async def image(self, image_id):
        return {"id": image_id, "name": NAME, "regions": list(self.regions)}

    async def transfer_image(self, image_id, region):
        self.events.append(("start", region))
        self.running += 1
        self.most = max(self.most, self.running)
        return {"id": f"a-{region}", "type": "transfer"}

    async def action(self, action_id):
        region = action_id.removeprefix("a-")
        self.polls[region] = self.polls.get(region, 0) + 1
        if self.polls[region] < 2:
            return {"status": "in-progress"}
        self.running -= 1
        self.events.append(("end", region))
        if region in self.errored:
            return {"status": "errored"}
        self.regions.append(region)
        return {"status": "completed"}


async def yield_only(seconds):
    await asyncio.sleep(0)


async def test_every_copy_starts_and_is_recorded_before_any_ends():
    cloud, recorded = Cloud(), {}

    def started(region, action_id):
        recorded[region] = action_id
        assert ("end", region) not in cloud.events

    assert await ops_api.transfer(cloud, "12", REGIONS, yield_only, started) == sorted(REGIONS)
    kinds = [kind for kind, _ in cloud.events]
    assert kinds[:7] == ["start"] * 7 and kinds[7:] == ["end"] * 7
    assert recorded == {r: f"a-{r}" for r in REGIONS[1:]} and cloud.most == 7


async def test_the_copies_are_bounded(monkeypatch):
    monkeypatch.setattr(ops_api, "TRANSFER_PARALLEL", 2)
    cloud = Cloud()
    assert await ops_api.transfer(cloud, "12", REGIONS, yield_only) == sorted(REGIONS)
    assert cloud.most == 2 and len(cloud.events) == 14


async def test_a_failed_copy_fails_only_after_every_other_copy_ended():
    cloud = Cloud(errored={"fra1"})
    with pytest.raises(ops_api.OpsError, match="a-fra1"):
        await ops_api.transfer(cloud, "12", REGIONS, yield_only)
    assert sorted(r for kind, r in cloud.events if kind == "end") == sorted(REGIONS[1:])
    assert cloud.running == 0


async def test_the_real_client_runs_the_copies_side_by_side():
    fake = FakeDigitalOcean()
    fake.images["12"] = {"id": 12, "name": NAME, "regions": ["lon1"], "droplet": ""}
    recorded = {}
    async with running(fake) as cloud:
        found = await ops_api.transfer(
            cloud, "12", REGIONS, yield_only, lambda r, a: recorded.setdefault(r, a)
        )
    assert found == sorted(REGIONS) and sorted(recorded) == sorted(REGIONS[1:])
    posted = [b["region"] for _, p, b in fake.bodies if p.startswith("/v2/images")]
    assert sorted(posted) == sorted(REGIONS[1:])
