"""Shifter runs through a loopback gateway: auth on both targets, provider errors, cancellation
and rotation."""

import asyncio
import json
import ssl
from dataclasses import replace

import pytest
import trustme
from helpers import Gateway, server
from providers_cases import ENV, shifter

from proxy_index import JsonSink, RequestContext, RunConfig, RunOptions, run
from proxy_index.targets import EchoIpTarget, TargetRegistry
from proxy_index.transport import HttpTransport

class LoopbackProvider:
    """Replace only the endpoint; production Shifter credentials still get resolved."""

    def __init__(self, provider, port):
        self.provider, self.port, self.scheme = provider, port, provider.scheme
        self.contexts = []

    def public(self):
        return self.provider.public()

    def resolve(self, context):
        self.contexts.append(context)
        return replace(self.provider.resolve(context), host="127.0.0.1", port=self.port)


@pytest.mark.parametrize("scheme", ["http", "socks5"])
@pytest.mark.parametrize("tls", [False, True])
@pytest.mark.parametrize("policy", ["per_request", "per_worker", "per_run"])
async def test_provider_auth_on_both_targets_with_concurrent_sessions(
    tmp_path,
    scheme,
    tls,
    policy,
):
    class TrackedTransport(HttpTransport):
        active = 0
        peak = 0

        async def execute(self, target, *, context=None):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                return await super().execute(target, context=context)
            finally:
                self.active -= 1

    tls_server, tls_client = None, None
    if tls:
        ca = trustme.CA()
        tls_server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ca.issue_cert("localhost").configure_cert(tls_server)
        tls_client = ssl.create_default_context()
        ca.configure_trust(tls_client)
    origin_requests = []

    async def origin(reader, writer):
        origin_requests.append(await reader.readuntil(b"\r\n\r\n"))
        await Gateway().respond(writer)

    def auth(username, password):
        return (
            username.startswith("customer-fixture_33-country-us-sid-")
            and password == ENV["SHIFTER_PASSWORD"]
        )

    gateway = Gateway(scheme, auth=auth, delay=0.005)
    async with server(origin, tls=tls_server) as origin_port, server(gateway) as port:
        target = "generic_https" if tls else "generic_http"
        targets = TargetRegistry()
        targets.register(
            EchoIpTarget(
                target, f"{'https' if tls else 'http'}://localhost:{origin_port}/", not tls
            )
        )
        provider = shifter(
            scheme=scheme, parameters={"country": "us", "ttl": 10}, session_policy=policy
        )
        proxy = LoopbackProvider(provider, port)
        sink = JsonSink(tmp_path)
        config = RunConfig(target, proxy, requests=12, concurrency=3)
        transport = TrackedTransport(config, ssl_context=tls_client)
        result = await run(
            config, sink, RunOptions(targets=targets, transport_factory=lambda _: transport)
        )
    assert result.successes == 12 and gateway.connections == 12
    # TLS close_notify can leave completed server tunnels draining while the
    # next request starts. Bound live request execution, not server teardown.
    assert 1 < transport.peak <= 3 and transport.active == 0
    assert {ctx.request_index for ctx in proxy.contexts} == set(range(12))
    assert {ctx.worker_index for ctx in proxy.contexts} == {0, 1, 2}
    assert {ctx.run_id for ctx in proxy.contexts} == {result.run_id}
    actual = [username for username, password in gateway.credentials]
    expected = [provider.resolve(ctx).username for ctx in proxy.contexts]
    assert sorted(actual) == sorted(expected)
    assert len(set(actual)) == {"per_request": 12, "per_worker": 3, "per_run": 1}[policy]
    assert result.unique_ips == (None if tls else 1)
    assert all(b"Proxy-Authorization" not in request for request in origin_requests)
    text = (sink.directory / "summary.json").read_text()
    assert "private-password" not in text and "fixture_33" not in text
    assert json.loads(text)["config"]["provider_config"]["session_policy"] == policy


@pytest.mark.parametrize(
    "status,error", [(407, "proxy_auth"), (502, "http_status"), (509, "http_status")]
)
async def test_provider_errors_keep_status_without_retry_or_filter_changes(tmp_path, status, error):
    gateway = Gateway(status=status)
    async with server(gateway) as port:
        proxy = LoopbackProvider(shifter(parameters={"country": "us", "strict": True}), port)
        result = await run(RunConfig("generic_http", proxy, requests=3), JsonSink(tmp_path))
    assert gateway.connections == 3 and result.errors == {error: 3}
    assert result.status_codes == {str(status): 3}
    assert all(username.endswith("country-us-strict-true") for username, _ in gateway.credentials)


async def test_dynamic_socks_sessions_close_on_cancellation(tmp_path, monkeypatch):
    gateway = Gateway("socks5", delay=1)
    sessions = []
    original = HttpTransport._make_session

    def track(self, proxy):
        session = original(self, proxy)
        sessions.append(session)
        return session

    monkeypatch.setattr(HttpTransport, "_make_session", track)
    stop = asyncio.Event()
    async with server(gateway) as port:
        proxy = LoopbackProvider(shifter(scheme="socks5", session_policy="per_request"), port)
        # This inspects in-process session objects; multiprocess cancellation has its own tests.
        config = RunConfig("generic_http", proxy, requests=100, concurrency=3, processes=1)
        task = asyncio.create_task(run(config, JsonSink(tmp_path), RunOptions(stop=stop)))
        await asyncio.wait_for(gateway.accepted.wait(), 2)
        stop.set()
        result = await task
    assert result.status == "cancelled" and result.cancelled == 3
    assert len(sessions) == 3 and all(session.closed for session in sessions)


async def test_rotate_policy_never_sends_a_session_id(tmp_path):
    # Census and pulse rely on the gateway rotating per connection: no sid,
    # so no sticky session, on any request of any run.
    provider = shifter(parameters={"country": "de"})
    assert provider.session_policy == "rotate"
    usernames = {
        provider.resolve(RequestContext(run, index, index % 7)).username
        for run in ("one", "two")
        for index in range(50)
    }
    assert usernames == {"customer-fixture_33-country-de"}
    with pytest.raises(ValueError):
        shifter(parameters={"country": "de", "ttl": 1})
    gateway = Gateway(auth=lambda username, password: True)
    async with server(gateway) as port:
        proxy = LoopbackProvider(provider, port)
        result = await run(
            RunConfig("generic_http", proxy, requests=6, concurrency=3), JsonSink(tmp_path)
        )
    assert result.successes == 6
    assert {username for username, _ in gateway.credentials} == {"customer-fixture_33-country-de"}
    assert all("sid" not in username for username, _ in gateway.credentials)
    assert result.config["provider_config"]["session_policy"] == "rotate"
