"""CAMP-1g: the manager's units, upload-spec, enable, disable and its CLI (fakes only)."""

from __future__ import annotations

import configparser
import io
import json
import shlex
import shutil
import subprocess
import tarfile

import pytest
from campaign_deploy_cases import planned_root
from campaign_deploy_fakes import (
    ASSETS,
    CID,
    MAC_IP,
    MANAGER_IP,
    NOW,
    OTHER_IP,
    Shell,
    make_job,
    parse,
)
from campaign_fakes import CAMPAIGN_PROJECT

from atlas.campaign import cli as campaign_cli
from atlas.campaign import deploy, teardown
from atlas.campaign.tick import timer_calendars


@pytest.fixture
def job(tmp_path):
    return make_job(tmp_path)


def units():
    parsed = {}
    for name in deploy.UNIT_FILES:
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        parser.read_string((ASSETS / "droplet" / name).read_text())
        parsed[name] = parser
    return parsed


def test_the_unit_files_render():
    found = units()
    tick = found["atlas-campaign-tick@.service"]
    timer = found["atlas-campaign-tick@.timer"]
    assert dict(timer["Timer"]) == {
        "OnCalendar": "*-*-* *:58:30",
        "AccuracySec": "1s",
        "Persistent": "true",
    }
    assert (tick["Service"]["Type"], tick["Service"]["Restart"]) == ("oneshot", "on-failure")
    assert tick["Service"]["TimeoutStartSec"] == "15min"
    collector = found["atlas-campaign-collector@.service"]["Service"]
    assert (collector["Restart"], collector["RestartPreventExitStatus"]) == ("always", "0")
    for name in ("tick@.service", "collector@.service"):
        assert found[f"atlas-campaign-{name}"]["Service"]["LimitNOFILE"] == "65536"
    for check, calendar in (("armed", "*-*-* *:00:30"), ("fired", "*-*-* *:02:00")):
        assert found[f"atlas-campaign-watchdog-{check}@.timer"]["Timer"]["OnCalendar"] == calendar
        exec_start = found[f"atlas-campaign-watchdog-{check}@.service"]["Service"]["ExecStart"]
        assert exec_start.endswith(f"--check {check}")
    for parser in found.values():
        if parser.has_section("Service"):
            argv = shlex.split(parser["Service"]["ExecStart"])
            assert argv[:2] == ["/opt/atlas-campaign/venv/bin/atlas", "campaign"]
            assert argv[argv.index("--root") + 1] == "/opt/atlas-campaign/campaigns/%i"


def test_the_unit_commands_exist_in_the_cli():
    pytest.importorskip(
        "atlas.campaign.tick",
        reason="CAMP-1d (tick, collector, watchdog) is not merged into this base yet",
    )
    for parser_unit in units().values():
        if parser_unit.has_section("Service"):
            argv = shlex.split(parser_unit["Service"]["ExecStart"].replace("%i", CID))
            parse(argv[1:])


def test_systemd_analyze_verifies_the_units(tmp_path):
    tool = shutil.which("systemd-analyze")
    if tool is None:
        pytest.skip("systemd-analyze is not available on this host (Linux only)")
    for name in deploy.UNIT_FILES:
        (tmp_path / name.replace("@.", f"@{CID}.")).write_text((ASSETS / "droplet" / name).read_text())
    found = subprocess.run(
        [tool, "verify", *sorted(map(str, tmp_path.iterdir()))], capture_output=True, text=True
    )
    assert "Unknown key" not in found.stderr and "Invalid" not in found.stderr


def test_unit_names_for_a_campaign():
    assert deploy.unit_names(CID) == [
        f"atlas-campaign-tick@{CID}.timer",
        f"atlas-campaign-collector@{CID}.service",
        f"atlas-campaign-watchdog-armed@{CID}.timer",
        f"atlas-campaign-watchdog-fired@{CID}.timer",
    ]
    with pytest.raises(ValueError):
        deploy.unit_names("../etc")


@pytest.fixture
def deployed(job):
    deploy.save(
        job.inventory,
        {},
        manager_id="500001",
        host=MANAGER_IP,
        user="root",
        identity="k",
        identity_file="k",
        known_hosts="kh",
        project_id=CAMPAIGN_PROJECT,
        manager_since=NOW.isoformat(),
        manager_firewall_id="fw-1",
        campaigns=[CID],
        wheel_sha256="ab" * 32,
        account="leftclick",
        specs={CID: {"starts_at": "2027-01-01T23:00:00+00:00", "synthetic": False}},
    )
    return job.inventory


async def test_enable_refuses_without_the_owners_go(deployed, tmp_path):
    shell = Shell()
    empty = tmp_path / "empty.md"
    empty.write_text(" \n")
    for go in (None, tmp_path / "missing.md", empty):
        with pytest.raises(deploy.DeployError, match="--owner-go"):
            await deploy.enable(deployed, CID, go, shell=shell, now=NOW)
    answer = tmp_path / "CAMP-1-go.md"
    answer.write_text("Owner: go.\nStart: 2027-01-01T23:00Z\nAccount: leftclick\n")
    with pytest.raises(deploy.DeployError, match="never uploaded"):
        await deploy.enable(deployed, "20270101t230000z-ffffffffffff", answer, shell=shell)
    assert shell.calls == [] and "enabled" not in json.loads(deployed.read_text())
    stale = tmp_path / "CAMP-1-plan-approval.md"
    for text in (
        "Owner: the plan is approved.\n",
        "start at 23:00 UTC, round HH:00:00\n",
        "T22:23:00Z and 2027-01-01T23:00Z\n",
        "Start: 2027-01-01T23:00Z, maybe\n",
    ):
        stale.write_text(text)  # m3, n2: only the exact start line counts
        with pytest.raises(deploy.DeployError, match="'Start: 2027-01-01T23:00Z'"):
            await deploy.enable(deployed, CID, stale, shell=shell, now=NOW)
    assert shell.calls == []
    result = await deploy.enable(deployed, CID, answer, shell=shell, now=NOW)
    assert shell.commands() == ["systemctl enable --now " + " ".join(deploy.unit_names(CID))]
    recorded = json.loads(deployed.read_text())["enabled"]
    assert recorded["owner_go"] == str(answer.resolve()) and len(recorded["owner_go_sha256"]) == 64
    assert result["units"] == deploy.unit_names(CID) and recorded["synthetic"] is False


async def test_a_synthetic_campaign_needs_a_go_but_no_start_hour(deployed, tmp_path):
    saved = json.loads(deployed.read_text())
    deploy.save(
        deployed, saved, specs={CID: {"starts_at": "2027-01-01T23:00:00+00:00", "synthetic": True}}
    )
    approval = tmp_path / "approval.md"
    approval.write_text("Owner: plan approved, including the scheduler proof.\n")
    result = await deploy.enable(deployed, CID, approval, shell=Shell(), now=NOW)
    assert result["synthetic"] is True


def test_the_enable_cli_refuses_without_owner_go(deployed, capsys):
    args = parse(["campaign", "manager", "enable", CID, "--inventory", str(deployed)])
    assert campaign_cli.dispatch(args) == 3
    assert "--owner-go" in capsys.readouterr().err


async def test_disable_stops_one_campaigns_units_and_records_it(deployed):
    shell = Shell()
    with pytest.raises(deploy.DeployError, match="never uploaded"):
        await deploy.disable(deployed, "20270101t230000z-ffffffffffff", shell=shell)
    assert shell.calls == []
    result = await deploy.disable(deployed, CID, shell=shell, now=NOW)
    assert shell.commands() == ["systemctl disable --now " + " ".join(deploy.unit_names(CID))]
    assert result["units"] == deploy.unit_names(CID)
    assert json.loads(deployed.read_text())["disabled"] == {CID: NOW.isoformat()}


async def test_upload_spec_sends_the_root_and_records_the_campaign(deployed, tmp_path):
    saved = json.loads(deployed.read_text())
    deploy.save(deployed, saved, campaigns=[])
    shell, root = Shell(), planned_root(tmp_path)
    assert await deploy.upload_spec(deployed, root, shell=shell) == {
        "campaign_id": CID,
        "campaigns": [CID],
    }
    [(argv, stdin)] = shell.calls
    assert argv[-1].startswith("umask 077 && test ! -e /opt/atlas-campaign/campaigns/" + CID)
    assert sorted(tarfile.open(fileobj=io.BytesIO(stdin)).getnames()) == [
        "campaign-spec.json",
        "networks/shifter/residential/m/campaign.json",
        "networks/shifter/residential/m/provider.toml",
    ]
    saved = json.loads(deployed.read_text())
    assert saved["campaign_id"] == CID and saved["specs"][CID] == {
        "starts_at": "2027-01-01T23:00:00+00:00",
        "synthetic": False,
    }
    deploy.save(deployed, saved, account="atlas")
    with pytest.raises(deploy.DeployError, match="account"):
        await deploy.upload_spec(deployed, planned_root(tmp_path / "c"), shell=shell)
    deploy.save(deployed, saved, account="leftclick")
    for other in (
        planned_root(tmp_path / "a", manager_id="9"),
        planned_root(tmp_path / "b", wheel="cd" * 32),
    ):
        with pytest.raises(deploy.DeployError, match="deployed manager, wheel and account"):
            await deploy.upload_spec(deployed, other, shell=shell)
    with pytest.raises(deploy.DeployError, match="exists or failed"):
        await deploy.upload_spec(deployed, root, shell=Shell(fail=("test ! -e",)))


async def test_upload_spec_refuses_a_spec_of_the_other_environment(deployed, tmp_path):
    """CAMP-STAGING-1: an inventory without `environment` is production's."""
    saved, shell = json.loads(deployed.read_text()), Shell()
    with pytest.raises(deploy.DeployError, match="deployed manager, wheel and account"):
        await deploy.upload_spec(
            deployed, planned_root(tmp_path / "s", environment="staging"), shell=shell
        )
    deploy.save(deployed, saved, environment="staging", campaigns=[])
    with pytest.raises(deploy.DeployError, match="deployed manager, wheel and account"):
        await deploy.upload_spec(deployed, planned_root(tmp_path / "p"), shell=shell)
    root = planned_root(tmp_path / "t", environment="staging")
    assert (await deploy.upload_spec(deployed, root, shell=shell))["campaigns"] == [CID]


class NoCloud:
    """Stands in for the client the CLI builds; records the account it was built for."""

    accounts: list = []

    def __init__(self, account):
        self.accounts.append(account)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


@pytest.mark.parametrize(
    "argv, called",
    [
        (["upload-spec", "root"], "upload_spec"),
        (["enable", CID, "--owner-go", "go.md"], "enable"),
        (["disable", CID], "disable"),
        (["allow-me", "--mac-ip", MAC_IP], "allow_me"),
        (["allow-me"], "allow_me"),
        (["teardown", "--after-collect", "r.json"], "teardown"),
        (
            [
                "deploy",
                "--account",
                "leftclick",
                "--wheel",
                "w",
                "--requirements",
                "r",
                "--networks",
                "shifter",
                "--mac-ip",
                MAC_IP,
            ],
            "deploy",
        ),
    ],
)
def test_the_manager_cli_routes_each_step(monkeypatch, capsys, tmp_path, argv, called):
    seen, recorded = [], tmp_path / "campaign-manager.json"
    recorded.write_text(json.dumps({"account": "atlas"}))

    def stub(name, result):
        async def step(*args, **_):
            seen.append((name, args))
            return result

        return step

    for name in ("upload_spec", "enable", "disable", "allow_me", "deploy"):
        monkeypatch.setattr(deploy, name, stub(name, {"clean": True}))
    monkeypatch.setattr(teardown, "teardown", stub("teardown", {"clean": False}))
    monkeypatch.setattr(deploy, "mac_ipv4", lambda: OTHER_IP)
    monkeypatch.setattr(deploy, "credential_files", lambda path, nets: {n: b"" for n in nets})
    monkeypatch.setattr("atlas.campaign.image.ensure_mac_key", lambda path: path)
    monkeypatch.setattr(campaign_cli, "_cloud", NoCloud)
    NoCloud.accounts = []
    args = parse(["campaign", "manager", *argv, "--inventory", str(recorded)])
    assert campaign_cli.dispatch(args) == (3 if called == "teardown" else 0)
    assert [name for name, _ in seen] == [called]
    if called in ("allow_me", "teardown"):  # B1: the inventory's account, never a flag
        assert NoCloud.accounts == ["atlas"]
    if called == "allow_me":
        assert seen[0][1][-1] == (MAC_IP if "--mac-ip" in argv else OTHER_IP)
    if called == "deploy":
        job = seen[0][1][1]
        assert job.mac_ipv4 == MAC_IP and list(job.credentials) == ["shifter"]
        assert job.size is None  # the recorded manager's size, else s-8vcpu-16gb
        assert NoCloud.accounts == ["leftclick"]
    assert json.loads(capsys.readouterr().out) == {"clean": called != "teardown"}


def test_the_manager_cli_needs_a_recorded_manager_for_teardown(tmp_path, capsys):
    args = parse(
        [
            "campaign",
            "manager",
            "teardown",
            "--after-collect",
            "r.json",
            "--inventory",
            str(tmp_path / "none.json"),
        ]
    )
    assert campaign_cli.dispatch(args) == 3
    assert "no manager is recorded" in capsys.readouterr().err


# CAMP-1n: a compressed test (4 slots of 15 minutes) moves the three timers to its slots.
def test_the_timer_calendars_match_the_unit_files_and_quarter_slots():
    found, hourly = units(), timer_calendars(60, 90)
    for name in ("tick", "watchdog-armed", "watchdog-fired"):
        assert found[f"atlas-campaign-{name}@.timer"]["Timer"]["OnCalendar"] == hourly[name]
    assert timer_calendars(15, 90) == {
        "tick": "*-*-* *:13,28,43,58:30",
        "watchdog-armed": "*-*-* *:00,15,30,45:30",
        "watchdog-fired": "*-*-* *:02,17,32,47:00",
    }
    assert timer_calendars(20, 90)["tick"] == "*-*-* *:18,38,58:30"
    assert deploy.timer_dropins(CID, {"starts_at": "x", "synthetic": False}) == []


async def test_enable_installs_quarter_slot_timer_dropins(deployed, tmp_path):
    saved = json.loads(deployed.read_text())
    deploy.save(deployed, saved, campaigns=[])
    root = planned_root(tmp_path, slot_minutes=15, hours=4)
    await deploy.upload_spec(deployed, root, shell=Shell())
    assert json.loads(deployed.read_text())["specs"][CID] == {
        "starts_at": "2027-01-01T23:00:00+00:00",
        "synthetic": False,
        "slot_minutes": 15,
        "create_lead_seconds": 90,
    }
    answer = tmp_path / "CAMP-1-go.md"
    answer.write_text("Start: 2027-01-01T23:00Z\n")
    shell = Shell()
    await deploy.enable(deployed, CID, answer, shell=shell, now=NOW)
    [(argv, stdin)] = shell.calls
    assert argv[-1] == (
        "umask 022 && tar -x --no-same-owner -C /etc/systemd/system && "
        "systemctl daemon-reload && systemctl enable --now " + " ".join(deploy.unit_names(CID))
    )
    archive = tarfile.open(fileobj=io.BytesIO(stdin))
    dropins = {m.name: archive.extractfile(m).read().decode() for m in archive.getmembers()}
    assert {m.mode for m in archive.getmembers()} == {0o644}
    calendars = timer_calendars(15, 90)
    assert dropins == {
        f"atlas-campaign-{name}@{CID}.timer.d/slots.conf": (
            f"# CAMP-1: campaign {CID} has a slot every 15 minutes.\n[Unit]\n"
            f"Description=Atlas campaign %i: {role} every 15 min at "
            f"{calendars[name].removeprefix('*-*-* ')}\n[Timer]\n"
            f"OnCalendar=\nOnCalendar={calendars[name]}\n"
        )
        for name, role in (
            ("tick", "launch"),
            ("watchdog-armed", "watchdog (armed)"),
            ("watchdog-fired", "watchdog (fired)"),
        )
    }
    assert (
        "launch every 15 min at *:13,28,43,58:30"
        in dropins[f"atlas-campaign-tick@{CID}.timer.d/slots.conf"]
    )
