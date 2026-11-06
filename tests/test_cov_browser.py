"""COV-5: the browser host's outbox and serve paths, a download cut by an I/O error and the
Cloudflare error page, which no other test reaches."""

import asyncio
import contextlib
import logging
import os
import signal
import types

import pytest
from browser_node_cases import SHIFTER, Fleet, host_view, log_lines, make_state
from browser_targets_cases import context, outcome

from proxy_index.browser import contracts as c
from proxy_index.browser import judge as j
from proxy_index.browser import node as host_module
from proxy_index.browser import release
from proxy_index.browser.node import BrowserHost
from proxy_index.targets import PageView, registry

ORCHESTRATOR = "http://127.0.0.1:9"


@pytest.fixture
def host(tmp_path):
    browser_host = BrowserHost(
        host_view(make_state(tmp_path)), tmp_path / "state", ORCHESTRATOR, launch=Fleet().launch
    )
    yield browser_host
    browser_host.log.close()


class Outbox:
    """An outbox holding `bundles`, every one due, sent by `send`."""

    def __init__(self, bundles, send):
        self.bundles, self._send, self.sent = bundles, send, []

    def pending(self):
        return list(self.bundles)

    def due(self, bundle):
        return True

    async def send(self, bundle, agent):
        self.sent.append(bundle.run_id)
        return await self._send()


def bundle(run_id, network):
    return types.SimpleNamespace(run_id=run_id, network=network)


def registered(host, network):
    agent = host.by_network[network]
    agent.node_id, agent.session = 7, object()
    return agent


def test_a_full_outbox_writes_one_host_log_line(host):
    host.guard.limit = 0
    host.runs.mkdir(parents=True, exist_ok=True)
    (host.runs / "screenshot.png").write_bytes(b"x")
    assert host.guard.full()
    host.log.close()
    (line,) = log_lines(host.state)
    assert line["kind"] == "browser_outbox_full" and line["limit_bytes"] == 0
    assert line["at"].endswith("+00:00")


async def test_flush_skips_other_networks_and_unregistered_agents(host):
    async def confirmed():
        return "confirmed"

    host.outbox = Outbox([bundle("other", "other/residential"), bundle("mine", SHIFTER)], confirmed)
    assert await host.flush() == []  # no agent of this host has registered yet
    registered(host, SHIFTER)
    assert await host.flush() == ["confirmed"]
    assert host.outbox.sent == ["mine"]


async def test_flush_reports_a_failed_send_and_passes_on_cancellation(host):
    registered(host, SHIFTER)

    async def broken():
        raise OSError("disk")

    host.outbox = Outbox([bundle("mine", SHIFTER)], broken)
    assert await host.flush() == ["error"]

    async def cancelled():
        raise asyncio.CancelledError

    host.outbox = Outbox([bundle("mine", SHIFTER)], cancelled)
    with pytest.raises(asyncio.CancelledError):
        await host.flush()


async def test_the_outbox_loop_survives_a_failed_pass_and_wakes_early(host, caplog):
    host.wake_outbox()  # before the host serves: nothing to wake
    host._wake = asyncio.Event()  # as BrowserHost._serve sets it
    host.outbox_interval = 3600
    stop, passes = asyncio.Event(), []

    async def flush():
        passes.append(len(passes))
        if len(passes) == 1:
            raise RuntimeError("database gone")
        stop.set()
        host.wake_outbox()  # end the loop's wait at once
        return []

    host.flush = flush
    with caplog.at_level(logging.WARNING, logger=host_module.__name__):
        loop = asyncio.create_task(host._outbox_loop(stop))
        await asyncio.sleep(0.05)
        assert passes == [0]  # the failed pass waits for a wake, not the hour
        host.wake_outbox()
        await asyncio.wait_for(loop, 5)
    assert passes == [0, 1]
    assert "outbox pass failed (RuntimeError)" in caplog.text


async def test_serve_runs_the_host_until_sigterm():
    class Host:
        stopped = False

        async def run(self, stop):
            os.kill(os.getpid(), signal.SIGTERM)
            await stop.wait()
            self.stopped = True

    served = Host()
    await asyncio.wait_for(host_module.serve(served), 5)
    assert served.stopped


def test_an_io_error_during_the_download_is_refused_and_leaves_nothing(tmp_path, monkeypatch):
    class Cut:
        def read(self, size):
            raise ConnectionResetError

    @contextlib.contextmanager
    def cut(url, step):
        yield Cut()

    build = release.Build("https://cdn.example/camoufox.zip", "0" * 64, 10, "camoufox-bin")
    monkeypatch.setitem(release.BUILDS, "linux-x86_64", build)
    monkeypatch.setattr(release, "_open", cut)
    with pytest.raises(release.ReleaseError, match="download camoufox.zip: ConnectionResetError"):
        release.fetch(tmp_path, "linux-x86_64")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status", [200, 403])
def test_a_cloudflare_error_page_without_the_expected_text(status):
    target = registry.get(c.CLOUDFLARE_CHALLENGE)
    page = {"url": "https://www.example.com/", "expect": "Example Domain"}
    counts = dict.fromkeys(j.selectors(target.page(context(0), (page,)).wait_for), 0)
    view = PageView(page["url"], status, "Bad gateway", "Bad gateway", counts)
    assert outcome(target.judge(view)) == ("error", "no_content", "no_expect")
    view.found["#cf-error-details"] = 1
    assert outcome(target.judge(view)) == ("error", "no_content", "cf_error_page")
    # The rules alone: the expected text wins over the error details (judge() accepts such a
    # page earlier, through shown()).
    view.found['body:has-text("Example Domain")'] = 1
    assert outcome(target.rules(view)) == ("ok", None, None)
    assert outcome(target.judge(view)) == ("ok", None, None)
