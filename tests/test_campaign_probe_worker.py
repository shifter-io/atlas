"""CONC-1: the `campaign probe` worker, its inputs, its setup and its removal in the run.

The probe runs against the in-process fake DigitalOcean and a fake remote that answers each
step with a run summary; no request or SSH connection leaves the machine. The S1 recovery
(`campaign probe --remove`) is in test_campaign_probe_recovery.py.
"""

from __future__ import annotations

import io
import json
import tarfile
from dataclasses import replace

import pytest
from campaign_fakes import CAMPAIGN_PROJECT, FakeDigitalOcean, Fault, no_sleep, running
from campaign_hour_cases import DROPLET, FakeRunner
from campaign_probe_cases import PROBED, FakeProbeRemote, probed, world
from campaign_probe_recovery_cases import posts

from atlas.campaign import fanout
from atlas.campaign import probe as probing
from atlas.campaign import recover as recovering
from atlas.campaign.contracts import audit_path
from atlas.campaign.fanout import RemoteError


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
        "wheel/atlas-1.0-py3-none-any.whl",
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
    assert len((stub / "calls").read_text().splitlines()) == 2  # install, then check
