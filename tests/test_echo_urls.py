"""Per-network IP-check URLs: the grammar's [testing] echo_http / echo_https (ECHO-HOST-1)."""

import tomllib
from functools import partial
from pathlib import Path

import pytest
from helpers import Gateway, Network, server
from test_network_protocol import Origin, tls_pair

from atlas import GrammarProxy, JsonSink, ProxyConfig, RunConfig, RunOptions, run
from atlas.providers.grammar import Grammar, grammar_ids, load_grammar
from atlas.targets import BrowserTarget, EchoIpTarget, network_echo, registry
from atlas.transport import HttpTransport

ROOT = Path(__file__).resolve().parents[1]
CHECKIP = {
    "generic_http": "http://checkip.amazonaws.com/",
    "generic_https": "https://checkip.amazonaws.com/",
}


def echo_network(proxy, echo_urls):
    """A loopback proxy source whose network has its own IP-check URLs (an importable class, so
    worker processes can rebuild it)."""
    network = Network(proxy, "default")
    network.echo_urls = echo_urls
    return network


def grammar_data(**testing):
    data = tomllib.loads((ROOT / "src/atlas/providers/grammars/shifter.toml").read_text())
    data.pop("testing", None)
    return data | ({"testing": testing} if testing else {})


def test_webshare_sets_checkip_and_every_other_network_sets_none():
    assert dict(load_grammar("webshare").echo_urls) == CHECKIP
    proxy = GrammarProxy("webshare", "fixture1", "secret", parameters={"country": "us"})
    assert dict(proxy.echo_urls) == CHECKIP
    # The run config the network publishes is unchanged: the URL shows in target.url only.
    assert "echo_urls" not in proxy.public()
    assert "echo_urls" not in RunConfig("generic_http", proxy).public()
    others = [p for p in grammar_ids() if p != "webshare"]
    assert others and all(dict(load_grammar(p).echo_urls) == {} for p in others)


def test_grammar_echo_url_validation():
    assert dict(Grammar.parse(grammar_data()).echo_urls) == {}
    parsed = Grammar.parse(grammar_data(echo_https="https://echo.example.test/ip?x=1"))
    assert dict(parsed.echo_urls) == {"generic_https": "https://echo.example.test/ip?x=1"}
    bad = (
        {"echo_http": "https://echo.example.test/"},
        {"echo_https": "http://echo.example.test/"},
        {"echo_http": "http://user:pw@echo.example.test/"},
        {"echo_http": "http://echo.example.test/#top"},
        {"echo_http": 5},
    )
    for testing in bad:
        with pytest.raises(ValueError, match="Grammar echo_https? must be a generic_https? URL"):
            Grammar.parse(grammar_data(**testing))
    with pytest.raises(ValueError, match="Unknown keys in grammar table testing"):
        Grammar.parse(grammar_data(echo_socks="http://echo.example.test/"))


def test_network_echo_moves_only_built_in_echo_targets_at_their_built_in_url():
    network = echo_network(ProxyConfig("http", "127.0.0.1", 8080), CHECKIP)
    for target_id in CHECKIP:
        built_in = registry.get(target_id)
        moved = network_echo(built_in, network)
        assert moved == EchoIpTarget(target_id, CHECKIP[target_id])
        assert (moved.id, moved.version, moved.pool_eligible) == (
            built_in.id,
            built_in.version,
            built_in.pool_eligible,
        )
        # Without echo URLs (or another network's adapter), the target is the same object.
        assert network_echo(built_in, ProxyConfig("http", "127.0.0.1", 8080)) is built_in
        assert network_echo(built_in, echo_network(network.proxy, {})) is built_in
    given = EchoIpTarget("generic_http", "http://echo.example.test/")
    assert network_echo(given, network) is given
    adhoc = EchoIpTarget("generic_http_b", "http://ip-info.com/myip")
    assert network_echo(adhoc, network) is adhoc
    page = BrowserTarget("generic_http", "https://www.example.test/")
    assert network_echo(page, network) is page


def test_the_checkip_answer_passes():
    target = EchoIpTarget("generic_http", CHECKIP["generic_http"])
    assessed = target.assess(200, b"203.0.113.8\n")
    assert (assessed.ip, assessed.error) == ("203.0.113.8", None)
    assert target.assess(200, b"2001:db8::8\n").ip == "2001:db8::8"
    assert target.assess(200, b"203.0.113.8\n203.0.113.9\n").error == "invalid_body"


async def plain_run(tmp_path, proxy_of, **kwargs):
    gateway = Gateway()
    async with server(gateway) as port:
        proxy = proxy_of(ProxyConfig("http", "127.0.0.1", port))
        result = await run(RunConfig("generic_http", proxy, **kwargs), JsonSink(tmp_path))
    return result, gateway.requests


@pytest.mark.parametrize("processes", [1, 2])
@pytest.mark.parametrize("given", [None, "http://ip-info.com/myip"])
async def test_plain_http_runs_at_the_network_url(tmp_path, processes, given):
    # `given` is how a daily manifest names the built-in URL; a pulse passes none.
    result, requests = await plain_run(
        tmp_path,
        lambda proxy: echo_network(proxy, CHECKIP),
        requests=4,
        concurrency=2,
        processes=processes,
        target_url=given,
    )
    assert result.successes == 4
    assert result.target["id"] == "generic_http"
    assert result.target["url"] == CHECKIP["generic_http"]
    assert len(requests) == 4
    assert all(r.startswith(b"GET http://checkip.amazonaws.com/ HTTP/1.1") for r in requests)


async def test_networks_without_echo_urls_run_as_before(tmp_path):
    plain, plain_requests = await plain_run(tmp_path / "a", lambda proxy: proxy, requests=2)
    empty, empty_requests = await plain_run(
        tmp_path / "b", lambda proxy: echo_network(proxy, {}), requests=2
    )
    for result, requests in ((plain, plain_requests), (empty, empty_requests)):
        assert result.successes == 2
        assert result.target["url"] == "http://ip-info.com/myip"
        assert all(r.startswith(b"GET http://ip-info.com/myip HTTP/1.1") for r in requests)
    # Byte for byte the same requests, whatever the gateway port.
    assert [r.split(b"\r\nHost")[0] for r in plain_requests] == [
        r.split(b"\r\nHost")[0] for r in empty_requests
    ]


async def test_a_url_given_with_the_run_is_kept(tmp_path):
    result, requests = await plain_run(
        tmp_path,
        lambda proxy: echo_network(proxy, CHECKIP),
        requests=2,
        target_url="http://echo.example.test/ip",
    )
    assert result.target["url"] == "http://echo.example.test/ip"
    assert all(r.startswith(b"GET http://echo.example.test/ip HTTP/1.1") for r in requests)


async def test_https_runs_at_the_network_url(tmp_path):
    tls_server, tls_client = tls_pair()
    origin = Origin()
    gateway = Gateway(auth=True)
    async with server(origin, tls=tls_server) as origin_port, server(gateway) as port:
        echo = {"generic_https": f"https://localhost:{origin_port}/"}
        proxy = echo_network(ProxyConfig("http", "127.0.0.1", port, "test", "secret"), echo)
        result = await run(
            RunConfig("generic_https", proxy, requests=3, concurrency=1, processes=1),
            JsonSink(tmp_path),
            RunOptions(transport_factory=partial(HttpTransport, ssl_context=tls_client)),
        )
    assert result.successes == 3
    assert result.target["id"] == "generic_https"
    assert result.target["url"] == echo["generic_https"]
    assert len(gateway.requests) == 3
    assert all(r.startswith(f"CONNECT localhost:{origin_port} ".encode()) for r in gateway.requests)
    assert len(origin.requests) == 3
