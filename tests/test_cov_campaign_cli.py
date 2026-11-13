"""COV-2: `campaign manager deploy --spaces-file` writes the archive settings to the manager
after the deploy, and `campaign shot` stopped by a signal outside a shot names `--remove`
(fake DigitalOcean and fake SSH; nothing leaves the machine)."""

from __future__ import annotations

import asyncio

import pytest
from campaign_deploy_fakes import NOW, Fake, Shell, fast_assign, inventory, make_job, parse
from campaign_fakes import TOKEN, no_sleep, running

from atlas import cli as main_cli
from atlas.archive import spaces
from atlas.campaign import cli as campaign_cli
from atlas.campaign import deploy
from atlas.campaign import shot as shooting

SPACES = '[spaces]\naccess_key_id = "DO00EXAMPLE"\nsecret_access_key = "spaces-secret"\n'


@pytest.fixture
def deploy_world(tmp_path, monkeypatch):
    """The real deploy and settings push, through one fake SSH shell and no keychain."""
    fast_assign(monkeypatch)
    shell, real_deploy, real_push = Shell(), deploy.deploy, spaces.push_settings
    monkeypatch.setattr("atlas.campaign.do_api.keychain_token", lambda account: lambda: TOKEN)
    monkeypatch.setattr(
        deploy,
        "deploy",
        lambda cloud, job: real_deploy(cloud, job, shell=shell, sleep=no_sleep, now=NOW),
    )
    monkeypatch.setattr(spaces, "push_settings", lambda path, data: real_push(path, data, shell))
    job = make_job(tmp_path)
    credentials = tmp_path / "credentials.toml"
    argv = ["campaign", "manager", "deploy", "--account", "leftclick", "--wheel", str(job.wheel)]
    argv += ["--requirements", str(job.requirements), "--networks", "shifter"]
    argv += ["--credentials-file", str(credentials), "--mac-key", str(job.mac_key)]
    return job, shell, [*argv, "--inventory", str(job.inventory)]


def spaces_writes(shell):
    return [stdin for argv, stdin in shell.calls if "spaces.toml" in argv[-1]]


async def test_the_spaces_settings_are_written_to_the_manager_after_the_deploy(
    tmp_path, deploy_world
):
    job, shell, argv = deploy_world
    settings = tmp_path / "spaces.toml"
    settings.write_text(SPACES)
    settings.chmod(0o600)
    args = parse([*argv, "--spaces-file", str(settings)])
    async with running(Fake()) as cloud:
        result = await campaign_cli._deploy(args, cloud, "198.51.100.7")
    assert result["manager_id"] == inventory(job)["manager_id"]
    assert spaces_writes(shell) == [spaces.manager_settings(settings)]
    assert "spaces.toml" in shell.calls[-1][0][-1]  # after every deploy step
    assert all("spaces-secret" not in " ".join(argv) for argv, _ in shell.calls)


async def test_without_a_spaces_file_no_settings_are_written(deploy_world):
    job, shell, argv = deploy_world
    async with running(Fake()) as cloud:
        result = await campaign_cli._deploy(parse(argv), cloud, "198.51.100.7")
    assert result["manager_id"] == inventory(job)["manager_id"] and shell.calls
    assert spaces_writes(shell) == []


def test_a_signal_during_shot_remove_names_the_rerun(tmp_path, monkeypatch, capsys):
    async def stopped(cloud, folder):  # SIGTERM: the signal handler cancels the main task
        asyncio.current_task().cancel()
        await asyncio.sleep(0)

    monkeypatch.setattr(shooting, "recover", stopped)
    monkeypatch.setattr("atlas.campaign.do_api.file_token", lambda path: lambda: "t")
    assert main_cli.main(["campaign", "shot", "--remove", str(tmp_path)]) == 3
    out, err = capsys.readouterr()
    assert out == "" and err == "Interrupted: run `campaign shot --remove` again\n"
