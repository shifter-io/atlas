"""SYNC-1: `campaign shot`, one fixed step per network and region started at one time.

Runs against the in-process fake DigitalOcean, a fake clock and a fake remote that answers
each step with a run summary; no request or SSH connection leaves the machine.
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from dataclasses import replace
from importlib import resources

import pytest
from aiohttp import web
from campaign_fakes import CAMPAIGN_PROJECT, FakeDigitalOcean, Fault, running
from campaign_hour_cases import FakeClock
from test_campaign_probe import (
    IP,
    NOW,
    PROBED,
    FakeProbeRemote,
    Recording,
    classified,
    summary,
    world,
)

from proxy_index import cli as main_cli
from proxy_index.campaign import probe as probing
from proxy_index.campaign import shot as shooting
from proxy_index.campaign.contracts import WORKER_TAG, audit_path
from proxy_index.campaign.fanout import RemoteError
from proxy_index.campaign.probe import ProbeError
from proxy_index.campaign.shot import ShotInputs
from proxy_index.providers.grammar import Grammar

GRAMMARS = resources.files("proxy_index.providers").joinpath("grammars")

START = NOW.timestamp() + shooting.LEAD_SECONDS
SIZES = [{"slug": slug, "available": True, "regions": ["lon1", "nyc3"]} for slug in ("c-2", "c-4")]


def cell_of(droplet):
    """(network, region) from a worker name `pic-<sid>-h00-<region>-<network>`."""
    region, network = droplet.name.split("-h00-")[1].split("-", 1)
    return network, region


class ShotRemote(FakeProbeRemote):
    """Also records the clock at each step and answers per (network, region)."""

    def __init__(self, clock, answers=None, unready=(), setup_fails=()):
        super().__init__({})
        self.clock, self.by_cell = clock, answers or {}
        self.unready, self.setup_fails, self.started = unready, setup_fails, {}

    async def wait_ready(self, droplet, limit_seconds):
        await asyncio.sleep(0)  # SSH yields: every worker gets ready before the first sleeps
        return cell_of(droplet) not in self.unready

    async def run(self, droplet, command, what, *, stdin=None, seconds=120):
        await asyncio.sleep(0)
        cell = cell_of(droplet)
        if command[0] == "python3" and cell in self.setup_fails:
            raise RemoteError("probe setup failed (exit 1)")
        if command[0] != "python3":
            self.started[cell] = self.clock.now()
            answer = self.by_cell.get(cell)
            if answer is not None:
                self.calls.append((command, stdin, what))
                return answer if isinstance(answer, bytes) else (json.dumps(answer) + "\n").encode()
        return await super().run(droplet, command, what, stdin=stdin, seconds=seconds)


def shot_world(tmp_path, **changes):
    probe_inputs, spec = world(tmp_path)
    inputs = ShotInputs(
        probe_inputs.root, tmp_path / "manager" / "shots", size_by_network={"soax": "c-2"}
    )
    return replace(inputs, **changes), spec


async def fired(fake, remote, inputs, clock):
    if fake.sizes[0]["regions"] != ["lon1", "nyc3"] and not getattr(fake, "keep_sizes", False):
        fake.sizes = SIZES
    async with running(fake) as cloud:
        return await shooting.shot(cloud, remote.factory, inputs, clock=clock)


async def test_every_cell_starts_at_the_recorded_time_and_every_worker_is_removed(tmp_path):
    fake, clock = Recording(), FakeClock(NOW.timestamp())
    fake.seed_production(40)
    production = set(fake.droplets)
    inputs, _ = shot_world(tmp_path)
    remote = ShotRemote(clock, {("webshare", "lon1"): summary(statuses={"429": 1000})})
    result = await fired(fake, remote, inputs, clock)

    sid = result["shot_id"].split("-")[1][:8]
    assert result["start_at"] == "2027-01-01T22:10:00.000Z" and "error" not in result
    assert sorted((c["region"], c["size"], tuple(c["names"])) for c in fake.creates) == [
        ("lon1", "c-2", (f"pic-{sid}-h00-lon1-soax",)),
        ("lon1", "c-4", (f"pic-{sid}-h00-lon1-webshare",)),
        ("nyc3", "c-2", (f"pic-{sid}-h00-nyc3-soax",)),
        ("nyc3", "c-4", (f"pic-{sid}-h00-nyc3-webshare",)),
    ]
    assert all(
        c["tags"] == [WORKER_TAG, f"pi-camp-{sid}", f"pi-camp-{sid}-h00"] for c in fake.creates
    )
    assert set(remote.started.values()) == {START} and len(remote.started) == 4
    commands = {
        tuple(
            cmd[cmd.index(flag) + 1]
            for flag in ("--provider", "--location", "--country", "--concurrency")
        )
        for cmd, _, what in remote.calls
        if what.startswith("shot ")
    }
    assert commands == {
        ("soax", "lon1", "gb", "500"),
        ("soax", "nyc3", "us", "500"),
        ("webshare", "lon1", "gb", "2000"),
        ("webshare", "nyc3", "us", "2000"),
    }

    cells = result["cells"]
    assert {k: c["outcome"] for k, c in cells.items()} == dict.fromkeys(
        ["soax/lon1", "webshare/lon1", "soax/nyc3", "webshare/nyc3"], "measured"
    )
    assert cells["soax/lon1"] | {"step": None} == {
        "country": "GB",
        "size": "c-2",
        "concurrency": 500,
        "outcome": "measured",
        "start_lag_seconds": 0.0,
        "step": None,
        "step_down": [],
    }
    assert cells["soax/nyc3"]["step"]["unique_ips"] == 24_000
    assert cells["webshare/lon1"]["step_down"] == ["rate_limited"]
    assert result["workers_left"] == 0 and "recover" not in result
    assert all(w["status"] == "destroyed" for w in result["workers"])
    assert set(fake.droplets) == production and len(fake.deleted) == 4

    folder = inputs.output / result["shot_id"]
    assert json.loads((folder / "shot-result.json").read_text()) == result
    table = (folder / "shot-result.md").read_text()
    assert "| soax | lon1 | GB | c-2 | 500 | measured | 25000 |" in table
    assert "| 24000 | 0.0 | - |" in table and "rate_limited" in table
    written = "".join(p.read_text() for p in folder.rglob("*") if p.is_file())
    assert not IP.search(written) and "secret-" not in written


async def test_a_network_country_override_and_the_setup_payload(tmp_path):
    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
    inputs, _ = shot_world(
        tmp_path,
        country_by_network={"webshare": "US"},
        size_by_network={},
        networks=("webshare",),
        regions=("lon1",),
    )
    remote = ShotRemote(clock)
    result = await fired(fake, remote, inputs, clock)
    assert list(result["cells"]) == ["webshare/lon1"]
    assert result["cells"]["webshare/lon1"]["country"] == "US"
    (setup, data, _), (step, _, _) = remote.calls
    assert setup[:2] == ["python3", "-c"] and b"secret-webshare" in data
    assert b"secret-soax" not in data and b"secret-" not in json.dumps(setup).encode()
    assert step[step.index("--country") + 1] == "us"


async def test_a_worker_not_ready_by_the_start_is_late_and_never_started(tmp_path):
    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
    inputs, _ = shot_world(tmp_path)
    remote = ShotRemote(clock, unready=[("webshare", "nyc3")], setup_fails=[("soax", "lon1")])
    result = await fired(fake, remote, inputs, clock)
    outcomes = {k: c["outcome"] for k, c in result["cells"].items()}
    assert outcomes == {
        "soax/lon1": "setup_failed",
        "webshare/lon1": "measured",
        "soax/nyc3": "measured",
        "webshare/nyc3": "late",
    }
    assert set(remote.started) == {("webshare", "lon1"), ("soax", "nyc3")}
    assert result["workers_left"] == 0 and len(fake.deleted) == 4


async def test_setup_after_the_start_is_late(tmp_path):
    class Slow(ShotRemote):
        async def run(self, droplet, command, what, *, stdin=None, seconds=120):
            if command[0] == "python3":
                self.clock.t = START + 1
            return await super().run(droplet, command, what, stdin=stdin, seconds=seconds)

    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
    inputs, _ = shot_world(tmp_path, networks=("soax",), regions=("lon1",))
    result = await fired(fake, Slow(clock), inputs, clock)
    assert result["cells"]["soax/lon1"]["outcome"] == "late" and len(fake.deleted) == 1


async def test_an_unreadable_step_is_run_failed(tmp_path):
    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
    inputs, _ = shot_world(tmp_path, networks=("soax",), regions=("lon1",))
    result = await fired(fake, ShotRemote(clock, {("soax", "lon1"): b"no json\n"}), inputs, clock)
    cell = result["cells"]["soax/lon1"]
    assert cell["outcome"] == "run_failed" and cell["step_down"] == ["run_failed"]


async def test_creates_are_batched_and_a_lost_answer_is_adopted(tmp_path, monkeypatch):
    monkeypatch.setattr(shooting, "CREATE_BATCH", 1)
    fake, clock = Recording(), FakeClock(NOW.timestamp())
    fake.create_then_fail = 1  # created, but the answer is lost
    inputs, _ = shot_world(tmp_path, size_by_network={}, regions=("lon1",))
    result = await fired(fake, ShotRemote(clock), inputs, clock)
    assert [len(c["names"]) for c in fake.creates] == [1, 1]
    assert all(c["outcome"] == "measured" for c in result["cells"].values())
    assert result["workers_left"] == 0 and len(fake.deleted) == 2


async def test_a_create_that_surely_failed_is_reported_and_nothing_is_left(tmp_path):
    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
    fake.faults.append(Fault("POST", "/v2/droplets", 422, times=5))
    inputs, _ = shot_world(tmp_path, networks=("soax",), regions=("lon1",))
    result = await fired(fake, ShotRemote(clock), inputs, clock)
    [worker] = result["workers"]
    assert worker["status"] == "create_failed" and worker["error_class"] == "create_failed"
    assert result["cells"]["soax/lon1"]["outcome"] == "create_failed"
    assert result["workers_left"] == 0 and fake.deleted == []


async def test_an_unconfirmed_worker_is_never_fired(tmp_path):
    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
    fake.faults.append(Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 400, times=3))
    inputs, _ = shot_world(tmp_path, networks=("soax",), regions=("lon1",))
    remote = ShotRemote(clock)
    result = await fired(fake, remote, inputs, clock)
    assert remote.calls == [] and result["workers"][0]["status"] == "destroyed"
    audit = [
        json.loads(line)
        for line in audit_path(inputs.output / result["shot_id"]).read_text().splitlines()
    ]
    assert audit[-1]["path"] == "own_unassigned" and audit[-1]["result"] == "deleted"


async def test_bad_inputs_are_refused_before_any_call(tmp_path):
    for index, (changes, text) in enumerate(
        (
            ({"regions": ("fra1",)}, "regions not planned"),
            ({"networks": ("iproyal",)}, "networks not planned"),
            ({"size_by_network": {"iproyal": "c-2"}}, "unplanned network"),
            ({"size": "huge"}, "size"),
            ({"requests": 0}, "requests or lead"),
        )
    ):
        fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
        inputs, _ = shot_world(tmp_path / str(index), **changes)
        with pytest.raises(ProbeError, match=text):
            await fired(fake, ShotRemote(clock), inputs, clock)
        assert fake.requests == [] and not inputs.output.exists()
    inputs, _ = shot_world(tmp_path / "x")
    (inputs.root.parent.parent / ".secrets" / "credentials" / "soax.toml").unlink()
    with pytest.raises(ProbeError, match="no credentials for soax"):
        await fired(FakeDigitalOcean(), ShotRemote(clock), inputs, clock)
    with pytest.raises(ProbeError, match="no planned campaign"):
        await fired(
            FakeDigitalOcean(), ShotRemote(clock), replace(inputs, root=tmp_path / "none"), clock
        )


async def test_a_size_not_offered_stops_before_any_create(tmp_path):
    fake, clock = Recording(), FakeClock(NOW.timestamp())
    fake.keep_sizes = True  # the fake offers c-2 in lon1 only
    inputs, _ = shot_world(tmp_path)
    result = await fired(fake, ShotRemote(clock), inputs, clock)
    assert result["error"].startswith("ProbeError: not available: c-2 in nyc3, c-4 in lon1")
    assert fake.creates == [] and result["workers_left"] == 0


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


def test_the_shot_command_is_wired(tmp_path, monkeypatch, capsys):
    seen = {}

    async def fake_shot(cloud, remotes, inputs):
        seen.update(inputs=inputs, remote=remotes(tmp_path / "s"))
        return {
            "shot_id": "s",
            "workers_left": 0,
            "workers": [],
            "cells": {"a/b": {"outcome": "measured"}, "c/d": {"outcome": "late"}},
        }

    monkeypatch.setattr(shooting, "shot", fake_shot)
    monkeypatch.setattr("proxy_index.campaign.do_api.file_token", lambda path: lambda: "t")
    assert (
        main_cli.main(
            [
                "campaign",
                "shot",
                "--root",
                str(tmp_path),
                "--networks",
                ",".join(PROBED[:2]),
                "--regions",
                "lon1,nyc3",
                "--size-by-network",
                "soax=c-2",
                "--country-by-network",
                "webshare=us",
                "--lead-minutes",
                "5",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "shot_id": "s",
        "workers_left": 0,
        "outcomes": {"measured": 1, "late": 1},
    }
    inputs = seen["inputs"]
    assert (inputs.networks, inputs.regions, inputs.requests, inputs.size) == (
        ("soax", "webshare"),
        ("lon1", "nyc3"),
        25_000,
        "c-4",
    )
    assert (inputs.size_by_network, inputs.country_by_network, inputs.lead_seconds) == (
        {"soax": "c-2"},
        {"webshare": "US"},
        300.0,
    )
    assert seen["remote"].runtime == tmp_path / "s" / ".runtime"

    async def left(cloud, folder):
        return {"shot_id": "s", "workers_left": 1}

    monkeypatch.setattr(shooting, "recover", left)
    assert main_cli.main(["campaign", "shot", "--remove", str(tmp_path)]) == 3

    async def refused(cloud, remotes, inputs):
        raise ProbeError("refused: size")

    monkeypatch.setattr(shooting, "shot", refused)
    assert main_cli.main(["campaign", "shot", "--root", str(tmp_path)]) == 3
    assert main_cli.main(["campaign", "shot", "--root", "r", "--size-by-network", "soax"]) == 3
    assert "Not fired" in capsys.readouterr().err


async def test_s1_a_repeated_region_or_network_is_refused_before_any_call(tmp_path):
    for index, changes in enumerate(
        ({"regions": ("lon1", "lon1")}, {"networks": ("soax", "soax")})
    ):
        fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
        inputs, _ = shot_world(tmp_path / str(index), **changes)
        with pytest.raises(ProbeError, match="repeated"):
            await fired(fake, ShotRemote(clock), inputs, clock)
        assert fake.requests == []
    assert (
        main_cli.main(["campaign", "shot", "--root", str(tmp_path), "--regions", "lon1,lon1"]) == 3
    )


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


async def test_m3_a_list_country_network_needs_its_country(tmp_path, monkeypatch):
    # A network whose country is a preset (one dashboard list's country) loads only that one.
    data = tomllib.loads(GRAMMARS.joinpath("proxy-seller.toml").read_text())
    data["flags"]["country"] = {"kind": "country", "preset": True}
    preset = Grammar.parse(data)
    monkeypatch.setattr(shooting, "load_grammar", lambda network: preset)
    probe_inputs, _ = world(tmp_path, ("proxy-seller",))
    inputs = ShotInputs(probe_inputs.root, tmp_path / "shots", regions=("lon1",))
    clock = FakeClock(NOW.timestamp())
    with pytest.raises(ProbeError, match="proxy-seller only loads its list's country"):
        await fired(FakeDigitalOcean(), ShotRemote(clock), inputs, clock)


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


async def test_m4_workers_still_booting_are_polled_again(tmp_path):
    class Booting(FakeDigitalOcean):
        boots = 2

        async def _list(self, request):
            ours = [d for d in self.droplets.values() if d["name"].startswith("pic-")]
            status = "new" if self.boots > 0 else "active"
            self.boots -= 1
            for droplet in ours:
                droplet["status"] = status
            return await super()._list(request)

    fake, clock = Booting(), FakeClock(NOW.timestamp())
    inputs, _ = shot_world(tmp_path, networks=("soax",), regions=("lon1",))
    result = await fired(fake, ShotRemote(clock), inputs, clock)
    assert result["cells"]["soax/lon1"]["outcome"] == "measured"


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


async def test_s1_worker_names_must_be_unique(tmp_path, monkeypatch):
    monkeypatch.setattr(shooting, "droplet_name", lambda *args: "pic-00000000-h00-lon1-x")
    inputs, _ = shot_world(tmp_path, regions=("lon1",))
    clock = FakeClock(NOW.timestamp())
    with pytest.raises(ProbeError, match="worker names repeated"):
        await fired(FakeDigitalOcean(), ShotRemote(clock), inputs, clock)


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


def test_markdown_answer_class_columns():
    """CTRY-1: the shot and probe tables show rate limits in their own column and the answer
    classes beside them; a result written before them renders "-"."""
    new = probing.step_result(500, 1000, classified())
    old = {
        k: v
        for k, v in new.items()
        if not k.endswith(
            ("no_inventory_pct", "other_country_pct", "duplicates_pct", "real_error_pct")
        )
    }
    cell = {
        "country": "US",
        "size": "c-4",
        "concurrency": 500,
        "outcome": "measured",
        "start_lag_seconds": 1.5,
        "step_down": [],
    }
    shot = shooting.markdown(
        {
            "shot_id": "s",
            "requests": 1000,
            "start_at": "t",
            "cells": {"geonode/nyc3": cell | {"step": new}, "soax/lon1": cell | {"step": old}},
        }
    )
    header, rule, *rows = shot.splitlines()[4:]
    columns = [c.strip() for c in header.strip("|").split("|")]
    assert columns[8] == "Rate-limited %" and "429 %" not in shot
    assert columns[17:21] == [
        "No-IP %",
        "Other-country %",
        "Duplicates %",
        "Real errors % (answered)",
    ]
    assert rule.count("---") == len(columns) == 23
    cells = [[c.strip() for c in row.strip("|").split("|")] for row in rows]
    assert cells[0][8] == "5.0" and cells[0][17:21] == ["0.5", "1.0", "9.0", "15.263"]
    assert cells[1][17:21] == ["-"] * 4 and all(len(c) == 23 for c in cells)
    probe = probing.markdown(
        {
            "probe_id": "p",
            "requests": 1000,
            "country": "US",
            "size": "c-4",
            "region": "nyc3",
            "networks": {"geonode": {"max_concurrency": 500, "steps": [new, old]}},
        }
    )
    header, rule, *rows = probe.split("\n\n")[-1].splitlines()
    columns = [c.strip() for c in header.strip("|").split("|")]
    assert columns[4] == "Rate-limited %" and (
        columns[12:16] == ["No-IP %", "Other-country %", "Duplicates %", "Real errors % (answered)"]
    )
    assert rule.count("---") == len(columns) == 17
    cells = [[c.strip() for c in row.strip("|").split("|")] for row in rows]
    assert cells[0][12:16] == ["0.5", "1.0", "9.0", "15.263"] and cells[1][12:16] == ["-"] * 4
