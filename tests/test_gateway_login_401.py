"""PROXY-AUTH-PAUSE-2: a gateway that refuses our login with HTTP 401 instead of 407 is a login
refusal (proxy_auth) like a 407, so the PROXY-AUTH-PAUSE-1 path pauses the network (17 Nov 2026:
proxy-cheap answered 401 and 163 false outages opened). A target's own 401 never is."""

import ssl
from functools import partial

import pytest
import trustme
from helpers import Gateway, server
from orchestrator_cases import prepare, query, set_setting
from psycopg.types.json import Jsonb
from pulse_cases import evaluated, products, run, tape, window
from test_login_pause import NOW, PAUSES, manager, state
from test_proxy_login import REGIONS

from atlas import JsonSink, ProxyConfig, RunConfig, RunOptions
from atlas import run as measure
from atlas.orchestrator import login_pause
from atlas.storage import connect
from atlas.targets import Assessment, EchoIpTarget, RequestSpec, TargetRegistry
from atlas.transport import HttpTransport

pytest_plugins = ["test_storage", "orchestrator_cases"]


async def refused_401(tmp_path, target="generic_http", url=None, requests=2):
    """A run through a loopback gateway that refuses our login with 401."""
    targets = TargetRegistry()
    targets.register(EchoIpTarget(target, url or "http://ip-info.com/myip"))
    gateway = Gateway(auth=True, refusal=401)
    async with server(gateway) as port:
        proxy = ProxyConfig("http", "127.0.0.1", port, "test", "changed")
        return await measure(
            RunConfig(target, proxy, requests=requests, concurrency=25),
            JsonSink(tmp_path / target),
            RunOptions(targets=targets),
        )


@pytest.mark.parametrize(
    "target,url",
    [
        ("generic_http", None),  # plain HTTP: the gateway itself answers the request
        ("generic_https", "https://localhost:9/"),  # HTTPS: the gateway answers the CONNECT
    ],
)
async def test_a_gateway_401_is_a_login_refusal(tmp_path, target, url):
    result = await refused_401(tmp_path, target, url)
    assert result.errors == {"proxy_auth": 2}
    assert result.status_codes == {"401": 2}
    assert (result.proxy_errors, result.blocks) == (2, 0)


async def test_a_401_inside_an_https_tunnel_is_the_targets(tmp_path):
    ca = trustme.CA()
    tls_server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert("localhost").configure_cert(tls_server)
    tls_client = ssl.create_default_context()
    ca.configure_trust(tls_client)

    async def origin(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        await Gateway(status=401, body=b"login").respond(writer)

    async with server(origin, tls=tls_server) as origin_port, server(Gateway()) as port:
        targets = TargetRegistry()
        targets.register(EchoIpTarget("generic_https", f"https://localhost:{origin_port}/"))
        result = await measure(
            RunConfig("generic_https", ProxyConfig("http", "127.0.0.1", port), requests=2),
            JsonSink(tmp_path),
            RunOptions(
                targets=targets, transport_factory=partial(HttpTransport, ssl_context=tls_client)
            ),
        )
    assert result.errors == {"http_status": 2}


async def test_a_401_over_socks_is_the_targets(tmp_path):
    async def origin(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        await Gateway(status=401, body=b"login").respond(writer)

    async with server(origin) as origin_port, server(Gateway("socks5")) as port:
        targets = TargetRegistry()
        targets.register(EchoIpTarget("generic_http", f"http://localhost:{origin_port}/", True))
        result = await measure(
            RunConfig("generic_http", ProxyConfig("socks5", "127.0.0.1", port), requests=1),
            JsonSink(tmp_path),
            RunOptions(targets=targets),
        )
    assert result.errors == {"http_status": 1}


async def test_a_site_targets_401_over_plain_http_is_the_targets(tmp_path):
    class Site:
        id, version, engine, pool_eligible = "site", "1", "http", False

        def build_request(self):
            return RequestSpec("http://example.test/account")

        def assess(self, status, body):
            return Assessment(error="http_status")

    targets = TargetRegistry()
    targets.register(Site())
    async with server(Gateway(status=401, body=b"login")) as port:
        result = await measure(
            RunConfig("site", ProxyConfig("http", "127.0.0.1", port), requests=1),
            JsonSink(tmp_path),
            RunOptions(targets=targets),
        )
    assert result.errors == {"http_status": 1}


async def test_gateway_401_pulses_pause_the_network_and_open_no_outage(database, db, tmp_path):
    """The incident: every pulse request of the network answered 401 by its gateway, in two
    regions. Before PROXY-AUTH-PAUSE-2 that was http_status: a pulse_down and no pause."""
    prepare(database, REGIONS)
    places = manager(tmp_path)
    await set_setting(db, "browser.settings_file", str(places.browser))
    (alpha,) = products(database, "alpha")
    result = await refused_401(tmp_path, requests=100)
    for country in ("DE", "GB"):
        rows = [(window(0), result.completed, result.completed, 300)]
        key = await run(tmp_path, database, "pulse", window(0), alpha, country, rows)
        with connect(database) as conn:
            conn.execute(
                "UPDATE run_buckets SET errors=%s,status_codes=%s,proxy_errors=%s,timeouts=0 "
                "WHERE run_key=%s",
                (Jsonb(result.errors), Jsonb(result.status_codes), result.proxy_errors, key),
            )
    assert (await evaluated(db, 0))["opened"] == 0
    assert tape(database, "pulse_down") == []
    assert await login_pause.login_pause(db, places.root, NOW) == {
        "paused": ["alpha"],
        "nothing_to_pause": [],
    }
    assert state(places) == (False, False, False)
    ((severity, acknowledged, data, message),) = query(database, PAUSES)
    assert (severity, acknowledged, data["reason"]) == ("major", False, "login refused")
    assert message.startswith(
        "alpha: the account credentials no longer work (HTTP 401 or 407 on 200 of 200 requests"
    )
