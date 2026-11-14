"""IMAGE-AUTO-1: `campaign image build` and `campaign limits` on the Linux manager read the
DigitalOcean token from a 0600 file (`--token-file`, else ATLAS_DO_TOKEN_FILE), the Mac's
keychain otherwise; `--mac-key` defaults to ATLAS_MAC_KEY_FILE. No request leaves the test and
no token is printed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas import cli as main_cli
from atlas.campaign import cli as campaign_cli
from atlas.campaign import do_api, image
from atlas.campaign.contracts import ACCOUNTS
from atlas.campaign.do_api import TOKEN_FILE_ENV, TokenError, account_token

TOKEN = "dop_v1_" + "f" * 64


def token_file(path: Path, text: str = TOKEN, mode: int = 0o600) -> Path:
    path.write_text(text + "\n")
    path.chmod(mode)
    return path


@pytest.fixture
def keychain(monkeypatch):
    """The keychain answers its own token; records that it was asked."""
    asked = []

    def run(command, **_):
        asked.append(command[0])
        return type("Found", (), {"returncode": 0, "stdout": "keychain-token\n"})

    monkeypatch.setattr(do_api.subprocess, "run", run)
    monkeypatch.delenv(TOKEN_FILE_ENV, raising=False)
    return asked


def test_the_flag_names_the_token_file(tmp_path, keychain):
    source = account_token(ACCOUNTS["leftclick"], token_file(tmp_path / "do-token"))
    assert source() == TOKEN and keychain == []


def test_the_environment_names_the_token_file_and_the_flag_wins(tmp_path, keychain, monkeypatch):
    monkeypatch.setenv(TOKEN_FILE_ENV, str(token_file(tmp_path / "env-token", "env-token")))
    assert account_token(ACCOUNTS["leftclick"])() == "env-token"
    flag = token_file(tmp_path / "flag-token")
    assert account_token(ACCOUNTS["leftclick"], flag)() == TOKEN and keychain == []


def test_without_a_file_the_keychain_stays_the_default(keychain):
    assert account_token(ACCOUNTS["leftclick"])() == "keychain-token"
    assert keychain == ["security"]


def test_a_token_file_others_can_read_refuses_without_its_content(tmp_path, keychain):
    source = account_token(ACCOUNTS["leftclick"], token_file(tmp_path / "t", mode=0o644))
    with pytest.raises(TokenError, match="0600") as caught:
        source()
    assert TOKEN not in str(caught.value)


def test_limits_reads_the_token_file_and_prints_no_token(tmp_path, keychain, monkeypatch, capsys):
    seen = {}

    async def limits(cloud, project, sizes):
        seen["token"] = cloud._token_source()
        return {"droplet_limit": 1}

    monkeypatch.setattr(campaign_cli, "limits", limits)
    path = token_file(tmp_path / "do-token")
    argv = ["campaign", "limits", "--account", "leftclick", "--token-file", str(path)]
    assert main_cli.main(argv) == 0
    assert seen["token"] == TOKEN and keychain == []
    assert TOKEN not in capsys.readouterr().out


def test_image_build_takes_the_token_file_and_the_key_from_the_environment(
    tmp_path, keychain, monkeypatch, capsys
):
    key = tmp_path / "campaign-key"
    key.write_text("private\n")
    monkeypatch.setenv("ATLAS_MAC_KEY_FILE", str(key))
    monkeypatch.setenv(TOKEN_FILE_ENV, str(token_file(tmp_path / "do-token")))
    seen = {}

    async def fake_build(cloud, job):
        seen.update(token=cloud._token_source(), key=job.mac_key)
        return {"state": "built"}

    monkeypatch.setattr(image, "build", fake_build)
    argv = ["campaign", "image", "build", "--account", "leftclick"]
    argv += ["--wheel", str(tmp_path / "w.whl"), "--requirements", str(tmp_path / "r.txt")]
    assert main_cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {"state": "built"}
    assert seen == {"token": TOKEN, "key": key} and keychain == []
