import gzip
import json
import pickle
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import trustme
from helpers import Gateway, server

from proxy_index import GeonodeProxy, JsonSink, RequestContext, RunConfig, load_provider, run
from proxy_index.onboarding import manifest as campaign
from proxy_index.targets import EchoIpTarget, TargetRegistry
from proxy_index.transport import HttpTransport

ROOT = Path(__file__).resolve().parents[1]
ENV = {"GEONODE_ACCOUNT": "fixture_account", "GEONODE_PASSWORD": "private-password"}
CONTEXT = RequestContext("run-one", 0, 0)


def proxy(**kwargs):
    return GeonodeProxy("fixture_account", "private-password", **kwargs)


def test_verified_gateway_grammar_and_provider_separation():
    p = proxy(parameters={"country": "US", "strict": True})
    assert p.resolve(CONTEXT).username == (
        "geonode_fixture_account-type-residential-country-us-strict-on"
    )
    assert p.port == 10000 and p.session_policy == "gateway"
    assert GeonodeProxy("geonode_fixture_account", "secret").account == "fixture_account"
    omitted = proxy(parameters={"strict": False}).resolve(CONTEXT).username
    assert omitted.endswith("type-residential-strict-off")
    assert proxy().resolve(CONTEXT).username.endswith("type-residential")
    assert proxy(parameters={"country": "de", "state": "berlin", "asn": "1234"}).resolve(
        CONTEXT,
    ).username.endswith("country-de-state-berlin-asn-1234")


@pytest.mark.parametrize("options", [
    {"parameters": {"strict": "true"}}, {"parameters": {"strict": 1}},
    {"parameters": {"country": "us-strict"}}, {"parameters": {"country": "USA"}},
    {"parameters": {"region": "berlin"}}, {"parameters": {"sid": "shifter"}},
    {"parameters": {"ttl": 600}}, {"parameters": {"state": "berlin"}},
    {"parameters": {"asn": 1234}},
    {"parameters": {"country": "us", "state": "ny", "city": "ny"}},
    {"parameters": {"session": "x" * 26}}, {"parameters": {"session": "x-country-us"}},
    {"parameters": {"lifetime": 2}}, {"parameters": {"lifetime": 1441}},
    {"parameters": {"country": "us", "asn": True}},
    {"session_policy": "rotate"}, {"session_policy": "fixed"},
    {"port": 9000, "session_policy": "per_request"},
    {"port": 9000, "parameters": {"lifetime": 10}},
    {"port": 10000, "parameters": {"session": "x"}, "session_policy": "rotate"},
    {"extra_flags": {"strict": True}}, {"parameters": []},
    {"port": True}, {"port": "9000"}, {"port": 0}, {"scheme": "https"},
    {"host": "host/path"},
])
def test_reject_invalid_flags_and_rotation_claims(options):
    with pytest.raises(ValueError):
        proxy(**options)


@pytest.mark.parametrize("policy,distinct", [("per_request", 4), ("per_worker", 2), ("per_run", 1)])
def test_spawn_safe_sessions_and_redaction(policy, distinct):
    p = proxy(parameters={"country": "us", "strict": True, "lifetime": 10}, session_policy=policy)
    restored = pickle.loads(pickle.dumps(p))
    names = [restored.resolve(RequestContext("one", i, i % 2)).username for i in range(4)]
    assert len(set(names)) == distinct
    assert all("-strict-on-session-" in name and name.endswith("-lifetime-10") for name in names)
    assert restored.resolve(RequestContext("two", 0, 0)).username != names[0]
    assert p.public() == restored.public()
    text = repr(p) + repr(p.resolve(CONTEXT)) + json.dumps(RunConfig("generic_http", p).public())
    assert "fixture_account" not in text and "private-password" not in text
    fixed = proxy(parameters={"session": "private_session"})
    assert "private_session" not in json.dumps(fixed.public())
    with pytest.raises(TypeError):
        p.parameters["country"] = "de"


def test_configs_and_future_campaign_preserve_all_hourly_country_jobs(tmp_path):
    p = load_provider(ROOT / "configs/geonode.toml", environ=ENV, parameters={"country": "us"})
    assert p.port == 10000 and p.parameters["strict"] is True
    config = ROOT / "configs/geonode-onboarding.toml"
    rotating = load_provider(config, environ=ENV, parameters={"country": "de"})
    assert rotating.port == 9000 and rotating.session_policy == "rotate"
    assert rotating.resolve(CONTEXT).username.endswith("country-de-strict-on")
    start = datetime(2027, 1, 1, tzinfo=UTC)
    path = campaign.create(config, "residential", campaign.timestamp(start), tmp_path,
                           now=start - timedelta(hours=1))
    data = campaign.load(path)
    assert data["provider"] == "geonode" and len(data["rounds"]) == 24
    assert "geonode/residential" in str(path)
    assert sum(map(len, data["regions"].values())) == 249


@pytest.mark.parametrize("tls", [False, True])
async def test_http_and_https_auth_and_timestamped_observations(tmp_path, tls):
    server_tls, client_tls = None, None
    if tls:
        ca = trustme.CA()
        server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ca.issue_cert("localhost").configure_cert(server_tls)
        client_tls = ssl.create_default_context()
        ca.configure_trust(client_tls)
    origin_headers = []

    async def origin(reader, writer):
        origin_headers.append(await reader.readuntil(b"\r\n\r\n"))
        await Gateway().respond(writer)

    expected = "geonode_fixture_account-type-residential-country-us-strict-on"
    gateway = Gateway(
        auth=lambda user, password: user == expected and password == "private-password",
    )
    observations = []

    async def observe(ip, timestamp):
        observations.append((ip, timestamp))

    async with server(origin, tls=server_tls) as origin_port, server(gateway) as port:
        p = proxy(host="localhost", port=port, parameters={"country": "us", "strict": True})
        name = "generic_https" if tls else "generic_http"
        targets = TargetRegistry()
        targets.register(EchoIpTarget(name, f"{'https' if tls else 'http'}://localhost:{origin_port}/",
                                      True))
        config = RunConfig(name, p, requests=12, concurrency=3, processes=1)
        sink = JsonSink(tmp_path)
        result = await run(config, sink, targets=targets, observation_consumer=observe,
                           transport_factory=lambda cfg: HttpTransport(cfg, ssl_context=client_tls))
    assert result.successes == 12 and result.unique_ips == 1
    assert len(observations) == 12 and all(t > 0 for _, t in observations)
    assert all(b"Proxy-Authorization" not in h for h in origin_headers)
    with gzip.open(sink.directory / "ips.jsonl.gz", "rt") as stream:
        assert all(json.loads(line)["ip"] == "8.8.8.8" for line in stream)
    summary = (sink.directory / "summary.json").read_text()
    assert "private-password" not in summary and "fixture_account" not in summary
