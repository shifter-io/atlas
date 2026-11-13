"""MANAGER-ORCH-1: the orchestrator serving HTTPS with its certificate and running the calculation
as a subprocess (its command, timeout, cancel and the scheduler's record of the outcome)."""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
import time

import aiohttp
import pytest
import trustme
from aiohttp import web

from atlas.orchestrator import app as orchestrator_app
from atlas.orchestrator import cli as orchestrator_cli
from atlas.orchestrator import jobs
from atlas.orchestrator.db import Database, put_setting
from atlas.orchestrator.scheduler import Scheduler
from atlas.storage import seed

pytest_plugins = ["test_storage"]


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
    assert seen["browser"] == (None, None)
    browser = ["--browser-port", "8444", "--client-ca", "ca.pem"]
    args = parser.parse_args(["orchestrator", "--database-file", "db.toml", *browser])
    assert orchestrator_cli.dispatch(args) == 0 and seen["browser"] == (8444, "ca.pem")


def test_the_browser_port_and_its_client_ca_go_together():
    tls = ("cert.pem", "key.pem")
    assert orchestrator_app.browser_tls(tls, None) is None
    assert orchestrator_app.browser_tls(tls, (None, None)) is None
    for half in ((8444, None), (None, "ca.pem")):
        with pytest.raises(ValueError, match="--browser-port and --client-ca go together"):
            orchestrator_app.browser_tls(tls, half)


# --- calculation subprocess ---------------------------------------------------------------------


def python(code):
    return [sys.executable, "-c", code]


def test_the_calculation_command():
    plain = jobs.calculation_argv("db.toml", "m.toml")
    tail = ["calculate", "--database-file", "db.toml", "--methodology", "m.toml", "--publish"]
    assert plain[-7:] == [*tail, "--incremental"]  # never the CLI's default
    scoped = jobs.calculation_argv("db.toml", "m.toml", "8G")
    assert scoped[:9] == [
        "systemd-run",
        "--scope",
        "--quiet",
        "--unit=proxy-index-calculate",
        "-p",
        "MemoryMax=8G",
        "-p",
        "CollectMode=inactive-or-failed",  # an OOM-killed scope never blocks the next hour
        "--",
    ]
    assert scoped[9:] == plain
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


async def test_a_cancelled_calculation_is_killed_and_the_cancel_propagates(tmp_path):
    pids = tmp_path / "pids"
    code = (
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')\n"
        "time.sleep(60)\n"
    )
    task = asyncio.ensure_future(jobs.run_calculation(python(code), 60))
    for _ in range(100):
        if pids.exists() and len(pids.read_text().split()) == 2:
            break
        await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for pid in map(int, pids.read_text().split()):
        for _ in range(50):
            if not _alive(pid):
                break
            await asyncio.sleep(0.1)
        assert not _alive(pid)


def _proc_stat(pid):
    with open(f"/proc/{pid}/stat") as stat:
        return stat.read()


def _alive(pid, read=_proc_stat):
    """Whether `pid` still runs: not gone, not a zombie waiting to be reaped. A process reaped
    between the signal check and the /proc read (ENOENT, or ESRCH from the read) is gone."""
    try:
        os.kill(pid, 0)
        state = read(pid).split(")")[-1].split()[0]
    except (ProcessLookupError, FileNotFoundError):
        return False
    return state != "Z"


@pytest.mark.parametrize("gone", [ProcessLookupError, FileNotFoundError])
def test_a_process_reaped_during_the_check_is_not_alive(gone):
    """The race CI hit (run 41887321826): kill(pid, 0) succeeds, then /proc says it is gone."""

    def reaped(pid):
        raise gone(3 if gone is ProcessLookupError else 2, "No such process")

    assert _alive(os.getpid()) is True
    assert _alive(os.getpid(), read=reaped) is False
    assert _alive(os.getpid(), read=lambda pid: f"{pid} (python) Z 1 2 3") is False


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
    from atlas.calculated.store import initialize

    seed(database)
    initialize(database)
    argv = jobs.calculation_argv(database, jobs.METHODOLOGY)
    result = await jobs.run_calculation(argv, 240)
    assert result["exit_code"] == 0 and result["snapshot_id"]
