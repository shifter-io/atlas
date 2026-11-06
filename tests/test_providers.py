import asyncio
import json
import ssl
from dataclasses import replace
from pathlib import Path

import pytest
import trustme
from helpers import Gateway, server

from proxy_index import (
    GrammarProxy,
    JsonSink,
    ProviderRegistry,
    ProxyConfig,
    RequestContext,
    RunConfig,
    RunOptions,
    load_provider,
    run,
)
from proxy_index.cli import main
from proxy_index.providers import load_grammar
from proxy_index.targets import EchoIpTarget, TargetRegistry
from proxy_index.transport import HttpTransport

CONTEXT = RequestContext("run-one", 0, 0)
CONFIG = Path(__file__).parents[1] / "configs" / "shifter.toml"
ENV = {"SHIFTER_ACCOUNT": "fixture_33", "SHIFTER_PASSWORD": "private-password"}


def shifter(**kwargs):
    return GrammarProxy("shifter", "fixture_33", "private-password", **kwargs)


def test_all_flags_and_omissions():
    proxy = shifter(
        parameters={
            "country": "US",
            "region": "New South Wales",
            "city": "New York",
            "asn": "7922",
            "strict": True,
            "session": "custom-session_42",
            "ttl": 10,
        }
    )
    assert proxy.resolve(CONTEXT).username == (
        "customer-fixture_33-country-us-region-new_south_wales-city-new_york-"
        "asn-7922-strict-true-sid-custom-session_42-ttl-600"
    )
    rotating = shifter(parameters={"country": None, "strict": False})
    assert rotating.resolve(CONTEXT).username == "customer-fixture_33"
    assert rotating.public()["provider_config"]["effective_ttl_seconds"] is None
    assert shifter(parameters={"city": "Berlin"}).resolve(CONTEXT).username.endswith("city-berlin")
    fixed = shifter(parameters={"session": "example"})
    assert fixed.public()["provider_config"]["effective_ttl_seconds"] == 120
    assert "ttl-" not in fixed.resolve(CONTEXT).username
    assert GrammarProxy("shifter", "customer-fixture_33", "secret").account == "fixture_33"


@pytest.mark.parametrize(
    "policy,expected",
    [
        ("rotate", 1),
        ("fixed", 1),
        ("per_run", 1),
        ("per_worker", 2),
        ("per_request", 4),
    ],
)
def test_session_scope_and_run_isolation(policy, expected):
    params = {"session": "deliberately-shared"} if policy == "fixed" else {}
    proxy = shifter(parameters=params, session_policy=policy)
    usernames = [proxy.resolve(RequestContext("one", i, i % 2)).username for i in range(4)]
    assert len(set(usernames)) == expected
    assert usernames[0] == proxy.resolve(RequestContext("one", 0, 0)).username
    next_run = proxy.resolve(RequestContext("two", 0, 0)).username
    assert (next_run == usernames[0]) == (policy in ("rotate", "fixed"))


@pytest.mark.parametrize("gateway,host", load_grammar("shifter").hosts.items())
def test_endpoints(gateway, host):
    resolved = shifter(gateway=gateway, scheme="socks5").resolve(CONTEXT)
    assert (resolved.scheme, resolved.host, resolved.port) == ("socks5", host, 443)


@pytest.mark.parametrize(
    "options",
    [
        {"parameters": {"unknown": 1}},
        {"parameters": {"ttl": 10}},
        {"parameters": {"ttl": 0}, "session_policy": "per_run"},
        {"parameters": {"ttl": True}, "session_policy": "per_run"},
        {"parameters": {"session": "example"}, "session_policy": "rotate"},
        {"parameters": {"session": "example"}, "session_policy": "per_request"},
        {"session_policy": "fixed"},
        {"session_policy": "unknown"},
        {"parameters": {"strict": "false"}},
        {"parameters": {"strict": 1}},
        {"parameters": {"country": "USA"}},
        {"parameters": {"country": 1}},
        {"parameters": {"country": "us-sid-injected"}},
        {"parameters": {"session": "session-country-us"}},
        {"parameters": {"session": "country-us"}},
        {"parameters": {"session": "session-country", "ttl": 10}},
        {"parameters": {"session": "{SESSION}"}},
        {"parameters": {"session": ""}},
        {"parameters": {"asn": 0}},
        {"parameters": {"asn": 4294967296}},
        {"parameters": {"asn": "AS7922"}},
        {"parameters": {"asn": 1.5}},
        {"parameters": {"city": "new\nyork-sid-injected"}},
        {"scheme": "https"},
        {"gateway": "legacy"},
        {"pool": "unknown"},
        {"pool": "non_geo", "parameters": {"country": "us"}},
        {"pool": "country_geo", "parameters": {"asn": 7922}},
        {"parameters": []},
        {"extra_flags": []},
        {"extra_flags": {"country": "us"}},
        {"extra_flags": {"password": "secret"}},
        {"extra_flags": {"bad-flag": "value"}},
        {"extra_flags": {"future": "x-sid-y"}},
        {"extra_flags": {"future": {}}},
        {"extra_flags": {"future": "x"}, "parameters": {"session": "x-future-y"}},
        {"extra_flags": {"future": "x"}, "parameters": {"session": "future-y"}},
        {"extra_flags": {"future": "x"}, "parameters": {"session": "y-future"}},
        {"scheme": "socks5", "parameters": {"session": "a" * 256}},
    ],
)
def test_invalid_configuration_fails_before_traffic(options):
    with pytest.raises(ValueError):
        shifter(**options)


@pytest.mark.parametrize(
    "account,password,scheme",
    [
        ("", "secret", "http"),
        (None, "secret", "http"),
        ("fixture-country-us", "secret", "http"),
        ("fixture", "", "http"),
        ("fixture", "non-ascii-é", "socks5"),
        ("fixture", "x" * 256, "socks5"),
    ],
)
def test_bad_credentials_do_not_echo_secrets(account, password, scheme):
    with pytest.raises(ValueError) as exc:
        GrammarProxy("shifter", account, password, scheme=scheme)
    if password:
        assert password not in str(exc.value)


def test_immutable_inputs_future_flags_and_safe_metadata():
    params = {"country": "US", "session": "private-session"}
    extras = {"future": "privatevalue", "enabled": True, "limit": 3, "absent": None}
    proxy = shifter(parameters=params, extra_flags=extras, pool="full_geo")
    params["country"] = "de"
    extras["future"] = "changed"
    with pytest.raises(TypeError):
        proxy.parameters["country"] = "de"
    username = proxy.resolve(CONTEXT).username
    assert "country-us" in username and "future-privatevalue" in username
    assert "enabled-true" in username and "limit-3" in username and "absent" not in username
    config = RunConfig("generic_http", proxy)
    assert config.provider == "shifter" and config.requested_country == "us"
    public = config.public()
    public["provider_config"]["parameters"]["country"] = "de"
    assert proxy.parameters["country"] == "us"
    text = json.dumps(config.public()) + repr(proxy) + repr(config) + repr(proxy.resolve(CONTEXT))
    for private in ("fixture_33", "private-password", "private-session", "privatevalue"):
        assert private not in text
    for labels in ({"provider": "wrong"}, {"product": "wrong"}, {"requested_country": "de"}):
        with pytest.raises(ValueError, match="conflicts"):
            RunConfig("generic_http", proxy, **labels)


def test_toml_and_per_test_overrides(tmp_path):
    proxy = load_provider(
        CONFIG, environ=ENV, parameters={"country": "us", "ttl": 10}, session_policy="per_worker"
    )
    assert proxy.session_policy == "per_worker" and proxy.parameters["ttl"] == 10
    assert load_provider(CONFIG, environ=ENV).parameters == {}
    path = tmp_path / "provider.toml"
    path.write_text(CONFIG.read_text().replace('# country = "us"', 'country = "us"'))
    omitted = load_provider(path, environ=ENV, parameters={"country": None})
    assert "country" not in omitted.parameters
    with pytest.raises(ValueError, match="missing"):
        load_provider(CONFIG, environ={})


@pytest.mark.parametrize(
    "change",
    [
        ("schema_version = 1", "schema_version = 2"),
        ("schema_version = 1", "schema_version = true"),
        ("schema_version = 1", "schema_version = 1\nunknown = true"),
        ("[credentials]", "credentials = []\n[unused]"),
        ('account_env = "SHIFTER_ACCOUNT"', 'account = "raw-account"'),
        ('account_env = "SHIFTER_ACCOUNT"', 'account_env = "bad env"'),
        ("[settings]", '[settings]\npassword = "raw-password"'),
        ("[settings]", '[settings]\naccount = "raw-account"'),
        ('account_env = "SHIFTER_ACCOUNT"', 'session_policy_env = "SHIFTER_ACCOUNT"'),
    ],
)
def test_invalid_config_files(tmp_path, change):
    path = tmp_path / "invalid.toml"
    path.write_text(CONFIG.read_text().replace(*change))
    with pytest.raises(ValueError):
        load_provider(path, environ=ENV)


def test_registry_extends_without_changes_to_targets(tmp_path):
    providers = ProviderRegistry()

    def other_provider(account, password, parameters, extra_flags):
        # The next provider can own completely different keys and formatting.
        return ProxyConfig("http", "example.test", 8080, account + parameters["custom"], password)

    providers.register("other", other_provider)
    path = tmp_path / "other.toml"
    path.write_text(
        CONFIG.read_text()
        .replace('provider = "shifter"', 'provider = "other"')
        .replace('scheme = "http"', '# scheme = "http"')
        .replace('gateway = "auto"', '# gateway = "auto"')
    )
    assert (
        load_provider(
            path, environ=ENV, providers=providers, parameters={"custom": "value"}
        ).username
        == "fixture_33value"
    )
    assert providers.all() == ("other",)
    with pytest.raises(ValueError):
        providers.register("other", other_provider)
    with pytest.raises(ValueError):
        providers.create("missing")


def test_cli_provider_overrides_and_secret_redaction(monkeypatch, capsys):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    observed = []

    async def fake_run(config, sink, options):
        observed.append(config)

        class Summary:
            status = "completed"

            def to_dict(self):
                return config.public()

        return Summary()

    monkeypatch.setattr("proxy_index.cli.run", fake_run)
    assert main(["providers"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 18 and {"shifter", "geonode", "soax"} <= set(listed)
    for target in ("generic_http", "generic_https"):
        assert (
            main(
                [
                    "run",
                    "--target",
                    target,
                    "--provider-config",
                    str(CONFIG),
                    "--param",
                    "country=US",
                    "--param",
                    "strict=true",
                    "--param",
                    "ttl=10",
                    "--session-policy",
                    "per_request",
                ]
            )
            == 0
        )
        text = capsys.readouterr()
        assert "private-password" not in text.out + text.err
        assert observed[-1].proxy.parameters == {"country": "us", "strict": True, "ttl": 10}
    for args in (
        ["--param", "country=us"],
        ["--provider-config", str(CONFIG), "--param", "bad"],
        ["--provider-config", str(CONFIG), "--param", "country=us", "--param", "country=de"],
    ):
        assert main(["run", "--target", "generic_http", *args]) == 2
    capsys.readouterr()
    # A country label that differs from the provider's parameter names both and the fix.
    label = ["--provider-config", str(CONFIG), "--param", "country=us", "--country", "US"]
    assert main(["run", "--target", "generic_http", *label]) == 2
    error = capsys.readouterr().err
    assert "--country 'US'" in error and "use --country us" in error
    assert "ValueError" not in error and "private-password" not in error
    from proxy_index.models import LabelError  # here, so the base code fails the assertion above

    unset = str(LabelError("requested_country", "us", None))
    assert "--country 'us'" in unset and "sets no country parameter" in unset


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
