"""Per-network test protocols: grammar setting, https-only upgrades and mixed redirects."""

import asyncio
import json
import ssl
import tomllib
from functools import partial
from pathlib import Path

import pytest
import trustme
from helpers import Gateway, Network, server

from proxy_index import GrammarProxy, JsonSink, ProxyConfig, RunConfig, run
from proxy_index.cli import main
from proxy_index.providers.grammar import (
    Grammar,
    grammar_ids,
    grammar_ids_by_protocol,
    load_grammar,
)
from proxy_index.targets import EchoIpTarget, RequestSpec, TargetRegistry, https_only
from proxy_index.transport import HttpTransport, _https_upgrade

ROOT = Path(__file__).resolve().parents[1]
HTTPS_ONLY = ("byteful", "infatica")
MIXED = ("fleetproxy", "proxy-cheap", "proxyempire")


def test_grammar_protocols_and_query():
    assert grammar_ids_by_protocol("https-only") == HTTPS_ONLY
    assert grammar_ids_by_protocol("mixed") == MIXED
    default = grammar_ids_by_protocol("default")
    assert set(default) == set(grammar_ids()) - set(HTTPS_ONLY) - set(MIXED)
    assert "shifter" in default and "geonode" in default
    with pytest.raises(ValueError, match="Unknown network test protocol"):
        grammar_ids_by_protocol("http-only")


@pytest.mark.parametrize("protocol,expected", [("https-only", HTTPS_ONLY), ("mixed", MIXED)])
def test_cli_lists_networks_by_protocol(capsys, protocol, expected):
    assert main(["providers", "--protocol", protocol]) == 0
    assert tuple(json.loads(capsys.readouterr().out)) == expected


def grammar_data(**testing):
    data = tomllib.loads((ROOT / "src/proxy_index/providers/grammars/byteful.toml").read_text())
    data.pop("testing", None)
    return data | ({"testing": testing} if testing else {})


def test_grammar_protocol_validation():
    assert Grammar.parse(grammar_data()).test_protocol == "default"
    assert Grammar.parse(grammar_data(protocol="mixed")).test_protocol == "mixed"
    with pytest.raises(ValueError, match="Unknown grammar test protocol"):
        Grammar.parse(grammar_data(protocol="http"))
    with pytest.raises(ValueError, match="Unknown keys in grammar table testing"):
        Grammar.parse(grammar_data(protocol="mixed", follow=True))


def test_only_non_default_networks_publish_their_protocol():
    byteful = GrammarProxy("byteful", "fixture1", "secret", parameters={"country": "us"})
    assert byteful.test_protocol == "https-only"
    assert byteful.public()["test_protocol"] == "https-only"
    assert RunConfig("generic_http", byteful).public()["test_protocol"] == "https-only"
    assert load_grammar("proxyempire").test_protocol == "mixed"
    shifter = GrammarProxy("shifter", "fixture1", "secret")
    assert shifter.test_protocol == "default"
    assert "test_protocol" not in shifter.public()
    assert "test_protocol" not in RunConfig("generic_http", shifter).public()


def test_run_config_rejects_an_unknown_protocol():
    with pytest.raises(ValueError, match="Unknown network test protocol"):
        RunConfig("generic_http", Network(ProxyConfig("http", "localhost", 8080), "sometimes"))


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://ip-info.com/myip", "https://ip-info.com/myip"),
        ("http://ip-info.com:80/myip?x=1", "https://ip-info.com/myip?x=1"),
        ("http://localhost:8443/", "https://localhost:8443/"),
        ("https://ip-info.com/myip", "https://ip-info.com/myip"),
    ],
)
def test_https_only_upgrades_echo_targets(url, expected):
    assert https_only(EchoIpTarget("generic_http", url)).url == expected


def test_https_only_refuses_other_plain_http_targets():
    class Custom:
        id, version, engine, pool_eligible = "custom", "1", "http", False

        def build_request(self):
            return RequestSpec("http://example.test/")

    with pytest.raises(ValueError, match="HTTPS tests only"):
        https_only(Custom())


@pytest.mark.parametrize(
    "status,location,expected",
    [
        (301, "https://ip-info.com/myip", "https://ip-info.com/myip"),
        (308, "HTTPS://ip-info.com/myip#top", "https://ip-info.com/myip"),
        (302, "https://ip-info.com:443/other", "https://ip-info.com:443/other"),
        (301, "http://ip-info.com/myip", None),
        (301, "https://elsewhere.test/myip", None),
        # Credentials refuse the upgrade; the upper-case host (urlsplit lowercases it, so it is
        # the same host) keeps this test data from reading as an e-mail address.
        (301, "https://user:pw@IP-INFO.COM/myip", None),
        (301, "/myip", None),
        (301, None, None),
        (200, "https://ip-info.com/myip", None),
        (None, "https://ip-info.com/myip", None),
        (301, "https://[::1", None),
    ],
)
def test_https_upgrade_follows_only_same_host_https_redirects(status, location, expected):
    assert _https_upgrade("http://ip-info.com/myip", status, location) == expected


def test_https_upgrade_never_follows_from_https():
    assert _https_upgrade("https://ip-info.com/myip", 301, "https://ip-info.com/myip") is None


def tls_pair():
    ca = trustme.CA()
    tls_server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert("localhost").configure_cert(tls_server)
    tls_client = ssl.create_default_context()
    ca.configure_trust(tls_client)
    return tls_server, tls_client


class Origin:
    """A TLS echo origin; records what reaches it."""

    def __init__(self, delay=0):
        self.requests = []
        self.delay = delay

    async def __call__(self, reader, writer):
        self.requests.append(await reader.readuntil(b"\r\n\r\n"))
        if self.delay:
            await asyncio.sleep(self.delay)
        await Gateway().respond(writer)


class RedirectingGateway(Gateway):
    """Answers every `every`-th plain-HTTP request with a 301 to `location`."""

    def __init__(self, location, every=1, **kwargs):
        super().__init__(auth=True, **kwargs)
        self.location = location
        self.every = every
        self.plain = 0

    async def respond(self, writer):
        self.plain += 1
        if self.plain % self.every:
            await super().respond(writer)
            return
        writer.write(
            f"HTTP/1.1 301 Moved\r\nLocation: {self.location}\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()


async def measure(tmp_path, gateway, origin, protocol, url, *, tls_client, **kwargs):
    async with server(gateway) as port:
        targets = TargetRegistry()
        targets.register(EchoIpTarget("generic_http", url(origin)))
        proxy = Network(ProxyConfig("http", "127.0.0.1", port, "test", "secret"), protocol)
        return await run(
            RunConfig("generic_http", proxy, **kwargs),
            JsonSink(tmp_path),
            targets=targets,
            transport_factory=partial(HttpTransport, ssl_context=tls_client),
        )


async def test_https_only_never_sends_plain_http(tmp_path):
    tls_server, tls_client = tls_pair()
    origin = Origin()
    gateway = Gateway(auth=True)
    async with server(origin, tls=tls_server) as origin_port:
        result = await measure(
            tmp_path,
            gateway,
            origin,
            "https-only",
            lambda _: f"http://localhost:{origin_port}/",
            tls_client=tls_client,
            requests=4,
            concurrency=2,
            processes=1,
        )
    assert result.successes == 4
    assert result.target["url"] == f"https://localhost:{origin_port}/"
    assert result.config["test_protocol"] == "https-only"
    assert "redirects" not in result.to_dict()
    # Every gateway request is a CONNECT tunnel; none carries a plain-HTTP absolute URI.
    assert len(gateway.requests) == 4
    assert all(r.startswith(b"CONNECT localhost:") for r in gateway.requests)
    assert all(b"Proxy-Authorization" not in r for r in origin.requests)


@pytest.mark.parametrize("processes", [1, 2])
async def test_mixed_follows_gateway_redirects_over_https(tmp_path, processes):
    tls_server, tls_client = tls_pair()
    origin = Origin()
    async with server(origin, tls=tls_server) as origin_port:
        gateway = RedirectingGateway(f"https://localhost:{origin_port}/", every=2)
        result = await measure(
            tmp_path,
            gateway,
            origin,
            "mixed",
            lambda _: f"http://localhost:{origin_port}/",
            tls_client=tls_client,
            requests=6,
            concurrency=1,
            processes=processes,
        )
    assert result.successes == 6
    assert result.redirects == 3
    assert result.to_dict()["redirects"] == 3
    # The followed request reports the HTTPS answer; the 301s stay out of the status codes.
    assert result.status_codes == {"200": 6}
    assert len(origin.requests) == 3
    assert all(b"Proxy-Authorization" not in r for r in origin.requests)
    plain = [r for r in gateway.requests if r.startswith(b"GET http://")]
    tunnels = [r for r in gateway.requests if r.startswith(b"CONNECT ")]
    assert (len(plain), len(tunnels)) == (6, 3)
    assert all(b"Proxy-Authorization: Basic" in r for r in tunnels)


async def test_mixed_counts_both_legs_of_traffic(tmp_path):
    tls_server, tls_client = tls_pair()
    async with server(Origin(), tls=tls_server) as origin_port:
        url = f"http://localhost:{origin_port}/"
        direct = await measure(
            tmp_path / "a",
            Gateway(auth=True),
            None,
            "mixed",
            lambda _: url,
            tls_client=tls_client,
            requests=1,
            processes=1,
        )
        followed = await measure(
            tmp_path / "b",
            RedirectingGateway(f"https://localhost:{origin_port}/"),
            None,
            "mixed",
            lambda _: url,
            tls_client=tls_client,
            requests=1,
            processes=1,
        )
    assert (direct.redirects, followed.redirects) == (0, 1)
    assert followed.bytes_up > direct.bytes_up and followed.bytes_down > direct.bytes_down


@pytest.mark.parametrize(
    "protocol,location",
    [
        ("default", "https://localhost:{port}/"),
        ("mixed", "https://elsewhere.test/"),
        ("mixed", "http://localhost:{port}/"),
    ],
)
async def test_redirects_are_not_followed_otherwise(tmp_path, protocol, location):
    tls_server, tls_client = tls_pair()
    origin = Origin()
    async with server(origin, tls=tls_server) as origin_port:
        gateway = RedirectingGateway(location.format(port=origin_port))
        result = await measure(
            tmp_path,
            gateway,
            origin,
            protocol,
            lambda _: f"http://localhost:{origin_port}/",
            tls_client=tls_client,
            requests=2,
            processes=1,
        )
    assert result.errors == {"http_status": 2}
    assert result.status_codes == {"301": 2}
    assert origin.requests == []
    assert result.to_dict().get("redirects") == (0 if protocol == "mixed" else None)


async def test_mixed_redirect_shares_one_timeout_budget(tmp_path):
    tls_server, tls_client = tls_pair()
    origin = Origin(delay=0.3)
    async with server(origin, tls=tls_server) as origin_port:
        gateway = RedirectingGateway(f"https://localhost:{origin_port}/", delay=0.05)
        result = await measure(
            tmp_path,
            gateway,
            origin,
            "mixed",
            lambda _: f"http://localhost:{origin_port}/",
            tls_client=tls_client,
            requests=1,
            processes=1,
            timeout_seconds=0.2,
        )
    assert result.errors == {"timeout": 1}
    assert result.redirects == 1
