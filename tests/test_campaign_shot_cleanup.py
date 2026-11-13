"""SYNC-1: a `campaign shot` stopped by a signal, a kill or an unexpected error still removes
every worker, or names the `--remove` that does (fake DigitalOcean, fake clock, fake remote;
nothing leaves the machine)."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from campaign_fakes import FakeDigitalOcean, Fault, running
from campaign_hour_cases import FakeClock
from campaign_probe_cases import NOW, Recording
from campaign_shot_cases import ShotRemote, cell_of, fired, shot_world

from atlas.campaign import shot as shooting
from atlas.campaign.probe import ProbeError


async def test_a_signal_still_removes_every_worker(tmp_path):
    class Cancelled(ShotRemote):
        async def run(self, droplet, command, what, *, stdin=None, seconds=120):
            if what == "shot webshare":
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            return await super().run(droplet, command, what, stdin=stdin, seconds=seconds)

    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
    inputs, _ = shot_world(tmp_path, regions=("lon1",))
    result = await fired(fake, Cancelled(clock), inputs, clock)
    assert result["error"] == "cancelled by a signal" and len(fake.deleted) == 2
    assert result["workers_left"] == 0


async def test_remove_finishes_what_a_stopped_shot_left(tmp_path, monkeypatch):
    async def kept(*args, **kwargs):
        return None

    monkeypatch.setattr(shooting, "release", kept)
    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
    inputs, _ = shot_world(tmp_path, regions=("lon1",))
    result = await fired(fake, ShotRemote(clock), inputs, clock)
    folder = inputs.output / result["shot_id"]
    assert result["workers_left"] == 2 and result["recover"].endswith(str(folder))
    monkeypatch.undo()
    async with running(fake) as cloud:
        assert await shooting.recover(cloud, folder) == {
            "shot_id": result["shot_id"],
            "workers_left": 0,
        }
        record = json.loads((folder / "shot-result.json").read_text())
        assert "recover" not in record and len(fake.deleted) == 2
        assert {w["status"] for w in record["workers"]} == {"destroyed"}
        assert (await shooting.recover(cloud, folder))["workers_left"] == 0
    with pytest.raises(ProbeError, match="no shot record"):
        async with running(fake) as cloud:
            await shooting.recover(cloud, tmp_path)


async def test_s1_a_second_droplet_with_a_workers_name_is_recorded_and_removed(tmp_path):
    class Doubled(Recording):
        async def _create(self, request):  # DigitalOcean made one droplet too many
            answer = await super()._create(request)
            body = json.loads(answer.body)
            [extra] = [
                self.droplets[
                    self.add(body["droplets"][0]["name"], self.creates[-1]["tags"], project=None)
                ]
            ]
            body["droplets"].append(extra)
            return web.json_response(body, status=202)

    fake, clock = Doubled(), FakeClock(NOW.timestamp())
    inputs, _ = shot_world(tmp_path, networks=("webshare",), regions=("lon1",), size_by_network={})
    result = await fired(fake, ShotRemote(clock), inputs, clock)
    [worker], [extra] = result["workers"], result["unrecorded"]
    assert extra["name"] == worker["name"] and extra["droplet_id"] != worker["droplet_id"]
    assert extra["status"] == "destroyed" and len(fake.deleted) == 2
    assert not [d for d in fake.droplets.values() if d["name"].startswith("pic-")]
    assert result["workers_left"] == 0


async def test_s2_a_killed_shot_still_names_its_recovery(tmp_path, monkeypatch):
    async def killed(self):  # SIGKILL after the stop timeout, or an OOM: no removal runs
        raise RuntimeError("killed")

    monkeypatch.setattr(shooting.Shot, "cleanup", killed)
    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
    inputs, _ = shot_world(tmp_path, regions=("lon1",))
    with pytest.raises(RuntimeError, match="killed"):
        await fired(fake, ShotRemote(clock), inputs, clock)
    [folder] = inputs.output.iterdir()
    record = json.loads((folder / "shot-result.json").read_text())
    assert record["recover"] == f"campaign shot --remove {folder}"
    assert all(w["droplet_id"] for w in record["workers"])
    monkeypatch.undo()
    async with running(fake) as cloud:
        assert (await shooting.recover(cloud, folder))["workers_left"] == 0
    assert "recover" not in json.loads((folder / "shot-result.json").read_text())


async def test_m4_a_signal_during_the_creates_still_removes_every_worker(tmp_path):
    class Stopped(Recording):
        async def _create(self, request):  # created, then SIGTERM before the answer is read
            answer = await super()._create(request)
            outer.cancel()
            return answer

    fake, clock = Stopped(), FakeClock(NOW.timestamp())
    inputs, _ = shot_world(tmp_path, networks=("webshare",), size_by_network={})
    outer = asyncio.current_task()
    result = await fired(fake, ShotRemote(clock), inputs, clock)
    assert result["error"] == "cancelled by a signal"
    assert not [d for d in fake.droplets.values() if d["name"].startswith("pic-")]
    assert {c["outcome"] for c in result["cells"].values()} == {"cancelled"}
    # A create stopped around its POST may still land: counted until a listing settles it.
    uncertain = [w for w in result["workers"] if w["error_class"] == "create_uncertain"]
    assert result["workers_left"] == len(uncertain) and ("recover" in result) == bool(uncertain)
    async with running(fake) as cloud:
        folder = inputs.output / result["shot_id"]
        assert (await shooting.recover(cloud, folder))["workers_left"] == 0


async def test_m4_a_signal_mid_step_removes_every_worker_and_marks_them_cancelled(tmp_path):
    class Stopped(ShotRemote):
        async def run(self, droplet, command, what, *, stdin=None, seconds=120):
            if what.startswith("shot "):
                outer.cancel()
                await asyncio.sleep(0)
            return await super().run(droplet, command, what, stdin=stdin, seconds=seconds)

    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
    inputs, _ = shot_world(tmp_path, regions=("lon1",))
    outer = asyncio.current_task()
    result = await fired(fake, Stopped(clock), inputs, clock)
    assert result["error"] == "cancelled by a signal" and len(fake.deleted) == 2
    assert {c["outcome"] for c in result["cells"].values()} == {"cancelled"}
    assert result["workers_left"] == 0 and "recover" not in result


async def test_m1_an_unexpected_error_stays_with_its_worker(tmp_path):
    class Broken(ShotRemote):
        async def run(self, droplet, command, what, *, stdin=None, seconds=120):
            if command[0] == "python3" and cell_of(droplet) == ("soax", "lon1"):
                raise OSError("too many open files")
            return await super().run(droplet, command, what, stdin=stdin, seconds=seconds)

    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
    inputs, _ = shot_world(tmp_path, regions=("lon1",))
    result = await fired(fake, Broken(clock), inputs, clock)
    assert {k: c["outcome"] for k, c in result["cells"].items()} == {
        "soax/lon1": "setup_failed",
        "webshare/lon1": "measured",
    }
    assert "error" not in result and result["workers_left"] == 0


async def test_m4_remove_settles_a_create_that_never_landed(tmp_path):
    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
    fake.faults.append(Fault("POST", "/v2/droplets", 500, times=10))  # answer lost, none made
    inputs, _ = shot_world(tmp_path, networks=("soax",), regions=("lon1",))
    result = await fired(fake, ShotRemote(clock), inputs, clock)
    [worker] = result["workers"]
    assert worker["error_class"] == "create_uncertain" and worker["droplet_id"] is None
    assert result["workers_left"] == 1 and "recover" in result
    assert result["cells"]["soax/lon1"]["outcome"] == "create_failed"
    async with running(fake) as cloud:
        folder = inputs.output / result["shot_id"]
        assert (await shooting.recover(cloud, folder))["workers_left"] == 0
    assert json.loads((folder / "shot-result.json").read_text())["workers"][0]["status"] == "absent"


async def test_s1r_remove_releases_every_droplet_with_the_name_when_no_id_is_recorded(
    tmp_path, monkeypatch
):
    async def killed(self):  # killed during the create POST: no id was ever recorded
        raise RuntimeError("killed")

    monkeypatch.setattr(shooting.Shot, "cleanup", killed)
    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
    inputs, _ = shot_world(tmp_path, networks=("webshare",), regions=("lon1",), size_by_network={})
    with pytest.raises(RuntimeError, match="killed"):
        await fired(fake, ShotRemote(clock), inputs, clock)
    [folder] = inputs.output.iterdir()
    path = folder / "shot-result.json"
    record = json.loads(path.read_text())
    [worker] = record["workers"]
    worker.update(
        droplet_id=None, status="requested", error_class="create_uncertain", assigned_at=None
    )
    path.write_text(json.dumps(record))
    first = fake.droplets[next(i for i, d in fake.droplets.items() if d["name"] == worker["name"])]
    fake.add(first["name"], first["tags"], project=None)  # DigitalOcean made one too many
    monkeypatch.undo()
    async with running(fake) as cloud:
        assert (await shooting.recover(cloud, folder))["workers_left"] == 0
    assert not [d for d in fake.droplets.values() if d["name"].startswith("pic-")]
    record = json.loads(path.read_text())
    assert [w["status"] for w in record["workers"] + record["unrecorded"]] == [
        "destroyed",
        "destroyed",
    ] and "recover" not in record
