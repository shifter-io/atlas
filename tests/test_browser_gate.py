"""BROWSER-BUDGET-1: a budgeted network's browser tries on its host stay within its browser
reserve of gateway connections, one try at a time; other networks run as before (fake pages
whose loads open real connections through the try's relay to a fake gateway)."""

from __future__ import annotations

import asyncio
import contextlib
import socket

import browser_transport_cases
import pytest
from browser_node_cases import CHEAP, SHIFTER, Fleet, browser_work, host_view, make_state
from browser_site import SiteTarget
from helpers import server

from atlas.browser import gate as gate_module
from atlas.browser.gate import Gate
from atlas.browser.node import BrowserHost
from atlas.browser.relay import BAD_GATEWAY, relayed
from atlas.browser.transport import NullRecorder
from atlas.daily import budget
from atlas.models import ProxyConfig, RequestContext, RunConfig

ANSWER = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
REQUEST = b"GET http://site.invalid/ok HTTP/1.1\r\nHost: site.invalid\r\n\r\n"
TUNNELS = 15  # the connections one heavy page load opens, each kept alive


class Gateway:
    """The fake gateway: answers every request on a kept-alive connection and counts how many
    connections are open at once; each answer `delay` seconds after its request."""

    def __init__(self, delay: float = 0) -> None:
        self.open = self.peak = self.connections = 0
        self.delay = delay

    async def __call__(self, reader, writer):
        self.open += 1
        self.connections += 1
        self.peak = max(self.peak, self.open)
        try:
            while await reader.readuntil(b"\r\n\r\n"):
                await asyncio.sleep(self.delay)
                writer.write(ANSWER)
                await writer.drain()
        finally:
            self.open -= 1


async def fetch(proxy: ProxyConfig) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """One kept-alive request through `proxy`: its connection stays open, like a browser's."""
    reader, writer = await asyncio.open_connection(proxy.host, proxy.port)
    writer.write(REQUEST)
    await writer.drain()
    assert await asyncio.wait_for(reader.readexactly(len(ANSWER)), 10) == ANSWER
    return reader, writer


@pytest.fixture
def heavy_page(monkeypatch):
    """The fake page's load opens TUNNELS connections through its context's proxy, all at once,
    and keeps them open (the relay closes them when the try's context closes); the test closes
    its own ends."""
    held: list[asyncio.StreamWriter] = []

    async def goto(self, url, timeout_ms):
        proxy = self.browser.contexts[-1][0]
        opened = await asyncio.gather(*(fetch(proxy) for _ in range(TUNNELS)))
        held.extend(writer for _, writer in opened)
        return 200

    monkeypatch.setattr(browser_transport_cases.FakePage, "goto", goto)
    return held


def proxy(port: int) -> ProxyConfig:
    return ProxyConfig("http", "127.0.0.1", port, "user", "pass")


async def two_tries(tmp_path, network: str, held) -> tuple[Gateway, Fleet, BrowserHost]:
    """Two tries of one run of `network` at once on the host's transport, as its run makes."""
    state = make_state(tmp_path)
    fleet = Fleet(delay=0)
    host = BrowserHost(host_view(state), state, "http://orchestrator.invalid", launch=fleet.launch)
    for gate in host.gates.values():
        gate.idle_s = 0.2
    [agent] = [a for a in host.agents if a.network.name == network]
    options = host.run_options(agent.network, browser_work(1, "amazon_pdp"), NullRecorder())
    gateway = Gateway()
    async with server(gateway) as port:
        provider, product = network.split("/")
        config = RunConfig("amazon_pdp", proxy(port), provider=provider, product=product)
        transport = options["transport_factory"](config)
        tries = [transport.execute(SiteTarget(), context=RequestContext("r", i, 0)) for i in (0, 1)]
        outcomes = await asyncio.wait_for(asyncio.gather(*tries), 30)
    for writer in held:
        writer.close()
    host.log.close()
    assert [o.verdict for o in outcomes] == ["ok", "ok"]
    return gateway, fleet, host


async def test_a_budgeted_network_keeps_its_tries_within_its_browser_reserve(tmp_path, heavy_page):
    gateway, fleet, host = await two_tries(tmp_path, CHEAP, heavy_page)
    assert budget.browser_connections("proxy-cheap") == budget.BROWSER_RESERVE == 10
    # 2 tries x 15 tunnels: never more than the reserve at the gateway, every load answered.
    assert gateway.peak <= budget.BROWSER_RESERVE < 2 * TUNNELS
    assert gateway.connections == 2 * TUNNELS
    assert fleet.peak == 1  # one try at a time
    stats = host.gates[CHEAP].stats()
    assert stats["limit"] == 10 and stats["peak"] == gateway.peak
    # Each try's 5 tunnels over the reserve waited, each for an idle one closed.
    assert (stats["waits"], stats["closed_idle"]) == (2 * (TUNNELS - 10), 2 * (TUNNELS - 10))


async def test_a_network_without_a_budget_runs_as_before(tmp_path, heavy_page):
    gateway, fleet, host = await two_tries(tmp_path, SHIFTER, heavy_page)
    assert budget.browser_connections("shifter") is None and SHIFTER not in host.gates
    assert gateway.peak == 2 * TUNNELS and fleet.peak == 2


async def test_the_hour_line_gives_each_gate_and_starts_its_counts_again(tmp_path):
    state = make_state(tmp_path)
    orchestrator = "http://orchestrator.invalid"
    host = BrowserHost(host_view(state), state, orchestrator, launch=Fleet().launch)
    host.gates[CHEAP].waits = 3
    line = await host.hour_line()
    assert line["gates"] == {"proxy-cheap": {"limit": 10, "peak": 0, "waits": 3, "closed_idle": 0}}
    assert (await host.hour_line())["gates"]["proxy-cheap"]["waits"] == 0
    host.log.close()


async def test_a_waiting_connection_closes_the_one_idle_longest():
    gateway, gate = Gateway(), Gate(2, idle_s=0.2)
    async with server(gateway) as port, relayed(proxy(port), gate) as (through, _):
        first = await fetch(through)
        await asyncio.sleep(0.1)
        second = await fetch(through)
        third = await fetch(through)  # waits until `first` has been idle for 0.2 s
        assert await asyncio.wait_for(first[0].read(), 5) == b""  # the relay closed it
        assert gate.held == 2 and gate.peak == 2
        second[1].write(REQUEST)  # `second` still answers
        assert await asyncio.wait_for(second[0].readexactly(len(ANSWER)), 5) == ANSWER
        for _, writer in (first, second, third):
            writer.close()
    assert (gate.closed_idle, gate.waits) == (1, 1)


async def test_a_connection_waiting_for_its_answer_is_not_closed():
    """A slow answer (proxy-cheap answers slowly) outlasts idle_s: the waiting connection does
    not cut it, and once it is answered and idle the waiting one closes it and goes through."""
    gate = Gate(1, idle_s=0.1)
    async with server(Gateway(delay=0.6)) as port, relayed(proxy(port), gate) as (through, _):
        slow = asyncio.create_task(fetch(through))
        await asyncio.sleep(0.05)
        waiting = asyncio.create_task(fetch(through))
        reader, writer = await slow  # its answer came: it was not closed
        _, other = await waiting
        assert await asyncio.wait_for(reader.read(), 5) == b""  # closed once idle
        for each in (writer, other):
            each.close()
    assert (gate.closed_idle, gate.waits) == (1, 1)


async def test_an_answer_pending_past_its_limit_is_closed_for_a_waiting_one(monkeypatch):
    monkeypatch.setattr(gate_module, "PENDING_S", 0.2)
    gate = Gate(1, idle_s=0.1)
    async with server(Gateway(delay=5)) as port, relayed(proxy(port), gate) as (through, _):
        reader, writer = await asyncio.open_connection(through.host, through.port)
        writer.write(REQUEST)
        await asyncio.sleep(0.05)
        waiting = asyncio.create_task(asyncio.open_connection(through.host, through.port))
        assert await asyncio.wait_for(reader.read(), 5) == b""  # cut after PENDING_S
        _, other = await waiting
        for each in (writer, other):
            each.close()
    assert gate.closed_idle == 1


async def test_a_refused_gateway_connection_frees_its_place():
    with contextlib.closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    gate = Gate(1)
    async with relayed(proxy(port), gate) as (through, _):
        for _ in range(3):
            reader, writer = await asyncio.open_connection(through.host, through.port)
            writer.write(REQUEST)
            assert await asyncio.wait_for(reader.read(), 5) == BAD_GATEWAY
            writer.close()
    assert gate.held == 0 and gate.waits == 0


def test_a_gate_holds_at_least_one_connection():
    with pytest.raises(ValueError, match="at least one"):
        Gate(0)
