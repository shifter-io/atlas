"""Whether the host's Chrome is the current Stable major (browser/chrome_current.py,
CHROME-CURRENT-1): Google's answer read, the comparison, the refusal of a CURRENT_CHROME
target's run and a host that reports it failed while its other runs go on. No test reaches
Google (conftest.py stubs the fetch; the one test of the fetch fakes urlopen)."""

from __future__ import annotations

import asyncio
import io
import json
import logging

import pytest
from aiohttp.test_utils import TestServer
from browser_node_cases import SHIFTER, TOKENS, Fleet, browser_work, host_view
from test_browser_node_engines import chrome_state, launches
from test_browser_rawfiles_host import Kbp7, serve_while

from atlas.browser import chrome, chrome_current
from atlas.browser.chrome_current import Outdated, Watch, behind, served_major, stable_major
from atlas.browser.node import BrowserHost
from atlas.targets import registry

FETCH = chrome_current.fetch_stable_major  # conftest.py replaces it in every test


def release(version, fraction):
    return {"version": version, "fraction": fraction, "fractionGroup": "200"}


# Google's answer of 22 Nov 2026 (shortened): 158 to every user, 157 still to most of one
# group, and an early 159 to half a percent, which does not count.
ANSWER = {
    "releases": [
        release("157.0.8103.79", 0.495),
        release("158.0.8127.36", 0.2475),
        release("159.0.8154.13", 0.005),
        release("158.0.8127.37", 1),
        release("156.0.8078.82", 0),
    ]
}


def test_the_current_stable_is_the_newest_major_served_to_most_users():
    assert stable_major(ANSWER) == 158
    # 158 not yet to most users: 157 is the current one.
    assert stable_major({"releases": ANSWER["releases"][:3] + [release("157.0.8103.2", 1)]}) == 157
    assert stable_major({"releases": [release("159.0.8154.1", 0.5)]}) == 159


@pytest.mark.parametrize(
    "answer", [{}, [], None, {"releases": None}, {"releases": [release("158.0.8127.1", 0.2)]}]
)
def test_an_answer_naming_no_current_stable_gives_none(answer):
    assert stable_major(answer) is None


@pytest.mark.parametrize(
    "item",
    ["158.0.8127.1", {"version": 158, "fraction": 1}, {"version": "158.0"}, release("x.0", 1)],
)
def test_a_malformed_release_counts_for_nothing(item):
    assert served_major(item) is None


@pytest.mark.parametrize(
    ("host", "stable", "outdated"),
    [
        (157, 158, True),
        (156, 158, True),
        (158, 158, False),
        (159, 158, False),
        (None, 158, False),
        (157, None, False),
    ],
)
def test_only_a_known_older_major_is_behind(host, stable, outdated):
    assert behind(host, stable) is outdated


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_the_fetch_reads_googles_stable_releases_for_the_host_os(monkeypatch, caplog):
    asked = []

    def urlopen(url, timeout):
        asked.append((url, timeout))
        return Answer(json.dumps(ANSWER).encode())

    monkeypatch.setattr(chrome_current.urllib.request, "urlopen", urlopen)
    assert FETCH("macos") == 158
    assert FETCH("linux") == 158
    assert [url.split("/")[6] for url, _ in asked] == ["mac", "linux"]
    assert asked[0][0].endswith("/channels/stable/versions/all/releases?filter=endtime=none")

    def down(url, timeout):
        raise OSError("unreachable")

    monkeypatch.setattr(chrome_current.urllib.request, "urlopen", down)
    assert FETCH("macos") is None
    monkeypatch.setattr(chrome_current.urllib.request, "urlopen", lambda u, timeout: Answer(b"<"))
    assert FETCH("macos") is None
    assert "Chrome Stable version not read (JSONDecodeError)" in caplog.text


def watch(monkeypatch, version, stable):
    """A Watch whose Chrome --version gives `version` and Google `stable` (a list: one per read)."""
    stables = list(stable)
    monkeypatch.setattr(chrome, "detect", lambda executable: version)
    return Watch("/chrome", "macos", fetch=lambda os_name: stables.pop(0), interval=0.01)


def test_a_refresh_reads_both_majors_and_keeps_the_last_stable(monkeypatch, caplog):
    current = watch(monkeypatch, "157.0.8103.79", [158, None])
    assert current.state() == {"host": None, "stable": None}
    current.refresh()
    assert current.state() == {"host": 157, "stable": 158}
    assert "Chrome 157 is behind the current Stable 158" in caplog.text
    current.refresh()  # Google unreachable: the last answer holds
    assert current.state() == {"host": 157, "stable": 158}

    def missing(executable):
        raise chrome.ChromeRefused("Chrome is missing or not executable: /chrome")

    monkeypatch.setattr(chrome, "detect", missing)
    current.fetch = lambda os_name: 158
    current.refresh()
    assert current.state() == {"host": None, "stable": 158}


def test_only_an_outdated_chrome_run_of_a_current_chrome_target_is_refused(monkeypatch):
    amazon, search = registry.get("amazon_pdp"), registry.get("google_search")
    current = watch(monkeypatch, "157.0.8103.2", [158])
    current.check(amazon, "chrome")  # nothing known yet: nothing refused
    current.refresh()
    with pytest.raises(Outdated, match=r"^chrome_outdated: Chrome 157, current Stable 158$"):
        current.check(amazon, "chrome")
    current.check(amazon, "camoufox")
    current.check(search, "chrome")
    current.host = 158
    current.check(amazon, "chrome")


async def test_the_loop_refreshes_until_stopped_and_survives_a_failed_pass(monkeypatch, caplog):
    current = watch(monkeypatch, "158.0.8127.2", [158])

    def broken(os_name):
        current.fetch = lambda os_name: 159
        raise RuntimeError("one bad pass")

    current.fetch = broken
    stop = asyncio.Event()
    task = asyncio.create_task(current.loop(stop))
    for _ in range(500):
        if current.stable == 159:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await task
    assert current.state() == {"host": 158, "stable": 159}
    assert "Chrome version check failed (RuntimeError)" in caplog.text


async def test_a_host_behind_reports_its_amazon_run_failed_and_runs_the_rest(
    tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.WARNING)
    state = chrome_state(tmp_path, monkeypatch)  # amazon_pdp on Chrome 151
    fox, chromes, launch = launches()
    monkeypatch.setattr(chrome_current, "fetch_stable_major", lambda os_name: 152)
    work = {TOKENS[SHIFTER]: [browser_work(1, "amazon_pdp"), browser_work(2, "google_search")]}
    fake = Kbp7(work)
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
    host.current.host, host.current.stable = 151, 152  # as the first refresh reads them
    try:
        await serve_while(host, lambda: 1 in fake.failed and 2 in fake.results)
    finally:
        await server.close()
    # Not measured: failed with the reason, no bundle, no Chrome launched for it.
    assert fake.failed[1] == (TOKENS[SHIFTER], "chrome_outdated: Chrome 151, current Stable 152")
    assert 1 not in fake.results and chromes.browsers == [] and len(fox.proxies()) == 2
    assert "batch 1 refused: chrome_outdated" in caplog.text
    assert (await host.hour_line())["chrome_current"] == {"host": 151, "stable": 152}


def test_a_camoufox_only_host_never_checks_chrome(tmp_path, monkeypatch):
    from browser_node_cases import make_state

    state = make_state(tmp_path)
    host = BrowserHost(
        host_view(state), state, "http://orchestrator.invalid", launch=Fleet().launch
    )
    host.current.host, host.current.stable = 100, 158
    options = host.config_options(host.networks[0], browser_work(1, "amazon_pdp"))
    assert options["browser_engine"] == "camoufox"
    host.log.close()
