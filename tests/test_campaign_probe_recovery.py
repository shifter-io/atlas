"""CONC-1: the `campaign probe` worker, its removal and `campaign probe --remove`.

The probe runs against the in-process fake DigitalOcean and a fake remote that answers each
step with a run summary; no request or SSH connection leaves the machine.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from dataclasses import replace

import pytest
from aiohttp import web
from campaign_fakes import CAMPAIGN_PROJECT, FakeDigitalOcean, Fault, no_sleep, running
from campaign_hour_cases import DROPLET, FakeRunner
from campaign_probe_cases import NOW, PROBED, FakeProbeRemote, probed, world

from proxy_index import cli as main_cli
from proxy_index.campaign import fanout
from proxy_index.campaign import probe as probing
from proxy_index.campaign import recover as recovering
from proxy_index.campaign.contracts import audit_path
from proxy_index.campaign.fanout import RemoteError
from proxy_index.campaign.probe import ProbeError


def posts(fake, path):
    return [r for r in fake.requests if r[0] == "POST" and r[1] == path]


async def test_the_worker_gets_inputs_on_stdin_only_and_fixed_commands(tmp_path):
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path, PROBED[:1])
    remote = FakeProbeRemote({})
    await probed(fake, remote, replace(inputs, start=1000, country="DE"))
    (setup, payload, _), *steps = remote.calls
    assert setup[:2] == ["python3", "-c"] and setup[-2:] == [
        "/opt/proxy-index-worker",
        "/run/proxy-index-worker",
    ]
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        names = archive.getnames()
        assert (
            archive.extractfile("secrets/soax.toml").read()
            == (inputs.root.parents[1] / ".secrets" / "credentials" / "soax.toml").read_bytes()
        )
        assert {m.mode for m in archive.getmembers()} == {0o600}
    assert names == [
        "wheel/proxy_index-1.0-py3-none-any.whl",
        "probe/soax.toml",
        "secrets/soax.toml",
    ]
    commands = [command for command, stdin, _ in steps]
    assert all(stdin is None for _, stdin, _ in steps) and len(commands) == 2
    argv = commands[1]
    assert argv[:3] == ["sh", "-c", probing.launcher(fanout.MMDB_DIR)]
    assert argv[argv.index("--concurrency") + 1] == "1000"
    assert argv[argv.index("--requests") + 1] == "25000"
    assert argv[argv.index("--param") + 1] == "country=DE"
    assert argv[argv.index("--target") + 1] == "generic_http"
    assert argv[argv.index("--credentials-file") + 1] == "/run/proxy-index-worker/probe/soax.toml"
    assert "secret" not in " ".join(argv)


async def test_a_setup_or_ssh_failure_still_removes_the_worker(tmp_path):
    for index, (remote, error) in enumerate(
        (
            (
                FakeProbeRemote({}, setup=RemoteError("probe setup failed (exit 1)")),
                "RemoteError: probe setup failed (exit 1)",
            ),
            (FakeProbeRemote({}, setup=b"oops\n"), "ProbeError: probe setup did not finish"),
            (FakeProbeRemote({}, ready=False), "ProbeError: worker SSH never became ready"),
        )
    ):
        fake = FakeDigitalOcean()
        inputs, _ = world(tmp_path / str(index))
        result = await probed(fake, remote, inputs)
        assert result["error"] == error and result["networks"] == {}
        assert result["worker_status"] == "destroyed" and fake.deleted == [
            ("droplet", result["worker"]["droplet_id"])
        ]


async def test_an_unconfirmed_worker_is_removed_on_the_own_unassigned_path(tmp_path):
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 400, times=3))
    inputs, _ = world(tmp_path)
    remote = FakeProbeRemote({})
    result = await probed(fake, remote, inputs)
    assert result["error"].startswith("ProbeError: worker not confirmed in the project")
    assert remote.calls == [] and result["worker_status"] == "destroyed"
    audit = [
        json.loads(line)
        for line in audit_path(inputs.output / result["probe_id"]).read_text().splitlines()
    ]
    assert audit[-1]["path"] == "own_unassigned" and audit[-1]["result"] == "deleted"


async def test_an_assignment_error_is_retried_by_the_removal_then_recovered(tmp_path, monkeypatch):
    """S1: an assignment that raised leaves no mark; the removal confirms it first."""
    real, calls = probing.scope.assign_and_verify_project, []

    async def once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("assignment crashed")
        return await real(*args, **kwargs)

    monkeypatch.setattr(probing.scope, "assign_and_verify_project", once)
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path / "a")
    result = await probed(fake, FakeProbeRemote({}), inputs)
    assert result["error"] == "RuntimeError: assignment crashed" and len(calls) == 2
    assert result["worker_status"] == "destroyed" and len(fake.deleted) == 1
    assert "recover" not in result

    async def broken(*args, **kwargs):
        raise RuntimeError("assignment crashed")

    monkeypatch.setattr(probing.scope, "assign_and_verify_project", broken)
    fake = FakeDigitalOcean()
    inputs, _ = world(tmp_path / "b")
    result = await probed(fake, FakeProbeRemote({}), inputs)
    folder = inputs.output / result["probe_id"]
    assert result["worker_status"] == "destroy_failed" and fake.deleted == []
    assert result["recover"] == f"campaign probe --remove {folder}"
    monkeypatch.setattr(probing.scope, "assign_and_verify_project", real)
    async with running(fake) as cloud:  # a later process: a new client, nothing in memory
        assert await recovering.recover(cloud, folder, sleep=no_sleep) == {
            "probe_id": result["probe_id"],
            "worker_status": "destroyed",
        }
    saved = json.loads((folder / "probe-result.json").read_text())
    assert saved["worker_status"] == "destroyed" and "recover" not in saved
    assert fake.deleted == [("droplet", result["worker"]["droplet_id"])]


async def test_a_lost_create_answer_is_adopted_and_a_failed_create_reported(tmp_path):
    fake = FakeDigitalOcean()
    fake.create_then_fail = 1
    inputs, _ = world(tmp_path / "a")
    result = await probed(fake, FakeProbeRemote({}), inputs)
    assert "error" not in result and result["worker_status"] == "destroyed"
    assert len(posts(fake, "/v2/droplets")) == 1
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", "/v2/droplets", 422))
    inputs, _ = world(tmp_path / "b")
    result = await probed(fake, FakeProbeRemote({}), inputs)
    assert (
        result["error"].startswith("CreateError")
        and result["worker_status"] == ("create_failed")
        and fake.deleted == []
    )


async def test_a_guard_refusal_or_error_is_reported_never_bypassed(tmp_path, monkeypatch):
    for failure, status in (
        (probing.scope.OwnershipError("droplet", "1", ["tags"]), "destroy_refused"),
        (OSError("down"), "destroy_failed"),
    ):

        async def refuse(*args, failure=failure, **kwargs):
            raise failure

        monkeypatch.setattr(probing.scope, "guarded_delete", refuse)
        fake = FakeDigitalOcean()
        inputs, _ = world(tmp_path / status)
        result = await probed(fake, FakeProbeRemote({}), inputs)
        assert result["worker_status"] == status and fake.deleted == []


async def test_the_ssh_remote_runs_one_fixed_command_with_stdin(tmp_path):
    runner = FakeRunner((0, b"ready\n"), (3, b"203.0.113.7 noise"))
    remote = fanout.OpenSSHRemote(tmp_path / "key", tmp_path / "rt", 0, runner=runner)
    assert (
        await remote.run(DROPLET, ["python3", "-c", "x"], "probe setup", stdin=b"tar") == b"ready\n"
    )
    assert runner.calls[0][1] == b"tar" and runner.calls[0][0][-1] == "python3 -c x"
    with pytest.raises(RemoteError, match=r"^probe soax failed \(exit 3\)$"):
        await remote.run(DROPLET, ["true"], "probe soax")


def test_the_setup_script_writes_private_credentials_and_installs_like_a_round(tmp_path):
    """m5: the probe and the round share one wheel install block."""
    import os
    import stat
    import subprocess
    import sys

    assert fanout.INSTALL_WHEEL in probing.SETUP_SCRIPT
    assert fanout.INSTALL_WHEEL in fanout.START_SCRIPT
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "uv").write_text('#!/bin/sh\necho "$@" >> "$(dirname "$0")/calls"\n')
    (stub / "uv").chmod(0o755)
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for name, data in (
            ("wheel/w-1-py3-none-any.whl", b"wheel"),
            ("probe/soax.toml", b"provider = 'soax'\n"),
            ("secrets/soax.toml", b"password = 'x'\n"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    root, secrets = tmp_path / "worker", tmp_path / "run"
    env = os.environ | {"PATH": f"{stub}:{os.environ['PATH']}"}
    for _ in range(2):  # the second push finds the same wheel installed
        reply = subprocess.run(
            [sys.executable, "-c", probing.SETUP_SCRIPT, str(root), str(secrets)],
            input=payload.getvalue(),
            capture_output=True,
            env=env,
            check=True,
        )
        assert reply.stdout.decode().splitlines()[-1] == "ready"
    credentials = secrets / "probe" / "soax.toml"
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert (root / "probe" / "soax.toml").exists() and not (root / "secrets").exists()
    assert len((stub / "calls").read_text().splitlines()) == 1


class Hidden(FakeDigitalOcean):
    """The create answer is lost and the first `hide` tag listings do not show it yet."""

    def __init__(self, hide):
        super().__init__()
        self.hide, self.create_then_fail = hide, 1

    async def _list(self, request):
        if self.hide and request.query.get("tag_name"):
            self.hide -= 1
            return web.json_response({"droplets": [], "meta": {"total": 0}})
        return await super()._list(request)


def sleeps_into(seen):
    async def sleep(seconds):
        seen.append(seconds)

    return sleep


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


async def died(fake, inputs, monkeypatch, stage):
    """A probe process that stops for good (SIGKILL, OOM, reboot) at `stage`: no cleanup."""
    if stage == "before_assignment":

        async def gone(*args, **kwargs):
            raise RuntimeError("killed")

        monkeypatch.setattr(probing.scope, "assign_and_verify_project", gone)
    async with running(fake) as cloud:
        run = probing.Probe(cloud, FakeProbeRemote({}).factory, inputs, NOW, no_sleep)
        with pytest.raises(RuntimeError if stage == "before_assignment" else ProbeError):
            await run.create()
            raise ProbeError("killed after the assignment")
    monkeypatch.undo()
    return inputs.output / run.probe_id


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
    monkeypatch.setattr("proxy_index.campaign.do_api.file_token", lambda path: lambda: "t")
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
    [(rid, first)] = [(i, d) for i, d in fake.droplets.items() if d["name"].startswith("pic-")]
    fake.add(first["name"], first["tags"], project=None)  # DigitalOcean made one too many
    async with running(fake) as cloud:
        assert (await recovering.recover(cloud, folder, sleep=no_sleep))[
            "worker_status"
        ] == "destroyed"
    assert not [d for d in fake.droplets.values() if d["name"].startswith("pic-")]
    record = json.loads((folder / "probe-result.json").read_text())
    assert record["worker"]["droplet_id"] == rid and "recover" not in record
    assert [w["status"] for w in record["unrecorded"]] == ["destroyed"]
