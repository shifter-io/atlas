"""CONC-1 S1: `campaign probe --remove` and the recovery of a worker the probe could not
remove: lost creates, dead probes, signals and listing errors.

The probe runs against the in-process fake DigitalOcean and a fake remote that answers each
step with a run summary; no request or SSH connection leaves the machine.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from campaign_fakes import CAMPAIGN_PROJECT, FakeDigitalOcean, Fault, no_sleep, running
from campaign_probe_cases import NOW, FakeProbeRemote, probed, world
from campaign_probe_recovery_cases import Hidden, died, posts, sleeps_into

from atlas import cli as main_cli
from atlas.campaign import probe as probing
from atlas.campaign import recover as recovering
from atlas.campaign.contracts import audit_path
from atlas.campaign.probe import ProbeError


async def test_s1_a_lost_create_is_looked_up_again_after_the_launch_delay(tmp_path):
    fake, seen = Hidden(1), []
    inputs, _ = world(tmp_path)
    async with running(fake) as cloud:
        result = await probing.probe(
            cloud, FakeProbeRemote({}).factory, inputs, now=NOW, sleep=sleeps_into(seen)
        )
    assert probing.CREATE_RETRY_SECONDS in seen and "error" not in result
    assert len(posts(fake, "/v2/droplets")) == 1 and result["worker_status"] == "destroyed"


async def test_s1_a_create_still_unlisted_is_reported_and_recovered_later(tmp_path):
    fake = Hidden(3)  # the first look, the look 15 s later, the last look in the cleanup
    fake.seed_production(40)
    production = set(fake.droplets)
    inputs, _ = world(tmp_path)
    result = await probed(fake, FakeProbeRemote({}), inputs)
    folder = inputs.output / result["probe_id"]
    assert result["worker"]["error_class"] == "create_uncertain"
    assert result["worker_status"] == "create_failed" and result["worker"]["droplet_id"] is None
    assert result["recover"] == f"campaign probe --remove {folder}"
    [left] = set(fake.droplets) - production  # the droplet the lost answer created
    async with running(fake) as cloud:
        assert (await recovering.recover(cloud, folder, sleep=no_sleep))["worker_status"] == (
            "destroyed"
        )
    assert fake.deleted == [("droplet", left)] and set(fake.droplets) == production
    async with running(fake) as cloud:  # a rerun finds it removed and does nothing
        assert (await recovering.recover(cloud, folder))["worker_status"] == "destroyed"
    assert len(fake.deleted) == 1


async def test_s1_an_unreadable_listing_or_a_create_that_never_landed(tmp_path):
    fake = FakeDigitalOcean()
    fake.create_then_fail = 1
    fake.faults.append(Fault("GET", "/v2/droplets", 400, times=10))
    inputs, _ = world(tmp_path / "a")
    result = await probed(fake, FakeProbeRemote({}), inputs)
    assert result["worker"]["error_class"] == "create_uncertain" and "recover" in result
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", "/v2/droplets", 500, times=10))  # nothing was created
    inputs, _ = world(tmp_path / "b")
    result = await probed(fake, FakeProbeRemote({}), inputs)
    async with running(fake) as cloud:
        found = await recovering.recover(cloud, inputs.output / result["probe_id"])
    assert found["worker_status"] == "absent" and fake.deleted == []


@pytest.mark.parametrize("stage", ["before_assignment", "after_assignment"])
async def test_s1_a_dead_probe_is_recovered_through_the_guard(tmp_path, monkeypatch, stage):
    fake = FakeDigitalOcean()
    fake.seed_production(40)
    production = set(fake.droplets)
    inputs, _ = world(tmp_path)
    folder = await died(fake, inputs, monkeypatch, stage)
    worker = json.loads((folder / "probe-result.json").read_text())["worker"]
    assert worker["droplet_id"] and bool(worker["assigned_at"]) == (stage == "after_assignment")
    async with running(fake) as cloud:  # nothing in memory: the record is all it has
        assert (await recovering.recover(cloud, folder, sleep=no_sleep))["worker_status"] == (
            "destroyed"
        )
    assert fake.deleted == [("droplet", worker["droplet_id"])] and set(fake.droplets) == (
        production
    )
    audit = [json.loads(line) for line in audit_path(folder).read_text().splitlines()]
    assert audit[-1]["result"] == "deleted" and audit[-1]["path"] == "normal"


async def test_s1_a_marked_worker_whose_removal_failed_takes_the_own_unassigned_path(tmp_path):
    fake = FakeDigitalOcean()
    fake.faults += [
        Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 400, times=3),
        Fault("DELETE", "", 500),
    ]  # path set below, once the id is known
    inputs, _ = world(tmp_path)
    async with running(fake) as cloud:
        run = probing.Probe(cloud, FakeProbeRemote({}).factory, inputs, NOW, no_sleep)
        original = cloud.create

        async def create(*args, **kwargs):
            made = await original(*args, **kwargs)
            fake.faults[-1] = Fault("DELETE", f"/v2/droplets/{made[0].id}", 500, times=10)
            return made

        cloud.create = create
        result = await run.run()
    assert result["worker"]["assign_failed_at"] and result["worker_status"] == "destroy_failed"
    fake.faults.clear()
    async with running(fake) as cloud:
        assert (await recovering.recover(cloud, inputs.output / run.probe_id))[
            "worker_status"
        ] == "destroyed"
    audit = [
        json.loads(line)
        for line in audit_path(inputs.output / run.probe_id).read_text().splitlines()
    ]
    assert audit[-1]["path"] == "own_unassigned" and audit[-1]["result"] == "deleted"


class Crash(BaseException):
    """The probe process dying: nothing after it runs, only what is on disk survives."""


async def test_a_crash_between_the_mark_and_the_delete_keeps_the_mark_on_disk(
    tmp_path, monkeypatch
):
    """PROBE-SAVE-1 (CAMP-1r n2): the probe saved its record only after
    `assign_and_verify_project` returned, which includes the delete. A crash in between left
    the worker unmarked on disk. Now the mark is saved before the delete, and `--remove` in a
    new process takes the own-unassigned path."""
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 400, times=3))
    inputs, _ = world(tmp_path)
    real = probing.scope.guarded_delete

    async def crash(*_args):
        monkeypatch.setattr(probing.scope, "guarded_delete", real)
        raise Crash

    monkeypatch.setattr(probing.scope, "guarded_delete", crash)
    async with running(fake) as cloud:
        run = probing.Probe(cloud, FakeProbeRemote({}).factory, inputs, NOW, no_sleep)
        with pytest.raises(Crash):
            await run.create()
    folder = inputs.output / run.probe_id
    worker = json.loads((folder / "probe-result.json").read_text())["worker"]
    assert (worker["status"], worker["assigned_at"]) == ("assign_failed", None)
    assert worker["assign_failed_at"] and worker["droplet_id"] in fake.droplets
    async with running(fake) as cloud:  # nothing in memory: the record is all it has
        assert (await recovering.recover(cloud, folder, sleep=no_sleep))["worker_status"] == (
            "destroyed"
        )
    assert fake.deleted == [("droplet", worker["droplet_id"])]
    audit = [json.loads(line) for line in audit_path(folder).read_text().splitlines()]
    assert audit[-1]["path"] == "own_unassigned" and audit[-1]["result"] == "deleted"


async def test_s1_recovery_never_touches_a_droplet_the_record_does_not_own(tmp_path):
    fake = FakeDigitalOcean()
    fake.seed_production(20)
    inputs, _ = world(tmp_path)
    result = await probed(fake, FakeProbeRemote({}, ready=False), inputs)
    folder = inputs.output / result["probe_id"]
    record = json.loads((folder / "probe-result.json").read_text())
    victim = next(iter(fake.droplets))
    record["worker"].update(droplet_id=victim, status="destroy_failed", assigned_at=None)
    (folder / "probe-result.json").write_text(json.dumps(record))
    async with running(fake) as cloud:
        assert (await recovering.recover(cloud, folder, sleep=no_sleep))["worker_status"] == (
            "destroy_refused"
        )
    assert victim in fake.droplets and fake.deleted[-1] != ("droplet", victim)
    with pytest.raises(ProbeError, match="no probe record"):
        async with running(fake) as cloud:
            await recovering.recover(cloud, tmp_path)


async def test_s1_a_signal_cancels_the_probe_and_the_worker_is_still_removed(tmp_path, monkeypatch):
    class Cancelled(FakeProbeRemote):
        async def run(self, droplet, command, what, *, stdin=None, seconds=120):
            if what == "probe webshare":
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            return await super().run(droplet, command, what, stdin=stdin, seconds=seconds)

    real = probing.release

    async def interrupted(*args, **kwargs):  # a second signal during the removal
        outer.cancel()
        await asyncio.sleep(0)
        return await real(*args, **kwargs)

    monkeypatch.setattr(probing, "release", interrupted)
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path)
    outer = asyncio.current_task()
    result = await probed(fake, Cancelled({}), inputs)
    assert result["error"] == "cancelled by a signal" and result["worker_status"] == "destroyed"
    assert list(result["networks"]) == ["soax", "webshare"] and len(fake.deleted) == 1
    assert (
        json.loads((inputs.output / result["probe_id"] / "probe-result.json").read_text())[
            "worker_status"
        ]
        == "destroyed"
    )


def test_s1_the_cli_cancels_on_sigterm_and_wires_remove(tmp_path, monkeypatch, capsys):
    import os
    import signal

    async def stopped(cloud, remotes, inputs):
        os.kill(os.getpid(), signal.SIGTERM)
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            os.kill(os.getpid(), signal.SIGTERM)  # ignored while the removal runs
            await asyncio.sleep(0)
            return {"worker_status": "destroyed", "error": "cancelled by a signal"}
        return {"worker_status": "destroyed"}

    monkeypatch.setattr(probing, "probe", stopped)
    monkeypatch.setattr("atlas.campaign.do_api.file_token", lambda path: lambda: "t")
    assert main_cli.main(["campaign", "probe", "--root", str(tmp_path)]) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "cancelled by a signal"

    async def removed(cloud, folder):
        return {"probe_id": "p", "worker_status": "absent"}

    monkeypatch.setattr(recovering, "recover", removed)
    assert main_cli.main(["campaign", "probe", "--remove", str(tmp_path)]) == 0

    async def interrupted(cloud, folder):
        raise asyncio.CancelledError

    monkeypatch.setattr(recovering, "recover", interrupted)
    assert main_cli.main(["campaign", "probe", "--remove", str(tmp_path)]) == 3
    assert "Interrupted" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main_cli.main(["campaign", "probe", "--root", "a", "--remove", "b"])


async def test_s1_a_recovered_worker_the_project_never_confirms_is_removed_fail_closed(
    tmp_path, monkeypatch
):
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path)
    folder = await died(fake, inputs, monkeypatch, "before_assignment")
    fake.faults.append(Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 400, times=3))
    async with running(fake) as cloud:
        assert (await recovering.recover(cloud, folder, sleep=no_sleep))["worker_status"] == (
            "destroyed"
        )
    audit = [json.loads(line) for line in audit_path(folder).read_text().splitlines()]
    assert audit[-1]["path"] == "own_unassigned" and len(fake.deleted) == 1


async def test_s1r_a_listing_error_during_remove_changes_nothing_and_a_rerun_removes(
    tmp_path, monkeypatch
):
    """S1r (a), (b): only a listing that answered may say absent or mark the worker ours."""
    for case in ("no_id", "neither", "get_fails"):
        fake = Hidden(3) if case == "no_id" else FakeDigitalOcean()
        inputs, _ = world(tmp_path / case)
        if case == "no_id":  # the create's answer was lost and every look missed it
            folder = inputs.output / (await probed(fake, FakeProbeRemote({}), inputs))["probe_id"]
        else:  # the process died between the create and the assignment
            folder = await died(fake, inputs, monkeypatch, "before_assignment")
        record = (folder / "probe-result.json").read_bytes()
        if case == "get_fails":
            worker_id = json.loads(record)["worker"]["droplet_id"]
            fake.faults += [Fault("GET", f"/v2/droplets/{worker_id}", 400, times=1)]
            fake.hide, fake.__class__ = 1, Hidden  # the listing answers, without the worker
        else:
            fake.faults.append(Fault("GET", "/v2/droplets", 400, times=1))
        async with running(fake) as cloud:
            with pytest.raises(ProbeError, match="could not read the probe's droplets.*again"):
                await recovering.recover(cloud, folder, sleep=no_sleep)
        assert (folder / "probe-result.json").read_bytes() == record and fake.deleted == []
        async with running(fake) as cloud:  # the outage is over
            assert (await recovering.recover(cloud, folder, sleep=no_sleep))[
                "worker_status"
            ] == "destroyed", case
        assert len(fake.deleted) == 1


class CancelledCreate(FakeDigitalOcean):
    """The create lands; the next tag listings do not show it yet."""

    def __init__(self, hide):
        super().__init__()
        self.hide = hide

    async def _list(self, request):
        if self.hide and request.query.get("tag_name"):
            self.hide -= 1
            return web.json_response({"droplets": [], "meta": {"total": 0}})
        return await super()._list(request)


@pytest.mark.parametrize("moment", ["post", "retry_wait"])
async def test_s1r_a_signal_during_the_create_still_reports_the_worker(tmp_path, moment):
    """S1r (c): cancelled during the POST or the 15 s wait: the hint, then --remove."""
    fake = CancelledCreate(1 if moment == "post" else 2)  # every in-run look misses it
    fake.create_then_fail = int(moment == "retry_wait")
    inputs, _ = world(tmp_path)
    task = asyncio.current_task()

    async def sleep(seconds):
        if seconds == probing.CREATE_RETRY_SECONDS:
            task.cancel()
            await asyncio.sleep(0)

    async with running(fake) as cloud:
        if moment == "post":
            original = cloud.create

            async def create(*args, **kwargs):
                await original(*args, **kwargs)  # it landed; the answer never arrives
                task.cancel()
                await asyncio.sleep(0)

            cloud.create = create
        result = await probing.probe(
            cloud, FakeProbeRemote({}).factory, inputs, now=NOW, sleep=sleep
        )
    folder = inputs.output / result["probe_id"]
    assert result["error"] == "cancelled by a signal" and result["worker"]["droplet_id"] is None
    assert result["worker"]["error_class"] == "create_uncertain"
    assert result["recover"] == f"campaign probe --remove {folder}" and fake.deleted == []
    async with running(fake) as cloud:
        assert (await recovering.recover(cloud, folder, sleep=no_sleep))["worker_status"] == (
            "destroyed"
        )
    assert len(fake.deleted) == 1


async def test_s1r_remove_releases_every_droplet_with_the_name_when_no_id_is_recorded(tmp_path):
    """SYNC-1 S1r: a record with no id and two droplets with its name never reads absent."""
    fake = Hidden(3)  # the create's answer was lost and every look missed it
    inputs, _ = world(tmp_path)
    folder = inputs.output / (await probed(fake, FakeProbeRemote({}), inputs))["probe_id"]
    [(rid, first)] = [(i, d) for i, d in fake.droplets.items() if d["name"].startswith("atlas-")]
    fake.add(first["name"], first["tags"], project=None)  # DigitalOcean made one too many
    async with running(fake) as cloud:
        assert (await recovering.recover(cloud, folder, sleep=no_sleep))[
            "worker_status"
        ] == "destroyed"
    assert not [d for d in fake.droplets.values() if d["name"].startswith("atlas-")]
    record = json.loads((folder / "probe-result.json").read_text())
    assert record["worker"]["droplet_id"] == rid and "recover" not in record
    assert [w["status"] for w in record["unrecorded"]] == ["destroyed"]
