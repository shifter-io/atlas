"""MANAGER-HOST-1: the manager on an existing server as a normal user (fakes only, no live call).

A host deploy makes no droplet, project or manager-firewall call, only the worker-firewall rule;
the user units carry each environment's names and ports; production and staging never share a
folder, unit, scope, port or private file; `orchestrator-enable --apply` needs the owner's go.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import replace

import pytest
from campaign_deploy_fakes import MANAGER_IP, WORKER_PUBLIC, Fake, inventory, make_job, parse
from campaign_fakes import TOKEN, running
from manager_orch_cases import CERT_PEM, Shell, formula_dir, go_inputs, installable, staged

from atlas.campaign import (
    browser_hosts,
    deploy,
    host_deploy,
    host_layout,
    manager_daily,
    manager_orch,
    teardown,
)
from atlas.campaign.host_layout import Layout
from atlas.orchestrator import jobs

CID = "20270101t230000z-0123456789ab"
DATABASE = b'[database]\nurl = "postgresql://atlas:db-secret@db/atlas"\n'


class HostShell(Shell):
    """Also answers the setup and install scripts like the server, and `ssh -G`, `git
    rev-parse` and `git push` like this Mac (`alias` names the server by its address)."""

    def __init__(self, alias_address=MANAGER_IP, push=0, held=False, **kwargs):
        super().__init__(**kwargs)
        self.alias_address, self.push, self.held = alias_address, push, held
        self.no_api_file = False

    async def __call__(self, argv, stdin):
        command = argv[-1]
        if argv[:2] == ["ssh", "-G"]:
            self.calls.append((argv, stdin))
            return 0, f"user deploy\nhostname {self.alias_address}\nport 2222\n"
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            self.calls.append((argv, stdin))
            return 0, "89abcdef0123\n"
        if argv[0] == "env":
            self.calls.append((argv, stdin))
            return self.push, ""
        if "database-api.toml" in command:
            self.calls.append((argv, stdin))
            return 0, "" if self.no_api_file else "present\n"
        if "merge-base --is-ancestor" in command:
            self.calls.append((argv, stdin))
            return 0, "held\n" if self.held else ""
        if "scripts/server-setup.sh" in command:
            self.calls.append((argv, stdin))
            return 0, "setup done\n"
        if "echo installed" in command:
            self.calls.append((argv, stdin))
            return 0, f"home /srv/atlas-user\nworker-key {WORKER_PUBLIC}\ninstalled\n"
        found = await super().__call__([*argv[:-1], command.replace(" --user ", " ")], stdin)
        self.calls[-1] = (argv, stdin)  # the script as sent
        return found


@pytest.fixture
def setup(tmp_path):
    script = tmp_path / "repo" / "scripts" / "server-setup.sh"
    (script.parent / "server-setup").mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\necho setup done\n")
    (script.parent / "server-setup" / "unit.service").write_text("[Unit]\n")
    return script


def host_job(tmp_path, environment="production"):
    job = make_job(tmp_path, account="proxy-index")
    inventory = tmp_path / "secrets" / f"atlas-{environment}.json"
    return replace(job, inventory=inventory, environment=environment, database=DATABASE)


async def host_deploy_run(fake, job, setup, shell=None, ssh_user="atlas"):
    target = host_deploy.HostTarget(MANAGER_IP, ssh_user, "0123abc", setup)
    async with running(fake) as cloud:
        return await host_deploy.deploy_host(cloud, job, target, shell=shell or HostShell())


async def test_a_host_deploy_makes_only_the_worker_firewall_rule(tmp_path, setup):
    fake, shell, job = Fake(), HostShell(), host_job(tmp_path, "staging")
    result = await host_deploy_run(fake, job, setup, shell)
    writes = [(m, p.rstrip("/0123456789")) for m, p, _, _ in fake.requests if m != "GET"]
    assert writes == [("POST", "/v2/account/keys"), ("POST", "/v2/tags"), ("POST", "/v2/firewalls")]
    assert not [p for _, p, _, _ in fake.requests if p.startswith("/v2/droplets")]
    assert not fake.droplets
    [firewall] = fake.resources["firewall"].values()
    assert firewall["name"] == "pi-camp-stg-worker-fw"
    assert firewall["inbound_rules"][0]["sources"]["addresses"] == [f"{MANAGER_IP}/32"]
    saved = inventory(job)
    assert {k: saved[k] for k in ("manager_kind", "host", "ssh_user", "environment")} == {
        "manager_kind": "host",
        "host": MANAGER_IP,
        "ssh_user": "atlas",
        "environment": "staging",
    }
    assert "manager_id" not in saved and "manager_firewall_id" not in saved
    assert saved["campaigns_root"] == "/srv/atlas-user/atlas/staging/campaigns"
    assert result["worker_firewall_id"] == saved["worker_firewall_id"]
    commands = shell.scripts()
    repo = '"$HOME/atlas/repo.git"'
    api_file = '"$HOME/.config/atlas/staging/database-api.toml"'
    assert commands[3] == f"test -f {api_file} && echo present || true"
    assert commands[4] == (
        f"git init -q --bare {repo} && if git --git-dir {repo} merge-base --is-ancestor "
        "0123abc refs/heads/main 2>/dev/null; then echo held; fi"
    )
    push = shell.calls[5][0]
    assert push[0] == "env" and push[1].startswith("GIT_SSH_COMMAND=ssh -o BatchMode=yes")
    assert push[2:] == ["git", "push", "--quiet", f"atlas@{MANAGER_IP}:atlas/repo.git"] + [
        "0123abc:refs/heads/main"
    ]
    assert "bash scripts/server-setup.sh staging --ref 0123abc" in commands[6]
    assert set(staged(shell.calls[6][1])) == {
        "scripts/server-setup.sh",
        "scripts/server-setup/unit.service",
    }
    remote = [argv for argv, _ in shell.calls if argv[0] == "ssh"]
    assert all(argv[-2] == f"atlas@{MANAGER_IP}" for argv in remote)
    assert not any("authorized_keys" in c or "sudo" in c or "root" in c for c in commands)
    targets = [c.split("&&")[2].strip() for c in commands[:3]]
    private = '"$HOME/.config/atlas/staging'
    assert targets == [
        f'cat > {private}/do-token".partial',
        f'cat > {private}/credentials/shifter.toml".partial',
        f'cat > {private}/database.toml".partial',
    ]


async def test_secrets_reach_the_server_only_on_stdin(tmp_path, setup, capsys):
    shell, job = HostShell(), host_job(tmp_path)
    result = await host_deploy_run(Fake(), job, setup, shell)
    for secret in (TOKEN.encode(), b"db-secret"):
        assert all(secret not in " ".join(argv).encode() for argv, _ in shell.calls)
        assert len([stdin for _, stdin in shell.calls if secret in stdin]) == 1
        assert secret not in job.inventory.read_bytes()
    assert TOKEN not in json.dumps(result) + capsys.readouterr().out


async def test_a_rerun_updates_the_same_host_and_its_firewall(tmp_path, setup):
    fake, job = Fake(), host_job(tmp_path)
    first = await host_deploy_run(fake, job, setup)
    second = await host_deploy_run(fake, job, setup)
    assert first["worker_firewall_id"] == second["worker_firewall_id"]
    assert len(fake.resources["firewall"]) == 1


async def test_an_ssh_alias_brings_its_own_port_user_and_key(tmp_path, setup):
    shell, job = HostShell(), host_job(tmp_path)
    target = host_deploy.HostTarget("atlas-server", None, None, setup)
    async with running(Fake()) as cloud:
        await host_deploy.deploy_host(cloud, job, target, shell=shell)
    saved = inventory(job)
    assert (saved["host"], saved["ssh_alias"], saved["ssh_user"]) == (
        MANAGER_IP,
        "atlas-server",
        "deploy",
    )
    remote = [argv for argv, _ in shell.calls if argv[0] == "ssh" and argv[1] != "-G"]
    assert remote and all(argv[-2] == "deploy@atlas-server" for argv in remote)
    assert all("-i" not in argv and "-p" not in argv for argv in remote)
    push = next(argv for argv, _ in shell.calls if argv[0] == "env")
    assert push[-2:] == ["deploy@atlas-server:atlas/repo.git", "89abcdef0123:refs/heads/main"]
    assert any("server-setup.sh production --ref 89abcdef0123" in c for c in shell.scripts())
    with pytest.raises(deploy.DeployError, match="by its public IPv4"):
        async with running(Fake()) as cloud:
            await host_deploy.deploy_host(
                cloud, host_job(tmp_path), target, shell=HostShell(alias_address="nowhere")
            )


async def test_an_address_takes_the_ssh_port_and_a_failed_push_stops(tmp_path, setup):
    shell, job = HostShell(push=1), host_job(tmp_path)
    target = host_deploy.HostTarget(MANAGER_IP, "atlas", "0123abc", setup, 2222)
    async with running(Fake()) as cloud:
        with pytest.raises(deploy.DeployError, match="repository failed"):
            await host_deploy.deploy_host(cloud, job, target, shell=shell)
    ssh = [argv for argv, _ in shell.calls if argv[0] == "ssh"]
    assert all(argv[argv.index("-p") + 1] == "2222" and "-i" in argv for argv in ssh)
    assert "-p 2222" in next(argv for argv, _ in shell.calls if argv[0] == "env")[1]
    assert not any("server-setup.sh" in c for c in shell.scripts())


async def test_a_commit_the_servers_main_holds_is_not_pushed(tmp_path, setup):
    """Staging may run ahead of production in the shared repository: deploying production's
    older commit pushes nothing (main never moves backwards) and runs the setup for it."""
    shell = HostShell(held=True)
    await host_deploy_run(Fake(), host_job(tmp_path), setup, shell)
    assert not [argv for argv, _ in shell.calls if argv[0] == "env"]
    assert any("server-setup.sh production --ref 0123abc" in c for c in shell.scripts())


async def test_a_missing_api_database_file_is_named_before_the_setup(tmp_path, setup):
    shell = HostShell()
    shell.no_api_file = True
    with pytest.raises(deploy.DeployError, match=r"~/.config/atlas/production/database-api.toml"):
        await host_deploy_run(Fake(), host_job(tmp_path), setup, shell)
    assert not any("server-setup.sh" in c or "git init" in c for c in shell.scripts())


async def test_a_droplet_deploy_refuses_a_host_inventory(tmp_path, setup):
    from campaign_deploy_fakes import run_deploy

    job = host_job(tmp_path)
    await host_deploy_run(Fake(), job, setup)
    with pytest.raises(deploy.DeployError, match="use --host"):
        await run_deploy(Fake(), job)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"ssh_user": "root"}, "non-root"),
        ({"host": "127.0.0.1"}, "public IPv4"),
        ({"host": "bad host; rm"}, "public IPv4 or an ssh alias"),
        ({"ref": "main; rm -rf ~"}, "commit hash"),
        ({"ssh_port": 70000}, "port number"),
    ],
)
async def test_a_bad_target_is_refused_before_any_call(tmp_path, setup, change, message):
    fake, target = Fake(), host_deploy.HostTarget(MANAGER_IP, "atlas", "0123abc", setup)
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match=message):
            await host_deploy.deploy_host(
                cloud, host_job(tmp_path), replace(target, **change), shell=HostShell()
            )
    assert not fake.requests


async def test_a_missing_setup_script_or_droplet_inventory_is_refused(tmp_path, setup):
    job = host_job(tmp_path)
    with pytest.raises(deploy.DeployError, match="is missing"):
        await host_deploy_run(Fake(), job, tmp_path / "absent.sh")
    deploy.save(job.inventory, {}, manager_id=7, host=MANAGER_IP)
    with pytest.raises(deploy.DeployError, match="droplet manager"):
        await host_deploy_run(Fake(), job, setup)
    deploy.save(job.inventory, {}, manager_kind="host", host="198.51.100.99")
    with pytest.raises(deploy.DeployError, match="another server"):
        await host_deploy_run(Fake(), job, setup)


def test_the_install_script_makes_private_folders_and_the_venv_once():
    script = host_deploy.install_script(Layout("staging"))
    assert 'chmod 700 "$HOME/.config/atlas" "$HOME/.config/atlas/staging"' in script
    assert 'if [ ! -x "$HOME/atlas/staging/venv/bin/' in script
    assert "--require-hashes" in script and "sudo" not in script and "/opt/" not in script
    with pytest.raises(deploy.DeployError, match="did not finish"):
        host_deploy._installed("home /srv/atlas-user\nfailed\n")


def test_production_and_staging_never_share_a_name():
    layouts = [Layout("production").names(), Layout("staging").names()]
    for key in layouts[0]:
        values = [v if isinstance(v, list) else [v] for v in (lay[key] for lay in layouts)]
        for one, other in itertools.product(*values):
            assert one != other and not str(one).startswith(f"{other}/"), key
    units = [Layout(env).unit_files() for env in ("production", "staging")]
    assert not {n for n, _, _ in units[0]} & {n for n, _, _ in units[1]}
    with pytest.raises(ValueError):
        Layout("qa")


@pytest.mark.parametrize(
    ("environment", "prefix", "ports"),
    [("production", "atlas", (8443, 8444)), ("staging", "atlas-staging", (9443, 9444))],
)
def test_the_unit_files_hold_the_environments_names_and_ports(environment, prefix, ports):
    units = dict((name, data.decode()) for name, data, _ in Layout(environment).unit_files())
    assert sorted(units) == sorted(f"{prefix}-{n}" for n in host_layout.ASSETS)
    orchestrator = units[f"{prefix}-orchestrator.service"]
    assert f"--port {ports[0]} " in orchestrator and f"--browser-port {ports[1]} " in orchestrator
    assert f"ATLAS_CALCULATE_SCOPE={prefix}-calculate" in orchestrator
    assert f"%h/.config/atlas/{environment}/database.toml" in orchestrator
    assert f"WorkingDirectory=%h/atlas/{environment}\n" in orchestrator
    assert f"WantedBy={prefix}-manager.target" in orchestrator
    hourly = units[f"{prefix}-hourly.service"]
    assert f"systemd-run --user --no-block --unit={prefix}-hourly-$$(date" in hourly
    assert not installable(units[f"{prefix}-hourly.timer"])
    for name, text in units.items():
        assert "@" not in text.replace("@.service", "") and "/opt/" not in text, name
        if name.endswith(".service"):
            for line in ("Restart=on-failure", "RestartSec=10", "StartLimitIntervalSec=600"):
                assert line in text, (name, line)


def test_the_calculation_runs_in_the_users_scope():
    argv = jobs.calculation_argv("db.toml", "m.toml", "8G", "atlas-staging-calculate")
    assert argv[:5] == [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--unit=atlas-staging-calculate",
    ]
    assert "MemoryMax=8G" in argv
    with pytest.raises(ValueError, match="atlas-calculate"):
        jobs.calculation_argv("db.toml", "m.toml", "8G", "other-scope")
    assert "--user" not in jobs.calculation_argv("db.toml", "m.toml", "8G")


async def deployed_host(tmp_path, setup, environment="production"):
    fake, job = Fake(), host_job(tmp_path, environment)
    await host_deploy_run(fake, job, setup)
    deploy.save(job.inventory, inventory(job), campaigns=[CID])
    return fake, job


async def test_orchestrator_enable_on_a_host_installs_user_units(tmp_path, setup):
    fake, job = await deployed_host(tmp_path, setup, "staging")
    sent = len(fake.requests)
    async with running(fake) as cloud:
        dry = await manager_orch.enable(cloud, job.inventory, go_inputs(tmp_path))
        with pytest.raises(deploy.DeployError, match="owner-go"):
            await manager_orch.enable(
                cloud, job.inventory, go_inputs(tmp_path, owner_go=None), apply=True
            )
        shell = HostShell()
        done = await manager_orch.enable(
            cloud, job.inventory, go_inputs(tmp_path), apply=True, shell=shell
        )
    assert dry["started"] == [
        "atlas-staging-manager.target",
        "atlas-staging-orchestrator.service",
        "atlas-staging-analyze.timer",
    ]
    assert (dry["node_api_port"], dry["browser_port"]) == (9443, 9444)
    assert dry["firewall_inbound"] == manager_orch.HOST_FIREWALL
    assert "$HOME/atlas/staging/venv/bin/" in dry["seed_targets"]
    assert all(m == "GET" for m, *_ in fake.requests[sent:])  # no firewall, no droplet change
    [script] = shell.scripts()
    assert "systemctl --user enable atlas-staging-manager.target" in script
    assert f"https://{MANAGER_IP}:9443/health" in script
    assert "/etc/" not in script and "/opt/" not in script and "/root/" not in script
    assert "atlas-staging-hourly.timer" in script and "systemctl --user start" not in script
    assert set(staged(shell.calls[0][1])) >= {"units/atlas-staging-orchestrator.service"}
    saved = inventory(job)
    assert saved["orchestrator"]["orchestrator_url"] == f"https://{MANAGER_IP}:9443"
    assert saved["browser_port"] == 9444 and done["cert_sha256"]
    assert job.inventory.with_name(f"{job.inventory.stem}.orchestrator.crt").read_text() == CERT_PEM


async def test_the_other_steps_work_or_refuse_on_a_host(tmp_path, setup):
    fake, job = await deployed_host(tmp_path, setup)
    deploy.save(job.inventory, inventory(job), orchestrator={"at": "x"}, daily_template=CID)
    shell = HostShell()
    on = await manager_daily.hourly_enable(
        job.inventory, apply=True, owner_go=go_inputs(tmp_path).owner_go, shell=shell
    )
    off = await manager_daily.hourly_disable(job.inventory, apply=True, shell=shell)
    assert on["timer"] == off["timer"] == "atlas-hourly.timer"
    assert "systemctl --user add-wants timers.target atlas-hourly.timer" in shell.scripts()[0]
    assert "$HOME/.config/systemd/user/timers.target.wants/atlas-hourly.timer" in shell.scripts()[1]
    async with running(fake) as cloud:
        stopped = await manager_orch.disable(cloud, job.inventory, apply=True, shell=shell)
        with pytest.raises(deploy.DeployError, match="no droplet to resize"):
            await manager_orch.resize(cloud, job.inventory, apply=True)
        with pytest.raises(deploy.DeployError, match="droplet managers only"):
            await teardown.teardown(cloud, job.inventory, [])
    assert stopped["stopped"][0] == "atlas-orchestrator.service"
    assert "systemctl --user disable --now atlas-manager.target" in shell.scripts()[-1]
    assert host_deploy.allow_me()["firewall"].startswith("the hosting provider's panel")
    deploy.save(job.inventory, inventory(job), browser_port=8444)
    cert_shell = HostShell()
    with pytest.raises(deploy.DeployError, match="one key and one certificate"):
        await browser_hosts.issue(job.inventory, "browser-1", tmp_path / "out", shell=cert_shell)
    assert "$HOME/.config/atlas/production/tls/browser-ca.key" in cert_shell.scripts()[0]


def test_a_host_inventory_names_its_manager_for_campaign_specs():
    host = {"manager_kind": "host", "host": MANAGER_IP, "environment": "staging"}
    assert host_layout.manager_ref(host) == "host:staging"
    assert host_layout.manager_ref({"manager_id": 42}) == "42"
    assert host_layout.manager_ref({"manager_kind": "host"}) is None
    assert host_layout.manager_ref({}) is None


async def test_the_droplet_deploy_streams_the_database_file(tmp_path):
    from campaign_deploy_fakes import Shell as DropletShell
    from campaign_deploy_fakes import run_deploy

    shell, job = DropletShell(), replace(make_job(tmp_path), database=DATABASE)
    await run_deploy(Fake(), job, shell)
    last = shell.commands()[-1]
    assert last.startswith('umask 077 && mkdir -p -m 700 "$(dirname /etc/')
    assert "database.toml.partial" in last and shell.calls[-1][1] == DATABASE


def test_the_cli_takes_the_host_options(tmp_path):
    args = parse(
        [
            "campaign",
            "manager",
            "deploy",
            "--account",
            "atlas",
            "--wheel",
            "w.whl",
            "--requirements",
            "r.txt",
            "--host",
            MANAGER_IP,
            "--ssh-user",
            "atlas",
            "--ssh-port",
            "2222",
            "--environment",
            "staging",
            "--database-file",
            str(tmp_path / "database.toml"),
        ]
    )
    assert (args.host, args.ssh_user, args.environment) == (MANAGER_IP, "atlas", "staging")
    assert args.setup_script == host_deploy.SETUP and args.ref is None and args.ssh_port == 2222
    assert formula_dir(tmp_path).is_dir()
