"""Real spawned processes and loopback HTTP only; never paid proxy traffic."""

import asyncio
import os
import pickle
import ssl
import time

import pytest
import trustme
from aiohttp import web
from helpers import Gateway, server

from proxy_index.models import Outcome, ProxyConfig, RequestContext, RunConfig
from proxy_index.process_transport import ProcessTransport, process_count
from proxy_index.providers import GrammarProxy
from proxy_index.runner import run
from proxy_index.sinks import JsonSink
from proxy_index.targets import registry


@pytest.fixture
async def gateway():
    state = {
        "six_started": asyncio.Event(),
        "active": 0,
        "peak": 0,
        "requests": 0,
        "delay": 0.005,
        "status": 200,
    }

    async def echo(request):
        state["requests"] += 1
        if state["requests"] >= 6:
            state["six_started"].set()
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            await asyncio.sleep(state["delay"])
            return web.Response(text="2001:db8::1", status=state["status"])
        finally:
            state["active"] -= 1

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", echo)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0, backlog=8192)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield ProxyConfig("http", "127.0.0.1", port), state
    await server.cleanup()


def gone(pids):
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.parametrize("concurrency,processes,requests", [(7, 3, 101), (2500, 4, 5000)])
async def test_real_http_workers_share_count_concurrency_and_uniqueness(
    gateway,
    tmp_path,
    concurrency,
    processes,
    requests,
):
    proxy, state = gateway
    observations = []
    # macOS caps the listener accept backlog at 128. Ramp loopback arrivals and
    # keep responses open to exercise 2,500 live connections without overflowing
    # that test-server queue. Production concurrency is not rate-limited by default.
    rate = None
    if concurrency == 2500:
        state["delay"] = 3
        rate = 1000

    async def observe(ip, at):
        observations.append((ip, at))

    config = RunConfig(
        "generic_http",
        proxy,
        requests=requests,
        concurrency=concurrency,
        processes=processes,
        requests_per_second=rate,
    )
    result = await run(config, JsonSink(tmp_path), observation_consumer=observe)
    assert result.completed == result.started == state["requests"] == requests
    assert result.successes == requests and result.unique_ips == 1
    assert len(observations) == requests and {ip for ip, _ in observations} == {"2001:db8::1"}
    assert 2 <= state["peak"] <= concurrency  # Total, never multiplied by processes.
    if concurrency == 2500:
        assert state["peak"] >= 2000
    assert result.config["execution_backend"] == "multiprocess"
    assert len(set(result.config["worker_pids"])) == processes
    gone(result.config["worker_pids"])


@pytest.mark.parametrize("status,reason", [(200, "unique_ratio"), (407, "no_success")])
async def test_census_is_global_and_errors_are_completions(gateway, tmp_path, status, reason):
    proxy, state = gateway
    state["status"] = status
    result = await run(
        RunConfig(
            "generic_http",
            proxy,
            requests=None,
            concurrency=12,
            processes=3,
            stop_rule="unique_ratio",
            min_sample=60,
        ),
        JsonSink(tmp_path),
    )
    assert 60 <= result.completed <= 71
    assert result.stop_reason == reason
    assert result.completed == state["requests"]
    assert result.unique_ips == (1 if status == 200 else 0)


@pytest.mark.parametrize("operator", [True, False])
async def test_stop_and_deadline_cancel_all_processes(gateway, tmp_path, operator):
    proxy, state = gateway
    state["delay"] = 0.5
    stop = asyncio.Event()
    # Spawning the workers can outlast any fixed deadline under load. The deadline
    # passes only once six requests are in flight: the run's wall clock jumps then.
    skew = [0]

    async def cancel():
        await state["six_started"].wait()
        if operator:
            stop.set()
        else:
            skew[0] = 3600

    task = asyncio.create_task(cancel())
    result = await run(
        RunConfig("generic_http", proxy, requests=1000, concurrency=6, processes=2),
        JsonSink(tmp_path),
        stop=stop,
        deadline=None if operator else time.time() + 3600,
        wall_clock=lambda: time.time() + skew[0],
    )
    await task
    assert result.status == "cancelled"
    assert result.stop_reason == ("operator" if operator else "deadline")
    assert result.cancelled > 0
    gone(result.config["worker_pids"])


class HangingTransport:
    def __init__(self, config):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, target, *, context):
        await asyncio.sleep(3600)


async def test_workers_enforce_the_deadline_themselves():
    config = RunConfig(
        "generic_http", ProxyConfig("http", "localhost", 1), concurrency=2, processes=2
    )
    target = registry.get("generic_http")
    # Spawn time decides whether a worker finds the deadline passed or times the
    # request out in flight; both answer "expired", which cancels the request.
    async with ProcessTransport(
        config, target, factory=HangingTransport, deadline=time.perf_counter() + 0.5
    ) as transport:
        pids = list(transport.pids)
        for index in range(2):
            with pytest.raises(asyncio.CancelledError):
                await transport.execute(target, context=RequestContext("hang", index, index))
    gone(pids)


class CPUTransport:
    def __init__(self, config):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, target, *, context):
        started = time.process_time()
        while time.process_time() - started < 0.2:
            sum(i * i for i in range(1000))
        return Outcome(
            latency_ms=(time.process_time() - started) * 1000,
            ip=f"10.0.0.{context.worker_index + 1}",
        )


async def test_independent_interpreters_execute_cpu_work_and_worker_crash_is_fatal():
    config = RunConfig(
        "generic_http", ProxyConfig("http", "localhost", 1), concurrency=4, processes=4
    )
    target = registry.get("generic_http")
    async with ProcessTransport(config, target, factory=CPUTransport) as transport:
        pids = list(transport.pids)
        assert len(set(pids)) == 4 and os.getpid() not in pids
        results = await asyncio.gather(
            *(transport.execute(target, context=RequestContext("cpu", i, i)) for i in range(4))
        )
        assert len({r.ip for r in results}) == 4
        assert all(r.latency_ms >= 200 and r.completed_at for r in results)
        transport.children[0].kill()
        with pytest.raises(RuntimeError, match="worker"):
            await asyncio.wait_for(
                transport.execute(target, context=RequestContext("cpu", 4, 0)), 5
            )
    gone(pids)


def test_process_configuration_is_bounded_and_validated():
    proxy = ProxyConfig("http", "localhost", 1)
    assert process_count(RunConfig("generic_http", proxy, concurrency=7, processes=4)) == 4
    assert process_count(RunConfig("generic_http", proxy, requests=2, processes=4)) == 2
    for value in (-1, True, 1.5):
        with pytest.raises(ValueError, match="processes"):
            RunConfig("generic_http", proxy, processes=value)


@pytest.mark.parametrize("policy", ["rotate", "fixed", "per_request", "per_worker", "per_run"])
def test_spawn_preserves_provider_targeting_and_global_session_identity(policy):
    params = {"country": "us", "session": "fixed"} if policy == "fixed" else {"country": "us"}
    proxy = GrammarProxy("shifter", "fixture", "secret", parameters=params, session_policy=policy)
    restored = pickle.loads(pickle.dumps(proxy))
    assert restored.public() == proxy.public()
    for index in range(10):
        context = RequestContext("shared-run", index, index % 4)
        assert restored.resolve(context) == proxy.resolve(context)
    with pytest.raises(TypeError):
        restored.parameters["country"] = "gb"


@pytest.mark.parametrize("scheme", ["http", "socks5"])
async def test_https_tunnels_use_process_backend(tmp_path, monkeypatch, scheme):
    ca = trustme.CA()
    ca_path = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(ca_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert("localhost").configure_cert(tls)

    async def origin(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        await Gateway().respond(writer)

    gateway = Gateway(scheme, auth=True)
    async with server(origin, tls=tls) as origin_port, server(gateway) as port:
        result = await run(
            RunConfig(
                "generic_https",
                ProxyConfig(scheme, "127.0.0.1", port, "test", "secret"),
                target_url=f"https://localhost:{origin_port}/",
                requests=20,
                concurrency=4,
                processes=2,
            ),
            JsonSink(tmp_path / "results"),
        )
    assert result.successes == 20 and result.unique_ips == 1
    assert result.config["execution_processes"] == 2
    assert gateway.connections == 20
