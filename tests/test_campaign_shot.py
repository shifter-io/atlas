"""SYNC-1: `campaign shot`, one fixed step per network and region started at one time.

Runs against the in-process fake DigitalOcean, a fake clock and a fake remote that answers
each step with a run summary; no request or SSH connection leaves the machine.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from importlib import resources

import pytest
from campaign_fakes import CAMPAIGN_PROJECT, FakeDigitalOcean, Fault
from campaign_hour_cases import FakeClock
from campaign_probe_cases import IP, NOW, PROBED, Recording, summary, world
from campaign_shot_cases import START, ShotRemote, fired, shot_world

from atlas import cli as main_cli
from atlas.campaign import shot as shooting
from atlas.campaign import shot_steps
from atlas.campaign.contracts import WORKER_TAG, audit_path, spec_path
from atlas.campaign.probe import ProbeError
from atlas.campaign.shot import ShotInputs
from atlas.providers.grammar import Grammar

GRAMMARS = resources.files("atlas.providers").joinpath("grammars")


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
        ("lon1", "c-2", (f"atlas-{sid}-h00-lon1-soax",)),
        ("lon1", "c-4", (f"atlas-{sid}-h00-lon1-webshare",)),
        ("nyc3", "c-2", (f"atlas-{sid}-h00-nyc3-soax",)),
        ("nyc3", "c-4", (f"atlas-{sid}-h00-nyc3-webshare",)),
    ]
    assert all(
        c["tags"] == [WORKER_TAG, f"atlas-camp-{sid}", f"atlas-camp-{sid}-h00"] for c in fake.creates
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


async def test_a_staging_campaign_is_refused(tmp_path):
    """CAMP-STAGING-1: a shot creates production's workers only."""
    fake, clock = FakeDigitalOcean(), FakeClock(NOW.timestamp())
    inputs, spec = shot_world(tmp_path)
    spec_path(inputs.root).write_text(replace(spec, environment="staging").to_json())
    with pytest.raises(ProbeError, match="production's workers only"):
        await fired(fake, ShotRemote(clock), inputs, clock)
    assert fake.requests == [] and not inputs.output.exists()


async def test_a_size_not_offered_stops_before_any_create(tmp_path):
    fake, clock = Recording(), FakeClock(NOW.timestamp())
    fake.keep_sizes = True  # the fake offers c-2 in lon1 only
    inputs, _ = shot_world(tmp_path)
    result = await fired(fake, ShotRemote(clock), inputs, clock)
    assert result["error"].startswith("ProbeError: not available: c-2 in nyc3, c-4 in lon1")
    assert fake.creates == [] and result["workers_left"] == 0


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
    monkeypatch.setattr("atlas.campaign.do_api.file_token", lambda path: lambda: "t")
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


async def test_m3_a_list_country_network_needs_its_country(tmp_path, monkeypatch):
    # A network whose country is a preset (one dashboard list's country) loads only that one.
    data = tomllib.loads(GRAMMARS.joinpath("proxy-seller.toml").read_text())
    data["flags"]["country"] = {"kind": "country", "preset": True}
    preset = Grammar.parse(data)
    monkeypatch.setattr(shot_steps, "load_grammar", lambda network: preset)
    probe_inputs, _ = world(tmp_path, ("proxy-seller",))
    inputs = ShotInputs(probe_inputs.root, tmp_path / "shots", regions=("lon1",))
    clock = FakeClock(NOW.timestamp())
    with pytest.raises(ProbeError, match="proxy-seller only loads its list's country"):
        await fired(FakeDigitalOcean(), ShotRemote(clock), inputs, clock)


async def test_m4_workers_still_booting_are_polled_again(tmp_path):
    class Booting(FakeDigitalOcean):
        boots = 2

        async def _list(self, request):
            ours = [d for d in self.droplets.values() if d["name"].startswith("atlas-")]
            status = "new" if self.boots > 0 else "active"
            self.boots -= 1
            for droplet in ours:
                droplet["status"] = status
            return await super()._list(request)

    fake, clock = Booting(), FakeClock(NOW.timestamp())
    inputs, _ = shot_world(tmp_path, networks=("soax",), regions=("lon1",))
    result = await fired(fake, ShotRemote(clock), inputs, clock)
    assert result["cells"]["soax/lon1"]["outcome"] == "measured"


async def test_s1_worker_names_must_be_unique(tmp_path, monkeypatch):
    monkeypatch.setattr(shot_steps, "droplet_name", lambda *args: "atlas-00000000-h00-lon1-x")
    inputs, _ = shot_world(tmp_path, regions=("lon1",))
    clock = FakeClock(NOW.timestamp())
    with pytest.raises(ProbeError, match="worker names repeated"):
        await fired(FakeDigitalOcean(), ShotRemote(clock), inputs, clock)
