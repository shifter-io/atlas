"""MANAGER-ORCH-1: the manager's size check, the orchestrator units and firewall rule (fakes
only, no live calls), HTTPS serving and the calculation as a subprocess."""

from __future__ import annotations

import asyncio
import io
import json
import os
import ssl
import sys
import tarfile
import time

import aiohttp
import pytest
import trustme
from aiohttp import web
from campaign_deploy_fakes import (
    MANAGER_IP,
    OTHER_IP,
    Fake,
    fast_assign,
    inventory,
    make_job,
    parse,
    run_deploy,
    writes,
)
from campaign_fakes import running

from proxy_index.campaign import cli as campaign_cli
from proxy_index.campaign import deploy, manager_orch, ops_api
from proxy_index.campaign.contracts import PULSE_TAG
from proxy_index.orchestrator import app as orchestrator_app
from proxy_index.orchestrator import cli as orchestrator_cli
from proxy_index.orchestrator import jobs
from proxy_index.orchestrator.db import Database, put_setting
from proxy_index.orchestrator.scheduler import Scheduler
from proxy_index.storage import seed

pytest_plugins = ["test_storage"]
WANTED = manager_orch.WANTED_SIZE
CERT_PEM = "-----BEGIN CERTIFICATE-----\nMIIBfakeCERTbody0123456789+/=\n-----END CERTIFICATE-----\n"


@pytest.fixture(autouse=True)
def quick_assign(monkeypatch):
    fast_assign(monkeypatch)


@pytest.fixture
def job(tmp_path):
    return make_job(tmp_path)


class Shell:
    """Fake SSH: records argv and stdin; the enable script prints the certificate and `healthy`."""

    def __init__(self, healthy=True):
        self.calls, self.healthy = [], healthy

    async def __call__(self, argv, stdin):
        self.calls.append((argv, stdin))
        if "systemctl enable" in argv[-1]:
            return 0, "Created symlink\n" + CERT_PEM + ("healthy\n" if self.healthy else "")
        return 0, ""


async def deployed(job, size=WANTED, pulse_tag=True):
    """A deployed manager in the fake, live at `size`, with its size recorded as at creation."""
    fake = Fake()
    fake.sizes.append({"slug": WANTED, "available": True, "regions": ["lon1"]})
    await run_deploy(fake, job)
    fake.droplets[str(inventory(job)["manager_id"])]["size_slug"] = size
    if pulse_tag:
        fake.tags.add(PULSE_TAG)
    return fake


def manager_firewall(fake, job):
    return fake.resources["firewall"][inventory(job)["manager_firewall_id"]]


def owner_go(tmp_path):
    path = tmp_path / "owner-go.md"
    path.write_text("Go: switch the orchestrator to the manager.\n")
    return path


# --- units --------------------------------------------------------------------------------------


def test_the_units_are_rendered_from_the_assets():
    found = {name: data.decode() for name, data, mode in manager_orch.units() if mode == 0o644}
    assert sorted(found) == sorted(manager_orch.UNIT_FILES)
    service = found[manager_orch.ORCHESTRATOR_UNIT]
    assert "MemoryMax=12G" in service
    assert "Environment=PROXY_INDEX_CALCULATE_MEMORY_MAX=8G" in service
    assert f"EnvironmentFile={manager_orch.ENV_FILE}" in service
    assert f"Environment=DBIP_MMDB={manager_orch.DBIP_DIR}/dbip-full.mmdb" in service
    assert f"WorkingDirectory={deploy.BASE}" in service
    start = next(line for line in service.splitlines() if line.startswith("ExecStart="))
    assert start.split() == [
        f"ExecStart={deploy.BASE}/venv/bin/proxy-index",
        "orchestrator",
        "--host",
        "0.0.0.0",
        "--port",
        str(manager_orch.NODE_API_PORT),
        "--database-file",
        manager_orch.DATABASE_FILE,
        "--tls-cert",
        manager_orch.CERT,
        "--tls-key",
        manager_orch.KEY,
    ]
    analyze = found["proxy-index-db-analyze.service"]
    assert "Type=oneshot" in analyze
    assert f"db-server analyze --database-file {manager_orch.DATABASE_FILE}" in analyze
    timer = found[manager_orch.ANALYZE_TIMER]
    assert "OnCalendar=*-*-* 03:47:00 UTC" in timer and "Persistent=true" in timer


def test_the_firewall_keeps_its_rules_and_adds_the_node_api_rule():
    recorded = {"manager_id": "42"}
    assert manager_orch.manager_rules(recorded, OTHER_IP) == ops_api.manager_firewall(
        OTHER_IP, "42"
    )
    opened = manager_orch.manager_rules(recorded | {"node_api_port": 8443}, OTHER_IP)
    assert opened["inbound_rules"] == [
        ops_api.ssh_from(OTHER_IP),
        {"protocol": "tcp", "ports": "8443", "sources": {"tags": [PULSE_TAG]}},
    ]
    assert opened["outbound_rules"] == ops_api.outbound_all()


def test_the_enable_script_reads_the_database_file_in_place():
    script = manager_orch.enable_script(MANAGER_IP)
    assert f"subjectAltName=IP:{MANAGER_IP}" in script
    assert f"https://{MANAGER_IP}:8443/health" in script and "--connect-to ::127.0.0.1:" in script
    assert script.splitlines()[-2:] == [f"cat {manager_orch.CERT}", "echo healthy"]
    uses = [line for line in script.splitlines() if manager_orch.DATABASE_FILE in line]
    assert all(line.startswith("test ") for line in uses) and len(uses) == 2  # never read
    with pytest.raises(ValueError):
        manager_orch.enable_script("203.0.113.10; rm -rf /")


# --- resize -------------------------------------------------------------------------------------


async def test_resize_records_the_live_size_and_its_dry_run_changes_nothing(job):
    fake = await deployed(job)
    before = inventory(job)
    assert before["manager_size"] == deploy.MANAGER_SIZE
    async with running(fake) as cloud:
        dry = await manager_orch.resize(cloud, job.inventory)
        assert dry == {
            "manager_size": WANTED,
            "recorded_size": deploy.MANAGER_SIZE,
            "applied": False,
            "action": "record",
        }
        assert inventory(job) == before
        writes_before = writes(fake)
        done = await manager_orch.resize(cloud, job.inventory, apply=True)
        assert done["action"] == "record" and inventory(job)["manager_size"] == WANTED
        again = await manager_orch.resize(cloud, job.inventory, apply=True)
    assert again["action"] == "none" and writes(fake) == writes_before  # no cloud write


@pytest.mark.parametrize("size", ["s-99vcpu-1gb", "c-2-unknown"])
async def test_resize_refuses_an_unknown_size(job, size):
    fake = await deployed(job)
    before = inventory(job)
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match=f"size {size} is not offered in lon1"):
            await manager_orch.resize(cloud, job.inventory, size, apply=True)
    assert inventory(job) == before


async def test_resize_never_resizes_a_manager_of_another_size(job):
    fake = await deployed(job, size="s-1vcpu-2gb")
    before = inventory(job)
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match="the manager is s-1vcpu-2gb, not"):
            await manager_orch.resize(cloud, job.inventory, apply=True)
        del fake.droplets[str(before["manager_id"])]
        with pytest.raises(deploy.DeployError, match="the recorded manager is gone"):
            await manager_orch.resize(cloud, job.inventory, apply=True)
    assert inventory(job) == before


async def test_steps_need_a_recorded_manager(tmp_path):
    async with running(Fake()) as cloud:
        for step in (manager_orch.resize, manager_orch.enable, manager_orch.disable):
            with pytest.raises(deploy.DeployError, match="no manager is recorded"):
                await step(cloud, tmp_path / "none.json")


# --- enable / disable ---------------------------------------------------------------------------


async def recorded_size(fake, job):
    async with running(fake) as cloud:
        await manager_orch.resize(cloud, job.inventory, apply=True)


async def test_enable_dry_run_changes_nothing(job):
    fake = await deployed(job)
    await recorded_size(fake, job)
    before, writes_before, shell = inventory(job), writes(fake), Shell()
    async with running(fake) as cloud:
        result = await manager_orch.enable(cloud, job.inventory, shell=shell)
    assert result["applied"] is False and result["node_api_port"] == 8443
    assert result["firewall_inbound"][-1]["sources"] == {"tags": [PULSE_TAG]}
    assert sorted(result["units"]) == sorted(manager_orch.UNIT_FILES)
    assert inventory(job) == before and writes(fake) == writes_before and shell.calls == []


async def test_enable_needs_the_recorded_size_and_the_owner_go(job, tmp_path):
    fake = await deployed(job)
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match="record the manager's size first"):
            await manager_orch.enable(cloud, job.inventory, apply=True, shell=Shell())
    await recorded_size(fake, job)
    empty = tmp_path / "empty.md"
    empty.write_text("\n")
    before, writes_before = inventory(job), writes(fake)
    async with running(fake) as cloud:
        for go in (None, empty, tmp_path / "missing.md"):
            with pytest.raises(deploy.DeployError, match="--owner-go"):
                await manager_orch.enable(cloud, job.inventory, apply=True, owner_go=go)
    assert inventory(job) == before and writes(fake) == writes_before


async def test_enable_opens_the_port_installs_and_starts_the_units(job, tmp_path):
    fake = await deployed(job)
    await recorded_size(fake, job)
    shell = Shell()
    async with running(fake) as cloud:
        result = await manager_orch.enable(
            cloud, job.inventory, apply=True, owner_go=owner_go(tmp_path), shell=shell
        )
    rules = manager_firewall(fake, job)
    assert rules["inbound_rules"][-1] == manager_orch.node_api_rule()
    assert rules["droplet_ids"] == [int(inventory(job)["manager_id"])]
    ((argv, stdin),) = shell.calls
    assert argv[-1] == manager_orch.enable_script(MANAGER_IP)
    with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
        installed = {m.name: (archive.extractfile(m).read(), m.mode) for m in archive}
    assert installed == {name: (data, mode) for name, data, mode in manager_orch.units()}
    saved = inventory(job)
    cert = job.inventory.with_name("campaign-manager.orchestrator.crt")
    assert cert.read_text() == CERT_PEM and result["cert"] == cert.name
    assert saved["node_api_port"] == 8443
    record = saved["orchestrator"]
    assert record["orchestrator_url"] == f"https://{MANAGER_IP}:8443"
    assert record["cert_sha256"] == result["cert_sha256"] and record["owner_go_sha256"]
    assert MANAGER_IP not in json.dumps(result)  # the address stays in the inventory
    # allow-me (a new Mac address) keeps the node API rule.
    async with running(fake) as cloud:
        await deploy.allow_me(cloud, job.inventory, OTHER_IP)
    assert manager_firewall(fake, job)["inbound_rules"] == [
        ops_api.ssh_from(OTHER_IP),
        manager_orch.node_api_rule(),
    ]


async def test_enable_fails_when_the_health_check_does_not_pass(job, tmp_path):
    fake = await deployed(job)
    await recorded_size(fake, job)
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match="did not answer /health"):
            await manager_orch.enable(
                cloud, job.inventory, apply=True, owner_go=owner_go(tmp_path), shell=Shell(False)
            )
    assert "orchestrator" not in inventory(job)
    assert not job.inventory.with_name("campaign-manager.orchestrator.crt").exists()


async def test_a_missing_pulse_tag_is_named_and_nothing_is_recorded(job, tmp_path):
    fake = await deployed(job, pulse_tag=False)
    await recorded_size(fake, job)
    shell = Shell()
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match=f"does the tag {PULSE_TAG} exist"):
            await manager_orch.enable(
                cloud, job.inventory, apply=True, owner_go=owner_go(tmp_path), shell=shell
            )
    assert "node_api_port" not in inventory(job) and shell.calls == []
    assert len(manager_firewall(fake, job)["inbound_rules"]) == 1


async def test_disable_stops_the_units_then_closes_the_port(job, tmp_path):
    fake = await deployed(job)
    await recorded_size(fake, job)
    async with running(fake) as cloud:
        await manager_orch.enable(
            cloud, job.inventory, apply=True, owner_go=owner_go(tmp_path), shell=Shell()
        )
        shell, before = Shell(), inventory(job)
        dry = await manager_orch.disable(cloud, job.inventory, shell=shell)
        assert dry["firewall_inbound"] == [ops_api.ssh_from(before["mac_ipv4"])]
        assert inventory(job) == before and shell.calls == []
        await manager_orch.disable(cloud, job.inventory, apply=True, shell=shell)
    assert shell.calls[0][0][-1] == (
        "systemctl disable --now proxy-index-orchestrator.service proxy-index-db-analyze.timer"
    )
    assert manager_firewall(fake, job)["inbound_rules"] == [ops_api.ssh_from(before["mac_ipv4"])]
    saved = inventory(job)
    assert "node_api_port" not in saved and saved["orchestrator_disabled_at"]


async def test_deploy_keeps_the_node_api_rule(job):
    fake = await deployed(job)
    deploy.save(job.inventory, inventory(job), node_api_port=8443)
    await run_deploy(fake, job)
    assert manager_firewall(fake, job)["inbound_rules"][-1] == manager_orch.node_api_rule()


# --- CLI ----------------------------------------------------------------------------------------


def test_the_cli_parses_the_new_manager_steps(tmp_path):
    args = parse(["campaign", "manager", "resize"])
    assert (args.manager_action, args.size, args.apply) == ("resize", WANTED, False)
    go = tmp_path / "go.md"
    args = parse(["campaign", "manager", "orchestrator-enable", "--owner-go", str(go), "--apply"])
    assert (args.owner_go, args.apply) == (go, True)
    args = parse(["campaign", "manager", "orchestrator-disable", "--inventory", str(go)])
    assert (args.manager_action, args.inventory, args.apply) == ("orchestrator-disable", go, False)


async def test_the_cli_runs_each_step_on_the_recorded_account(job, monkeypatch):
    fake = await deployed(job)
    accounts = []

    def cloud_for(account):
        accounts.append(account)
        return running(fake)

    monkeypatch.setattr(campaign_cli, "_cloud", cloud_for)
    for argv in (
        ["resize", "--apply"],
        ["orchestrator-enable"],
        ["orchestrator-disable"],
    ):
        args = parse(["campaign", "manager", *argv, "--inventory", str(job.inventory)])
        result = await campaign_cli._manager_step(args)
        assert result["applied"] is ("--apply" in argv)
    assert accounts == ["leftclick"] * 3
    assert inventory(job)["manager_size"] == WANTED
    with pytest.raises(deploy.DeployError, match="no manager is recorded"):
        await manager_orch.run(
            parse(["campaign", "manager", "resize", "--inventory", str(job.inventory) + "x"]),
            cloud_for,
        )


# --- HTTPS --------------------------------------------------------------------------------------


async def test_the_orchestrator_serves_https_with_its_certificate(tmp_path):
    ca = trustme.CA()
    issued = ca.issue_cert("127.0.0.1")
    cert, key = tmp_path / "server.crt", tmp_path / "server.key"
    issued.cert_chain_pems[0].write_to_path(cert)
    issued.private_key_pem.write_to_path(key)
    context = orchestrator_app.server_tls(str(cert), str(key))
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    app = web.Application()

    async def health(_request):
        return web.json_response({"ok": True})

    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    trusted = ssl.create_default_context()
    ca.configure_trust(trusted)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://127.0.0.1:{port}/health", ssl=trusted) as reply:
                assert await reply.json() == {"ok": True}
            with pytest.raises(aiohttp.ClientConnectorCertificateError):
                await session.get(f"https://127.0.0.1:{port}/health")  # an untrusted client
    finally:
        await runner.cleanup()


def test_tls_needs_both_files():
    assert orchestrator_app.server_tls(None, None) is None
    with pytest.raises(ValueError, match="go together"):
        orchestrator_app.server_tls("cert.pem", None)


def test_the_orchestrator_cli_passes_the_certificate(monkeypatch):
    import argparse

    seen = {}
    monkeypatch.setattr(orchestrator_app, "serve", lambda *a, **k: seen.update(k))
    parser = argparse.ArgumentParser()
    orchestrator_cli.register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        ["orchestrator", "--database-file", "db.toml", "--tls-cert", "c", "--tls-key", "k"]
    )
    assert orchestrator_cli.dispatch(args) == 0 and seen["tls"] == ("c", "k")


# --- calculation subprocess ---------------------------------------------------------------------


def python(code):
    return [sys.executable, "-c", code]


def test_the_calculation_command():
    plain = jobs.calculation_argv("db.toml", "m.toml")
    tail = ["calculate", "--database-file", "db.toml", "--methodology", "m.toml", "--publish"]
    assert plain[-6:] == tail
    scoped = jobs.calculation_argv("db.toml", "m.toml", "8G")
    assert scoped[:6] == ["systemd-run", "--scope", "--quiet", "-p", "MemoryMax=8G", "--"]
    assert scoped[6:] == plain
    for bad in ("8 G", "0", "8G; reboot", "-1G"):
        with pytest.raises(ValueError, match="PROXY_INDEX_CALCULATE_MEMORY_MAX"):
            jobs.calculation_argv("db.toml", "m.toml", bad)


async def test_a_calculation_records_its_snapshot():
    payload = {"snapshot_id": "s9", "as_of": "2026-11-04T10:05:00+00:00", "readiness": {}}
    result = await jobs.run_calculation(python(f"print({json.dumps(json.dumps(payload))})"), 30)
    assert result == {"snapshot_id": "s9", "as_of": payload["as_of"], "exit_code": 0}


@pytest.mark.parametrize(
    "code, message",
    [
        ("import sys; print('x', file=sys.stderr); sys.exit(3)", "exit 3: x"),
        ("import sys; sys.exit(2)", "exit 2"),
        ("print('not json')", "exit 0 without the JSON result"),
        ("print('{}')", "exit 0 without the JSON result"),
    ],
)
async def test_a_failed_calculation_raises_with_its_exit_code(code, message):
    with pytest.raises(jobs.CalculationFailed, match=f"^{message}"):
        await jobs.run_calculation(python(code), 30)


async def test_a_calculation_past_its_timeout_is_killed_with_its_children(tmp_path):
    pids = tmp_path / "pids"
    code = (
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')\n"
        "time.sleep(60)\n"
    )
    began = time.monotonic()
    with pytest.raises(jobs.CalculationFailed, match="timed out after 1 s"):
        await jobs.run_calculation(python(code), 1)
    assert time.monotonic() - began < 10
    for pid in map(int, pids.read_text().split()):
        for _ in range(50):
            if not _alive(pid):
                break
            await asyncio.sleep(0.1)
        assert not _alive(pid)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    with open(f"/proc/{pid}/stat") as stat:  # a reaped-later zombie is not running
        return stat.read().split(")")[-1].split()[0] != "Z"


async def test_the_scheduler_records_the_calculation_outcome(database, monkeypatch):
    commands = iter([python("import sys; sys.exit(4)"), python("import time; time.sleep(30)")])
    monkeypatch.setattr(jobs, "calculation_argv", lambda *a: next(commands))
    db = Database(database)
    try:
        async with db.connection() as conn:
            await put_setting(conn, "calculate.trigger", "hourly")
            await put_setting(conn, "calculate.timeout_seconds", 1)
        scheduler = Scheduler(db)
        scheduler.add("calculate", lambda: jobs.calculate(db, database, "m.toml"), every_s=60)
        await scheduler.run_now("calculate")
        await scheduler.run_now("calculate")
        async with db.connection() as conn:
            rows = await (
                await conn.execute("SELECT status,error FROM job_runs ORDER BY id")
            ).fetchall()
    finally:
        await db.close()
    assert [(r["status"], r["error"]) for r in rows] == [
        ("failed", "CalculationFailed: exit 4"),
        ("failed", "CalculationFailed: timed out after 1 s (killed)"),
    ]


async def test_the_real_calculate_command_runs_as_a_subprocess(database):
    from proxy_index.calculated.store import initialize

    seed(database)
    initialize(database)
    argv = jobs.calculation_argv(database, jobs.METHODOLOGY)
    result = await jobs.run_calculation(argv, 240)
    assert result["exit_code"] == 0 and result["snapshot_id"]
