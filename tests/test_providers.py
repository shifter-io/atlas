"""The Shifter provider: flags, sessions, endpoints, refusals, config files, the registry and
the CLI.

Runs through a loopback gateway are in `test_provider_runs.py`; the shared credentials are in
`providers_cases.py`.
"""

import json

import pytest
from providers_cases import CONFIG, CONTEXT, ENV, shifter

from atlas import (
    GrammarProxy,
    ProviderRegistry,
    ProxyConfig,
    RequestContext,
    RunConfig,
    load_provider,
)
from atlas.cli import main
from atlas.providers import load_grammar


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

    monkeypatch.setattr("atlas.cli.run", fake_run)
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
    from atlas.models import LabelError  # here, so the base code fails the assertion above

    unset = str(LabelError("requested_country", "us", None))
    assert "--country 'us'" in unset and "sets no country parameter" in unset
