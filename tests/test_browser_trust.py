"""SEC-MINI-KEYS-1: `campaign manager browser-trust` installs the Mac's browser CA and its
revocation list on the manager (fake SSH), keeps the CA the manager made until every host is
switched, deletes that CA's key only after the orchestrator answers again and puts the previous
files back when it does not (the real script, with stand-in systemctl, curl and sleep); and
`orchestrator-enable` refuses a manager that still holds a CA key."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime

import pytest
from campaign_deploy_fakes import MANAGER_IP, inventory, make_job, parse
from campaign_fakes import running
from helpers import client_ca
from manager_orch_cases import deployed, go_inputs, recorded_size, staged

from atlas.campaign import browser_ca, browser_trust, deploy, manager_orch
from atlas.campaign import cli as campaign_cli
from atlas.errors import UserError

NOW = datetime(2026, 11, 16, 8, tzinfo=UTC)


def recorded(tmp_path):
    path = tmp_path / "campaign-manager.json"
    path.write_text(
        json.dumps(
            {
                "account": "atlas",
                "manager_id": 1,
                "host": MANAGER_IP,
                "identity": str(tmp_path / "mac-key"),
                "known_hosts": "campaign-manager.known_hosts",
            }
        )
    )
    return path


@pytest.fixture
def ca_dir(tmp_path):
    folder = tmp_path / "ca"
    browser_ca.make_ca(folder, NOW)
    return folder


class Shell:
    def __init__(self, code=0, out="trusted\n"):
        self.calls, self.code, self.out = [], code, out

    async def __call__(self, argv, stdin):
        self.calls.append((argv, stdin))
        return self.code, self.out


async def test_the_dry_run_checks_the_ca_and_changes_nothing(tmp_path, ca_dir):
    path, shell = recorded(tmp_path), Shell()
    before = path.read_text()
    result = await browser_trust.trust(ca_dir, path, keep_old=True, shell=shell)
    assert result["applied"] is False and result["old_ca"] is True
    assert result["revocation_checked"] is False
    assert result["restarts"] == manager_orch.ORCHESTRATOR_UNIT
    assert shell.calls == [] and path.read_text() == before


async def test_apply_sends_the_certificate_and_list_and_records_them(tmp_path, ca_dir):
    path, shell = recorded(tmp_path), Shell()
    result = await browser_trust.trust(ca_dir, path, apply=True, shell=shell, now=NOW)
    ((argv, stdin),) = shell.calls
    assert argv[-1] == browser_trust.trust_script(MANAGER_IP, 8443, keep_old=False)
    sent = staged(stdin)
    assert sent == {name: (ca_dir / name).read_bytes() for name in ("ca.crt", "ca.crl")}
    assert all(b"PRIVATE" not in data for data in sent.values())
    record = json.loads(path.read_text())["browser_trust"]
    assert record == {
        "ca_sha256": result["ca_sha256"],
        "crl_sha256": result["crl_sha256"],
        "old_ca": False,
        "at": NOW.isoformat(),
    }


async def test_a_failed_or_undone_run_records_nothing(tmp_path, ca_dir):
    path = recorded(tmp_path)
    before = path.read_text()
    cases = [
        (Shell(4, "rolled-back\n"), "the previous files are back"),
        (Shell(1, ""), r"manager step failed: browser-trust \(exit 1\)"),
        (Shell(0, "something else\n"), r"manager step failed: browser-trust \(exit 0\)"),
    ]
    for shell, message in cases:
        with pytest.raises(deploy.DeployError, match=message):
            await browser_trust.trust(ca_dir, path, apply=True, shell=shell)
    assert path.read_text() == before


async def test_a_ca_that_would_not_load_is_never_sent(tmp_path, ca_dir):
    path, shell = recorded(tmp_path), Shell()
    with pytest.raises(UserError, match="no usable browser CA"):
        await browser_trust.trust(tmp_path / "none", path, apply=True, shell=shell)
    (ca_dir / "ca.crl").write_bytes((ca_dir / "ca.crt").read_bytes())
    with pytest.raises(UserError, match="ca.crl its revocation list"):
        await browser_trust.trust(ca_dir, path, apply=True, shell=shell)
    (ca_dir / "ca.crl").write_text("-----BEGIN X509 CRL-----\nbroken\n-----END X509 CRL-----\n")
    with pytest.raises(UserError, match="cannot be loaded"):
        await browser_trust.trust(ca_dir, path, apply=True, shell=shell)
    assert shell.calls == []


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """The manager's TLS folder on this machine with the CA it made (a certificate and a key
    stand-in), and stand-in systemctl, curl and sleep: curl answers while `healthy` exists."""
    tls, bin_dir = tmp_path / "tls", tmp_path / "bin"
    tls.mkdir(mode=0o700)
    bin_dir.mkdir()
    for name, value in {
        "TLS_DIR": tls,
        "CERT": tls / "orchestrator.crt",
        "BROWSER_CA": tls / "browser-ca.crt",
        "BROWSER_CA_KEY": tls / "browser-ca.key",
        "BROWSER_CRL": tls / "browser-ca.crl",
    }.items():
        monkeypatch.setattr(manager_orch, name, str(value))
    monkeypatch.setattr(browser_trust, "RETIRED_CA", str(tls / "browser-ca-retired.crt"))
    monkeypatch.setattr(browser_trust, "WORK", str(tmp_path / "trust-XXXXXX"))
    monkeypatch.setattr(browser_trust, "HEALTH_TRIES", 2)
    old, _ = client_ca(tmp_path, "made-on-the-manager")
    (tls / "browser-ca.crt").write_bytes(old.read_bytes())
    (tls / "browser-ca.key").write_text("the manager's CA key stand-in\n")
    scripts = {
        "systemctl": 'echo "$@" >> "$LOG"; exit 0',
        "curl": '[ -e "$HEALTHY" ]',
        "sleep": "exit 0",
    }
    for name, body in scripts.items():
        (bin_dir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / name).chmod(0o755)
    healthy = tmp_path / "healthy"
    healthy.touch()
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LOG": str(tmp_path / "systemctl.log"),
        "HEALTHY": str(healthy),
    }
    return tls, old.read_bytes(), healthy, env


async def run_script(manager, ca_dir, keep_old):
    _, _, _, env = manager
    stdin = deploy._tar(
        [(name, (ca_dir / name).read_bytes(), 0o600) for name in ("ca.crt", "ca.crl")]
    )
    script = browser_trust.trust_script(MANAGER_IP, 8443, keep_old)
    process = await asyncio.create_subprocess_exec(
        "sh", "-c", script, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env=env
    )
    out, _ = await process.communicate(stdin)
    return process.returncode, out.decode().split()


async def test_the_switch_keeps_the_old_ca_then_drops_it(tmp_path, ca_dir, manager):
    tls, old, healthy, _ = manager
    new, crl = (ca_dir / "ca.crt").read_bytes(), (ca_dir / "ca.crl").read_bytes()
    assert await run_script(manager, ca_dir, keep_old=True) == (0, ["trusted"])
    assert (tls / "browser-ca.crt").read_bytes() == new + old  # unswitched hosts still pass
    assert (tls / "browser-ca.crl").read_bytes() == b""  # the old CA has no list
    assert (tls / "browser-ca-retired.crt").read_bytes() == old
    assert (tls / "browser-ca.crt.prev").read_bytes() == old
    assert not (tls / "browser-ca.key").exists()  # the key left the server
    assert oct((tls / "browser-ca.crt").stat().st_mode & 0o777) == "0o600"
    assert "restart" in (tmp_path / "systemctl.log").read_text()
    assert await run_script(manager, ca_dir, keep_old=True) == (0, ["trusted"])  # a rerun
    assert (tls / "browser-ca.crt").read_bytes() == new + old

    assert await run_script(manager, ca_dir, keep_old=False) == (0, ["trusted"])
    assert (tls / "browser-ca.crt").read_bytes() == new
    assert (tls / "browser-ca.crl").read_bytes() == crl  # revocation is checked from now on
    assert (tls / "browser-ca-retired.crt").read_bytes() == old  # kept: --keep-old-ca undoes
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("trust-")) == []


async def test_an_orchestrator_that_does_not_answer_gets_the_old_files_back(
    tmp_path, ca_dir, manager
):
    tls, old, healthy, _ = manager
    healthy.unlink()
    assert await run_script(manager, ca_dir, keep_old=True) == (4, ["rolled-back"])
    assert (tls / "browser-ca.crt").read_bytes() == old
    assert not (tls / "browser-ca.crl").exists()  # there was none before
    assert (tls / "browser-ca.key").exists()  # kept until the switch works
    assert (tmp_path / "systemctl.log").read_text().count("restart") == 2


async def test_a_new_manager_gets_the_ca_without_a_restart(tmp_path, ca_dir, manager):
    tls, _, _, env = manager
    (tmp_path / "bin" / "systemctl").write_text('#!/bin/sh\necho "$@" >> "$LOG"\nexit 3\n')
    for name in ("browser-ca.crt", "browser-ca.key"):
        (tls / name).unlink()
    assert await run_script(manager, ca_dir, keep_old=True) == (1, [])  # nothing old to keep
    assert await run_script(manager, ca_dir, keep_old=False) == (0, ["trusted"])
    assert (tls / "browser-ca.crt").read_bytes() == (ca_dir / "ca.crt").read_bytes()
    assert "restart" not in (tmp_path / "systemctl.log").read_text()


async def test_enable_refuses_a_manager_holding_a_ca_key(tmp_path):
    job = make_job(tmp_path, account="atlas")
    fake = await deployed(job)
    await recorded_size(fake, job)
    for line, message in (
        ("browser-ca-key-on-server", "a browser CA key is on the manager: run browser-trust"),
        ("browser-trust-missing", "no browser CA or revocation list on the manager"),
    ):
        shell = Shell(3, f"{line}\n")
        async with running(fake) as cloud:
            with pytest.raises(deploy.DeployError, match=message):
                await manager_orch.enable(
                    cloud, job.inventory, go_inputs(tmp_path), apply=True, shell=shell
                )
        assert "orchestrator" not in inventory(job)


async def test_the_cli_passes_the_flags(tmp_path, monkeypatch):
    seen = []

    async def trust(ca_dir, inventory_path, *, keep_old, apply):
        seen.append((ca_dir, inventory_path, keep_old, apply))
        return {}

    monkeypatch.setattr(browser_trust, "trust", trust)
    argv = ["--ca-dir", "ca", "--inventory", "inv.json"]
    for extra, flags in (([], (False, False)), (["--keep-old-ca", "--apply"], (True, True))):
        args = parse(["campaign", "manager", "browser-trust", *argv, *extra])
        assert await campaign_cli._manager_step(args) == {}
        assert seen[-1][2:] == flags
    with pytest.raises(SystemExit):
        parse(["campaign", "manager", "browser-cert", "--host", "browser-1", "--out", "x"])
