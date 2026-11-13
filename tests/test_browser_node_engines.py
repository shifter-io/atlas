"""A browser host with targets on two engines (browser/node.py): each run on its target's
engine, recorded in its summary, tries, manifest and log line, with its process samples in
its bundle; a fake orchestrator in-process, fake browsers, no Chrome and no proxy traffic."""

from __future__ import annotations

import json
import logging
import tarfile

import pytest
from aiohttp.test_utils import TestServer
from browser_node_cases import (
    SHIFTER,
    TOKENS,
    Fleet,
    browser_work,
    host_view,
    log_lines,
    make_state,
)
from browser_rawfiles_cases import MEMBERS
from test_browser_rawfiles_host import Kbp7, serve_while

from atlas.browser import chrome
from atlas.browser import node as host_module
from atlas.browser import run as run_module
from atlas.browser.node import BrowserHost

DETECTED, LAUNCHED = "151.0.7", "151.0.9"


def chrome_state(tmp_path, monkeypatch):
    """make_state() with amazon_pdp on Chrome; the host's Chrome is detected as DETECTED."""
    state = make_state(tmp_path)
    path = state / "settings.toml"
    text = path.read_text().replace('id = "amazon_pdp"\n', 'id = "amazon_pdp"\nengine = "chrome"\n')
    path.write_text(text)
    monkeypatch.setattr(host_module.chrome, "detect", lambda executable: DETECTED)
    return state


def launches():
    """A Camoufox fleet and a Chrome fleet whose browsers report a Chrome identity."""
    fox, chromes = Fleet(), Fleet()

    async def launch_chrome():
        browser = await chromes.launch()
        agent = chrome.user_agent(LAUNCHED, "macos")
        browser.identity = {"engine": "chrome", "version": LAUNCHED, "user_agent": agent}
        return browser

    return fox, chromes, {"camoufox": fox.launch, "chrome": launch_chrome}


def bundle(archive):
    with tarfile.open(archive, "r:gz") as tar:
        read = {name: tar.extractfile(name).read() for name in tar.getnames() if "." in name}
    manifest = json.loads(read[MEMBERS.manifest])
    tries = [json.loads(line) for line in read[MEMBERS.tries].splitlines()]
    samples = [json.loads(line) for line in read[MEMBERS.samples].splitlines()]
    return json.loads(read["summary.json"]), manifest, tries, samples


async def test_each_run_uses_and_records_its_targets_engine(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(run_module, "DEADLINE_MARGIN_S", -600)
    state = chrome_state(tmp_path, monkeypatch)
    fox, chromes, launch = launches()
    work = {TOKENS[SHIFTER]: [browser_work(1, "amazon_pdp"), browser_work(2, "google_search")]}
    fake = Kbp7(work, replies=(503,))  # never confirmed: both bundles stay in the outbox
    server = TestServer(fake.app)
    await server.start_server()
    host = BrowserHost(
        host_view(state),
        state,
        str(server.make_url("")),
        launch=launch,
        poll_interval=0.02,
        heartbeat_interval=0.05,
    )
    host.read_ps = lambda: {}
    assert set(host.pools) == {"camoufox", "chrome"}
    assert host.chrome == {
        "name": "chrome",
        "version": DETECTED,
        "user_agent": chrome.user_agent(DETECTED, host_module.host_os()),
    }
    try:
        await serve_while(host, lambda: {1, 2} <= set(fake.results))
    finally:
        await server.close()

    # amazon_pdp's 3 tries ran on Chrome, google_search's 2 on Camoufox.
    assert len(chromes.proxies()) == 3 and len(fox.proxies()) == 2
    found = {}
    for pending in host.outbox.pending():
        summary, manifest, tries, samples = bundle(pending.archive)
        found[manifest["target"]] = (summary, manifest, tries, samples)
    summary, manifest, tries, samples = found["amazon_pdp"]
    assert summary["config"]["browser_engine"] == "chrome"
    # The launched Chrome's identity, not the detected one.
    assert manifest["engine"]["version"] == LAUNCHED and "camoufox" not in manifest
    assert {record["engine"] for record in tries} == {"chrome"} and len(tries) == 3
    assert samples[-1]["summary"]["samples"] == len(samples) - 1 >= 1
    summary, manifest, tries, _ = found["google_search"]
    assert summary["config"]["browser_engine"] == "camoufox"
    assert manifest["engine"]["name"] == "camoufox" and manifest["camoufox"] == host.identity
    assert {record["engine"] for record in tries} == {"camoufox"}
    lines = {line["batch"]: line for line in log_lines(state) if line["kind"] == "run"}
    assert (lines[1]["engine"], lines[2]["engine"]) == ("chrome", "camoufox")
    assert f"browser launched: Chrome {LAUNCHED}" in caplog.text
    # One budget of browsers for both engines.
    stats = host.pool.stats()
    assert stats["size"] == 3 and stats["launches"] == len(fox.browsers) + len(chromes.browsers)


def test_a_camoufox_only_host_detects_no_chrome(tmp_path, monkeypatch):
    def refuse(executable):
        raise AssertionError("no Chrome target: Chrome is never detected")

    monkeypatch.setattr(host_module.chrome, "detect", refuse)
    state = make_state(tmp_path)
    host = BrowserHost(
        host_view(state), state, "http://orchestrator.invalid", launch=Fleet().launch
    )
    assert set(host.pools) == {"camoufox"} and host.chrome is None
    options = host.config_options(host.networks[0], browser_work(1, "amazon_pdp"))
    assert options["browser_engine"] == "camoufox"
    # A Chrome run left from earlier settings has no identity to seal with: kept (ValueError).
    with pytest.raises(ValueError, match="runs no Chrome"):
        host.seal_run(tmp_path, None, {"config": {"browser_engine": "chrome"}}, "a" * 64)
    host.log.close()


def test_a_missing_chrome_refuses_the_start_of_a_host_with_a_chrome_target(tmp_path, monkeypatch):
    state = chrome_state(tmp_path, monkeypatch)

    def missing(executable):
        raise chrome.ChromeRefused("Chrome is missing or not executable")

    monkeypatch.setattr(host_module.chrome, "detect", missing)
    with pytest.raises(chrome.ChromeRefused):
        BrowserHost(host_view(state), state, "http://orchestrator.invalid", launch=Fleet().launch)
