"""COV-2: `campaign shot` corners: a signal during the removal, a droplet seen twice and a
removal the guard refuses (fake DigitalOcean, fake clock, fake remote; nothing leaves the
machine)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from campaign_fakes import FakeDigitalOcean, running
from campaign_hour_cases import FakeClock
from campaign_probe_cases import NOW
from campaign_shot_cases import ShotRemote, cell_of, fired, shot_world

from proxy_index.campaign import shot as shooting
from proxy_index.campaign.contracts import WORKER_TAG, Droplet


async def test_a_signal_during_the_removal_never_stops_it(tmp_path, monkeypatch):
    original = shooting.release

    async def interrupted(*args, **kwargs):  # SIGTERM while the workers are being removed
        outer.cancel()
        await asyncio.sleep(0)
        return await original(*args, **kwargs)

    monkeypatch.setattr(shooting, "release", interrupted)
    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
    inputs, _ = shot_world(tmp_path, regions=("lon1",))
    outer = asyncio.current_task()
    result = await fired(fake, ShotRemote(clock), inputs, clock)
    assert result["error"] == "cancelled by a signal"
    assert {c["outcome"] for c in result["cells"].values()} == {"measured"}
    assert len(fake.deleted) == 2 and {w["status"] for w in result["workers"]} == {"destroyed"}
    assert result["workers_left"] == 0 and "recover" not in result


def droplet(droplet_id, name):
    return Droplet(droplet_id, name, "lon1", "c-2", "new", NOW.astimezone(UTC), (WORKER_TAG,))


def test_a_droplet_already_held_is_never_recorded_twice(tmp_path):
    clock = FakeClock(NOW.timestamp())
    inputs, _ = shot_world(tmp_path, regions=("lon1",))
    shot = shooting.Shot(None, ShotRemote(clock).factory, inputs, clock)
    first, second = shot.workers
    held = droplet("700001", first.name)
    shot.take([first], [held], "create_failed")
    assert first.droplet_id == "700001" and second.droplet_id is None
    # The cleanup's last look for the uncertain one answers with the held droplet too.
    shot.take([second], [held, droplet("700002", second.name)], None)
    assert (first.droplet_id, second.droplet_id) == ("700001", "700002")
    assert shot.unrecorded == [] and first.error_class is None and second.error_class is None


async def test_a_refused_removal_is_reported_and_left_to_the_owner(tmp_path):
    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())

    class Untagged(ShotRemote):  # someone takes one worker's worker tag off during its step
        async def run(self, droplet, command, what, *, stdin=None, seconds=120):
            if command[0] != "python3" and cell_of(droplet) == ("soax", "lon1"):
                fake.droplets[droplet.id]["tags"].remove(WORKER_TAG)
            return await super().run(droplet, command, what, stdin=stdin, seconds=seconds)

    inputs, _ = shot_world(tmp_path, regions=("lon1",))
    result = await fired(fake, Untagged(clock), inputs, clock)
    moved = next(w for w in result["workers"] if w["network"] == "soax")
    assert result["destroy_refused"] == [moved["name"]] and moved["status"] == "destroy_refused"
    assert moved["droplet_id"] in fake.droplets and len(fake.deleted) == 1
    folder = inputs.output / result["shot_id"]
    assert result["workers_left"] == 1 and result["recover"].endswith(str(folder))
    async with running(fake) as cloud:  # --remove never retries a refusal
        assert await shooting.recover(cloud, folder) == {
            "shot_id": result["shot_id"],
            "workers_left": 1,
        }
    record = json.loads((folder / "shot-result.json").read_text())
    assert record["recover"] == result["recover"] and record["workers_left"] == 1
    assert moved["droplet_id"] in fake.droplets and len(fake.deleted) == 1
    assert datetime.fromisoformat(record["recovered_at"]) <= datetime.now(UTC)
