"""The browser-node command: --check launches browsers and reports them, serving honours the
settings' switch, pins the manager's certificate, presents its client certificate and stops on
SIGTERM."""

from __future__ import annotations

import asyncio
import json
import ssl
from pathlib import Path

import pytest
from browser_node_cases import CHEAP, SETTINGS, SHIFTER, Fleet, host_view, make_state, private
from helpers import client_ca, client_certificate, self_signed
from test_browser_driver import UA, PWBrowser, fake_modules

from atlas import cli
from atlas.browser import node as host_module
from atlas.browser import release
from atlas.browser.node import BrowserHost


def install_fake_release(state: Path) -> None:
    name = release.current_platform()
    build = release.BUILDS.get(name)
    if build is None:
        pytest.skip("no pinned build for this platform")
    target = state / "camoufox" / release.RELEASE
    executable = target / build.executable
    executable.parent.mkdir(parents=True)
    executable.write_text("")
    marker = {"release": release.RELEASE, "platform": name, "sha256": build.sha256}
    (target / release.MARKER).write_text(json.dumps(marker))


def run_cli(state: Path, *extra: str) -> int:
    settings = str(state / "settings.toml")
    argv = ["browser-node", "--settings", settings, "--state", str(state), "--host", "browser-1"]
    return cli.main([*argv, *extra])


def test_check_launches_browsers_and_prints_their_identity(tmp_path, monkeypatch, capsys):
    state = make_state(tmp_path)
    install_fake_release(state)
    fake_modules(monkeypatch, PWBrowser("158.0.1", [UA.format(158)] * 2))
    sizes = iter([100, 350, 600])
    monkeypatch.setattr("atlas.browser.pool.resident_bytes", lambda: next(sizes))
    assert run_cli(state, "--check", "--browsers", "2") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["release"] == release.RELEASE and report["engine"] == "Firefox"
    assert report["engine_version"] == "158.0.1" and report["user_agent"] == UA.format(158)
    assert report["networks"] == [SHIFTER, CHEAP] and report["host"] == "browser-1"
    assert report["browsers"] == [{"resident_bytes": 250}, {"resident_bytes": 250}]
    assert report["resident_bytes"] == 500
    assert report["queue_load"] is None  # slots mode


def test_check_fails_on_a_user_agent_mismatch(tmp_path, monkeypatch, capsys):
    state = make_state(tmp_path)
    install_fake_release(state)
    playwright = fake_modules(monkeypatch, PWBrowser("158.0.1", [UA.format(135)] * 2))
    assert run_cli(state, "--check") == 2
    assert "user agent" in capsys.readouterr().err and playwright.stopped


def test_check_refuses_bad_options_and_a_missing_browser(tmp_path, capsys):
    state = make_state(tmp_path)
    assert run_cli(state, "--check") == 2  # no verified Camoufox install
    assert "not installed" in capsys.readouterr().err
    install_fake_release(state)
    assert run_cli(state, "--check", "--browsers", "0") == 2
    assert run_cli(state, "--check", "--browsers", "49") == 2
    assert "1 to 48" in capsys.readouterr().err
    assert run_cli(state) == 2  # serving needs --orchestrator
    assert "--orchestrator" in capsys.readouterr().err


async def test_check_reports_unknown_memory_and_closes_browsers(tmp_path):
    state = make_state(tmp_path)
    fleet = Fleet()

    async def launch():
        browser = await fleet.launch()
        browser.identity = {"engine_version": "158.0.1", "user_agent": "UA"}
        return browser

    report = await host_module.check(host_view(state), state, launch, 2, memory=lambda: None)
    assert report["browsers"] == [{"resident_bytes": None}] * 2
    assert report["resident_bytes"] is None and fleet.closed == 2


def test_serving_disabled_settings_runs_nothing(tmp_path, monkeypatch):
    state = make_state(tmp_path)
    install_fake_release(state)
    private(state / "settings.toml", SETTINGS.replace("enabled = true", "enabled = false"))
    served = []

    async def fake_serve(host):
        served.append(host)

    monkeypatch.setattr(host_module, "serve", fake_serve)
    assert run_cli(state, "--orchestrator", "https://orchestrator.invalid") == 0
    assert served == [None]
    private(state / "settings.toml", SETTINGS)
    assert run_cli(state, "--orchestrator", "https://orchestrator.invalid") == 0
    assert isinstance(served[1], BrowserHost) and served[1].name == "browser-1"
    served[1].log.close()


def test_the_host_pins_the_managers_certificate_and_refuses_plain_http(
    tmp_path, monkeypatch, capsys
):
    state = make_state(tmp_path)
    install_fake_release(state)
    served = []

    async def fake_serve(host):
        served.append(host)

    monkeypatch.setattr(host_module, "serve", fake_serve)
    assert run_cli(state, "--orchestrator", "http://203.0.113.9:8080") == 2
    assert "plain http:// is refused" in capsys.readouterr().err and served == []
    cert, _ = self_signed(tmp_path, "manager")
    assert run_cli(state, "--orchestrator", "https://203.0.113.9:8443", "--ca-file", str(cert)) == 0
    (host,) = served
    assert all(isinstance(agent.tls, ssl.SSLContext) for agent in host.agents) and host.agents
    host.log.close()


def test_the_host_presents_its_client_certificate(tmp_path, monkeypatch, capsys):
    state = make_state(tmp_path)
    install_fake_release(state)
    served = []

    async def fake_serve(host):
        served.append(host)

    monkeypatch.setattr(host_module, "serve", fake_serve)
    cert, _ = self_signed(tmp_path, "manager")
    _, ca = client_ca(tmp_path, "browser")
    mine, key = client_certificate(tmp_path, ca, "browser-1")
    pinned = ["--orchestrator", "https://203.0.113.9:8444", "--ca-file", str(cert)]
    assert run_cli(state, *pinned, "--client-cert", str(mine)) == 2
    assert "--client-cert and --client-key go together" in capsys.readouterr().err
    key.chmod(0o640)
    assert run_cli(state, *pinned, "--client-cert", str(mine), "--client-key", str(key)) == 2
    assert "client key file must be owned" in capsys.readouterr().err and served == []
    key.chmod(0o600)
    assert run_cli(state, *pinned, "--client-cert", str(mine), "--client-key", str(key)) == 0
    (host,) = served
    contexts = {id(agent.tls) for agent in host.agents}
    assert len(contexts) == 1 and isinstance(host.agents[0].tls, ssl.SSLContext)
    host.log.close()


async def test_serve_stops_on_sigterm(tmp_path):
    import os
    import signal

    async def stop_soon():
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    task = asyncio.create_task(stop_soon())
    await asyncio.wait_for(host_module.serve(None), 5)
    await task
