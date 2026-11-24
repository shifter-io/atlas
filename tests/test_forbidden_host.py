"""WEBSHARE-ECHO-2: a gateway's 403 refusing our account the IP-check host is ours, not the
network's failure: recorded as proxy_auth, so the login-refused rule (proxy_login.py) applies."""

import tomllib
from pathlib import Path

import pytest
from helpers import Gateway, Network, server

from atlas import GrammarProxy, JsonSink, ProxyConfig, RunConfig, RunOptions, proxy_login
from atlas import run as measure
from atlas.providers.grammar import Grammar, grammar_ids, load_grammar
from atlas.targets import EchoIpTarget, TargetRegistry

ROOT = Path(__file__).resolve().parents[1]
MARKER = ("x-webshare-reason", "forbidden_host")
ACCOUNT = "X-Webshare-Reason: client_connect_forbidden_host_account\r\n"
BODY = b"You are forbidden to connect to client_connect_forbidden_host_account"


def grammar_data(**testing):
    data = tomllib.loads((ROOT / "src/atlas/providers/grammars/shifter.toml").read_text())
    data.pop("testing", None)
    return data | ({"testing": testing} if testing else {})


def test_webshare_declares_its_forbidden_host_answer_and_no_other_network_does():
    assert load_grammar("webshare").forbidden_host == MARKER
    proxy = GrammarProxy("webshare", "fixture1", "secret", parameters={"country": "us"})
    assert proxy.forbidden_host == MARKER
    assert "forbidden_host" not in proxy.public()
    others = [p for p in grammar_ids() if p != "webshare"]
    assert others and all(load_grammar(p).forbidden_host is None for p in others)


def test_grammar_forbidden_host_validation():
    assert Grammar.parse(grammar_data()).forbidden_host is None
    parsed = Grammar.parse(grammar_data(forbidden_host={"header": "X-R", "contains": " Host "}))
    assert parsed.forbidden_host == ("x-r", "host")
    bad = (
        "x",
        {"header": "X-R"},
        {"header": "X-R", "contains": ""},
        {"header": "X R", "contains": "host"},
        {"header": "X-R", "contains": 5},
        {"header": "X-R", "contains": "host", "status": 403},
    )
    for value in bad:
        with pytest.raises(ValueError, match="Grammar forbidden_host must name a header"):
            Grammar.parse(grammar_data(forbidden_host=value))


@pytest.mark.parametrize(
    "reason", ["client_connect_forbidden_host", "client_connect_forbidden_host_account"]
)
def test_the_gateways_answer_matches_both_reasons(reason):
    pairs = [(b"Content-Length", b"0"), (b"x-webshare-reason", reason.encode())]
    assert proxy_login.forbidden_host(MARKER, "plain", 403, pairs)
    assert proxy_login.forbidden_host(MARKER, "connect", 403, {"X-Webshare-Reason": reason})


def test_other_answers_do_not_match():
    pairs = [("X-Webshare-Reason", "client_connect_forbidden_host_account")]
    assert not proxy_login.forbidden_host(None, "plain", 403, pairs)  # no marker declared
    assert not proxy_login.forbidden_host(MARKER, "tunnel", 403, pairs)  # the target's own 403
    assert not proxy_login.forbidden_host(MARKER, "plain", 404, pairs)
    assert not proxy_login.forbidden_host(MARKER, "plain", 403, [("X-Other", "forbidden_host")])
    assert not proxy_login.forbidden_host(MARKER, "plain", 403, None)
    other = [("X-Webshare-Reason", "client_connect_invalid_params")]
    assert not proxy_login.forbidden_host(MARKER, "plain", 403, other)


async def run_through(tmp_path, gateway, url, marker):
    targets = TargetRegistry()
    targets.register(EchoIpTarget("echo", url))
    async with server(gateway) as port:
        network = Network(ProxyConfig("http", "127.0.0.1", port, "u", "p"), "default")
        network.forbidden_host = marker
        config = RunConfig("echo", network, requests=3, concurrency=1)
        return await measure(config, JsonSink(tmp_path), RunOptions(targets=targets))


CASES = [
    (lambda: Gateway(status=403, body=BODY, headers=ACCOUNT), "http://localhost:9/", "http_status"),
    (
        lambda: Gateway(auth=lambda *_: False, refusal=403, headers=ACCOUNT),
        "https://localhost:9/",
        "proxy_connect",
    ),
]


@pytest.mark.parametrize(("gateway", "url", "before"), CASES)
async def test_a_forbidden_host_answer_is_refused_by_our_side(tmp_path, gateway, url, before):
    """Through a loopback gateway: its 403 with the declared reason, to a plain-HTTP request or
    to our CONNECT, is proxy_auth, so the run is mostly refused (not judged, no outage, the
    proxy_login_refused alert and the pause). Without the network's marker it stays as before."""
    summary = await run_through(tmp_path / "a", gateway(), url, MARKER)
    assert (summary.completed, summary.errors, summary.status_codes) == (
        3,
        {proxy_login.ERROR: 3},
        {"403": 3},
    )
    assert summary.proxy_errors == 3
    assert proxy_login.refused(summary.errors[proxy_login.ERROR], summary.completed)
    unmarked = await run_through(tmp_path / "b", gateway(), url, None)
    assert unmarked.errors == {before: 3}
