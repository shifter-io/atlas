import asyncio
import itertools
import json
import pickle
import re
import tomllib
from pathlib import Path

import pytest

from proxy_index import GrammarProxy, RequestContext, RunConfig, load_provider
from proxy_index.providers import NoCountryList, registry
from proxy_index.providers.grammar import Grammar, grammar_ids, load_grammar
from proxy_index.storage import ADAPTER

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = RequestContext("run-one", 0, 0)
SECRET = "private-password"


def proxy(provider, account="fixture1", **kwargs):
    return GrammarProxy(provider, None if provider == "soax" else account, SECRET, **kwargs)


# Formats verified against provider documentation and live probes (2026-10-22).
GOLDEN = {
    "byteful": ({"country": "US", "session": "abc123", "ttl": 30},
                "residential.byteful.com:8000", "fixture1_c_us_s_abc123_ttl_30m", SECRET),
    "dataimpulse": ({"country": "us", "region": "new york", "session": "abc123", "ttl": 30},
                    "gw.dataimpulse.com:823",
                    "fixture1__cr.us;state.new_york;sessid.abc123;sessttl.30", SECRET),
    "decodo": ({"country": "us", "city": "New York", "session": "abc123", "ttl": 30},
               "gate.decodo.com:10000",
               "user-fixture1-country-us-city-new_york-session-abc123-sessionduration-30", SECRET),
    "evomi": ({"country": "us", "city": "New York", "session": "abc123", "ttl": 30},
              "premium-residential.evomi.com:1000", "fixture1",
              SECRET + "_country-US_city-new.york_session-abc123_lifetime-30"),
    "fleetproxy": ({"country": "US", "session": "abc123", "ttl": 30},
                   "residential-v2.fleetproxy.io:5000",
                   "fixture1-country-us-sid-abc123-ttl-30m", SECRET),
    "geonode": ({"country": "US", "strict": True, "session": "abc123", "ttl": 30},
                "proxy.geonode.io:10000",
                "geonode_fixture1-type-residential-country-us-strict-on-session-abc123-lifetime-30",
                SECRET),
    "infatica": ({"country": "us", "city": "New York", "session": "abc123", "ttl": 30},
                 "pool.infatica.io:10000", "fixture1_c_US_city_New-York_s_abc123_ttl_30m", SECRET),
    "iproyal": ({"country": "US", "session": "abcd1234", "ttl": 30}, "geo.iproyal.com:12321",
                "fixture1", SECRET + "_country-us_session-abcd1234_lifetime-30m"),
    "massive": ({"country": "us", "city": "New York", "session": "abc123", "ttl": 30},
                "network.joinmassive.com:65534",
                "fixture1-country-US-city-New York-session-abc123-sessionttl-30", SECRET),
    "nodemaven": ({"country": "US", "region": "california", "session": "abc123", "ttl": 30,
                   "filter": "medium"}, "gate.nodemaven.com:8080",
                  "fixture1-country-us-region-california-sid-abc123-ttl-30m-filter-medium",
                  SECRET),
    "oxylabs": ({"country": "us", "region": "california", "session": "abc123", "ttl": 30},
                "pr.oxylabs.io:7777",
                "customer-fixture1-cc-US-st-us_california-sessid-abc123-sesstime-30", SECRET),
    "proxy-cheap": ({"country": "US", "session": "12345678", "ttl": 10},
                    "thehub.proxy-cheap.com:8080", "fixture1",
                    SECRET + "_country-US_session-12345678_ttl-10"),
    "proxy-seller": ({"session": 7}, "res.proxy-seller.com:10007", "fixture1", SECRET),
    "proxyempire": ({"country": "US", "session": "abc123", "ttl": 30}, "v2.proxyempire.io:5000",
                    "fixture1-country-us-sid-abc123-ttl-30m", SECRET),
    "rayobyte": ({"country": "us", "session": "abc123", "ttl": 30},
                 "us-east.gw.rayobyte.com:8000", "fixture1",
                 SECRET + "-country-US-session-abc123-duration-30"),
    "shifter": ({"country": "US", "strict": True, "session": "abc-123", "ttl": 2},
                "p.shifter.io:443", "customer-fixture1-country-us-strict-true-sid-abc-123-ttl-120",
                SECRET),
    "soax": ({"country": "US", "network": "res_mob", "session": "abc_123", "ttl": 5},
             "proxy.soax.com:1337",
             "country-us-network-res_mob-session-abc_123-rotate-timed_300", SECRET),
    "webshare": ({"country": "US", "session": "1234"}, "p.webshare.io:80", "fixture1-us-1234",
                 SECRET),
}


def test_every_grammar_has_a_golden_case_config_and_registry_entry():
    assert set(GOLDEN) == set(grammar_ids()) == set(registry.all())
    for provider in grammar_ids():
        data = tomllib.loads((ROOT / f"configs/{provider}.toml").read_text())
        assert data["provider"] == provider and "account" not in json.dumps(data["settings"])
        grammar = load_grammar(provider)
        assert ("account_env" in data["credentials"]) == grammar.account_required


@pytest.mark.parametrize("provider", sorted(GOLDEN))
def test_golden_credentials(provider):
    params, endpoint, username, password = GOLDEN[provider]
    resolved = proxy(provider, parameters=params).resolve(CONTEXT)
    assert (f"{resolved.host}:{resolved.port}", resolved.username, resolved.password) == (
        endpoint, username, password,
    )


@pytest.mark.parametrize("provider", sorted(GOLDEN))
def test_generated_sessions_are_valid_scoped_and_pickle_safe(provider):
    params = {k: v for k, v in GOLDEN[provider][0].items() if k != "session"}
    p = proxy(provider, parameters=params, session_policy="per_request")
    restored = pickle.loads(pickle.dumps(p))
    configs = [restored.resolve(RequestContext("one", i, 0)) for i in range(8)]
    assert len({(c.username, c.password, c.port) for c in configs}) == 8
    assert restored.resolve(RequestContext("one", 3, 0)) == p.resolve(RequestContext("one", 3, 0))
    assert restored.resolve(RequestContext("two", 0, 0)) != configs[0]
    assert p.public() == restored.public()
    worker = proxy(provider, parameters=params, session_policy="per_worker")
    assert len({worker.resolve(RequestContext("one", i, i % 2)) for i in range(6)}) == 2


@pytest.mark.parametrize("provider", sorted(GOLDEN))
def test_public_metadata_matches_storage_and_redacts(provider):
    p = proxy(provider, parameters=GOLDEN[provider][0])
    public = RunConfig("generic_http", p).public()
    adapter = public["provider_config"]
    assert set(adapter) == {*ADAPTER, "gateway"} and set(adapter["gateway"]) == {"host", "port"}
    assert adapter["adapter"] == provider and public["provider"] == provider
    ttl = adapter["effective_ttl_seconds"]
    assert adapter["session_policy"] == "fixed" and (ttl is None or ttl > 0)
    text = json.dumps(public) + repr(p) + repr(p.resolve(CONTEXT))
    for private in ("fixture1", SECRET, "abc123", "abcd1234", "12345678", "abc_123"):
        assert private not in text


@pytest.mark.parametrize("provider", sorted(GOLDEN))
def test_rotation_never_sends_a_session(provider):
    params = {k: v for k, v in GOLDEN[provider][0].items() if k not in ("session", "ttl")}
    generated = load_grammar(provider).no_session_policy not in ("rotate", "gateway")
    p = proxy(provider, parameters=params, session_policy="gateway" if generated else None)
    first = p.resolve(RequestContext("one", 0, 0))
    assert all(p.resolve(RequestContext("one", i, 0)) == first for i in range(4))
    key = load_grammar(provider).flags.get("session")
    if key is not None and key.format != "{}":
        rendered = key.format.split("{}")[0]
        assert rendered not in f"{first.username} {first.password}"


def test_proxy_cheap_renders_the_dashboard_credential_shape():
    # The dashboard generator emits {password}_country-US_session-<8 digits>_ttl-<n>.
    rotating = proxy("proxy-cheap", parameters={"country": "us"})
    assert rotating.resolve(CONTEXT).password == SECRET + "_country-US"
    sessions = proxy("proxy-cheap", parameters={"country": "us", "ttl": 10},
                     session_policy="per_request")
    passwords = {sessions.resolve(RequestContext("run-one", i, 0)).password
                 for i in range(100)}
    assert len(passwords) == 100
    shape = re.escape(SECRET) + r"_country-US_session-[1-9][0-9]{7}_ttl-10"
    assert all(re.fullmatch(shape, password) for password in passwords)


def test_proxy_seller_takes_its_country_from_the_list_login(tmp_path):
    # Proxy-Seller's geography is set on its dashboard list; the login carries none (any
    # suffix is answered 407). Census rounds and probes still pass a country: it must match
    # the list the config declares, and send the plain login.
    path = tmp_path / "credentials.toml"
    path.write_text(f'["proxy-seller"]\naccount = "fixture1"\npassword = "{SECRET}"\n')
    path.chmod(0o600)
    config = ROOT / "configs/proxy-seller.toml"
    seller = load_provider(config, credentials_file=path, parameters={"country": "US"})
    resolved = seller.resolve(CONTEXT)
    assert (resolved.password, resolved.port) == (SECRET, 10000)
    assert re.fullmatch(r"fixture1_s_[0-9a-f]{16}", resolved.username)
    assert seller.public()["requested_country"] == "us"
    assert pickle.loads(pickle.dumps(seller)).public() == seller.public()
    for proxy_source in (seller, load_provider(config, credentials_file=path)):
        assert RunConfig("generic_http", proxy_source,
                         requested_country="us").requested_country == "us"
    with pytest.raises(ValueError, match="country differs from the provider-side preset"):
        load_provider(config, credentials_file=path, parameters={"country": "gb"})
    with pytest.raises(ValueError, match="requested_country label conflicts"):
        RunConfig("generic_http", load_provider(config, credentials_file=path),
                  requested_country="gb")


def test_proxy_seller_takes_each_census_country_from_its_own_list_login(tmp_path):
    # One dashboard list (login) per country (PSL-3): a requested country takes its own login
    # and preset; the plain login stays the config's country; one without a list is refused.
    path = tmp_path / "credentials.toml"
    path.write_text(f'["proxy-seller"]\naccount = "fixture1"\npassword = "{SECRET}"\n'
                    f'account_gb = "fixturegb"\npassword_gb = "{SECRET}-gb"\n')
    path.chmod(0o600)
    config = ROOT / "configs/proxy-seller.toml"
    seller = load_provider(config, credentials_file=path, parameters={"country": "GB"})
    resolved = seller.resolve(CONTEXT)
    assert resolved.password == f"{SECRET}-gb"
    assert re.fullmatch(r"fixturegb_s_[0-9a-f]{16}", resolved.username)
    assert seller.public()["requested_country"] == "gb"
    assert pickle.loads(pickle.dumps(seller)).public() == seller.public()
    for parameters in ({"country": "us"}, {}):
        plain = load_provider(config, credentials_file=path, parameters=parameters)
        assert plain.resolve(CONTEXT).username.startswith("fixture1_")
        assert plain.resolve(CONTEXT).password == SECRET
    with pytest.raises(NoCountryList, match="^no proxy-seller list for DE$"):
        load_provider(config, credentials_file=path, parameters={"country": "de"})
    path.write_text(f'["proxy-seller"]\naccount = "fixture1"\npassword = "{SECRET}"\n'
                    'account_gb = "fixturegb"\n')
    with pytest.raises(ValueError, match="both account_<cc> and password_<cc>"):
        load_provider(config, credentials_file=path, parameters={"country": "gb"})
    # A provider whose country is a free flag has no per-country logins.
    path.write_text(f'[shifter]\naccount = "customer-a"\npassword = "{SECRET}"\n'
                    f'account_gb = "customer-b"\npassword_gb = "{SECRET}"\n')
    with pytest.raises(ValueError, match="need a preset country"):
        load_provider(ROOT / "configs/shifter.toml", credentials_file=path,
                      parameters={"country": "gb"})


def test_proxy_seller_cycles_its_ports_with_a_new_session_each_pass():
    # Each request takes the next port of 10000-10999; after the last port the cycle starts
    # again at the first with a new documented `_s_<id>` session. This is the default.
    seller = proxy("proxy-seller", parameters={"country": "us"}, presets={"country": "us"})
    assert seller.session_policy == "per_request"
    resolved = [seller.resolve(RequestContext("run-one", i, i % 7)) for i in range(2500)]
    assert [r.port for r in resolved] == [10000 + i % 1000 for i in range(2500)]
    sessions = [r.username for r in resolved]
    assert all(re.fullmatch(r"fixture1_s_[0-9a-f]{16}", name) for name in sessions)
    assert [len(set(sessions[i:i + 1000])) for i in (0, 1000, 2000)] == [1, 1, 1]
    assert len(set(sessions)) == 3
    assert seller.resolve(RequestContext("run-two", 0, 0)).username != sessions[0]
    assert pickle.loads(pickle.dumps(seller)).resolve(RequestContext("run-one", 1999, 0)) == (
        resolved[1999])
    fixed = proxy("proxy-seller", parameters={"session": 7}).resolve(CONTEXT)
    assert (fixed.port, fixed.username) == (10007, "fixture1")
    worker = proxy("proxy-seller", session_policy="per_worker").resolve(CONTEXT)
    assert worker.username == "fixture1"
    with pytest.raises(ValueError, match="generated for each pass"):
        proxy("proxy-seller", parameters={"cycle": "abc"})


async def test_proxy_seller_census_cycles_ports_until_half_the_requests_are_duplicates(tmp_path):
    # A sticky list: each port keeps its exit whatever the session. The census rule (stop once
    # completed > 2 x unique, from 1,000 completions) runs a full pass over the 1,000 ports, a
    # second pass with a new session, and stops once half the requests repeat (at 2,001, the
    # first request of the third pass; what is in flight then finishes).
    from proxy_index import JsonSink, run
    from proxy_index.models import Outcome

    seen = []

    class StickyList:
        def __init__(self, config):
            self.proxy = config.proxy

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, target, *, context=None):
            resolved = self.proxy.resolve(context)
            seen.append((context.request_index, resolved.port, resolved.username))
            await asyncio.sleep(0)
            return Outcome(5.0, ip=f"10.0.{resolved.port % 1000 // 250}.{resolved.port % 250}",
                           status=200)

    seller = proxy("proxy-seller", parameters={"country": "us"}, presets={"country": "us"})
    result = await run(RunConfig("generic_http", seller, requests=None, concurrency=50,
                                 stop_rule="unique_ratio", requested_country="us"),
                       JsonSink(tmp_path), transport_factory=StickyList)
    assert result.stop_reason == "unique_ratio" and result.unique_ips == 1000
    assert 2000 < result.completed <= 2050
    seen.sort()
    assert [port for _, port, _ in seen] == [10000 + i % 1000 for i in range(len(seen))]
    passes = [len(list(group)) for _, group in itertools.groupby(name for *_, name in seen)]
    assert passes == [1000, 1000, len(seen) - 2000]  # A new session for every pass.


@pytest.mark.parametrize("provider,options", [
    ("soax", {}),  # SOAX needs at least one targeting flag.
    ("soax", {"parameters": {"country": "us", "session": "has-dash"}}),
    ("proxy-cheap", {"parameters": {"session": "12345678"}}),  # Sessions need a ttl.
    ("proxy-cheap", {"session_policy": "per_request"}),
    ("proxy-cheap", {"parameters": {"session": "abcd1234", "ttl": 30}}),
    ("iproyal", {"parameters": {"session": "short"}}),
    ("evomi", {"parameters": {"session": "abc12"}}),
    ("nodemaven", {"parameters": {"city": "miami"}}),  # City requires region.
    ("nodemaven", {"parameters": {"country": "us", "zip": "33101", "region": "florida"}}),
    ("decodo", {"parameters": {"country": "us", "city": "miami", "asn": 7922}}),
    ("webshare", {"parameters": {"session": "0123"}}),
    ("webshare", {"parameters": {"region": "texas"}}),
    ("proxy-seller", {"parameters": {"session": 1000}}),
    ("proxy-seller", {"parameters": {"country": "us"}}),  # No list country declared.
    ("proxy-seller", {"presets": {"city": "x"}}),
    ("proxy-seller", {"presets": {"country": "usa"}}),
    ("massive", {"presets": {"country": "us"}}),
    ("proxy-seller", {"session_policy": "rotate"}),
    ("rayobyte", {"parameters": {"ttl": 61}, "session_policy": "per_run"}),
    ("massive", {"parameters": {"city": "New York"}}),  # Geo keys require country.
    ("massive", {"parameters": {"ttl": 10}}),  # TTL needs a session.
    ("geonode", {"port": 9000, "session_policy": "per_request"}),
    ("shifter", {"parameters": {"session": "x-country-us"}}),
    ("shifter", {"parameters": {"session": "ttl", "ttl": 10}}),  # Value equal to a flag key.
    ("shifter", {"account": "country"}),
    ("byteful", {"scheme": "socks5"}),
    ("oxylabs", {"gateway": "fra"}),
    ("infatica", {"host": "evil.example"}),
    ("dataimpulse", {"parameters": {"country": "us;sessid.x"}}),
    ("dataimpulse", {"extra_flags": {"anon": 1}}),
    ("byteful", {"parameters": {"country": "us", "city": "x s evil1"}}),  # Hidden session.
    ("byteful", {"parameters": {"country": "us", "region": "x c de"}}),
    ("iproyal", {"parameters": {"country": "us", "city": "new_york"}}),
    ("decodo", {"session_policy": "gateway"}),
    ("webshare", {"session_policy": "gateway"}),
])
def test_invalid_configuration_fails_before_traffic(provider, options):
    with pytest.raises(ValueError) as exc:
        proxy(provider, **options)
    assert SECRET not in str(exc.value)


def test_provider_specific_mechanics():
    webshare = proxy("webshare", parameters={"country": "us"})
    assert webshare.resolve(CONTEXT).username == "fixture1-us-rotate"
    assert proxy("decodo").resolve(CONTEXT).username == "fixture1"
    seller = proxy("proxy-seller", session_policy="per_request")
    ports = {seller.resolve(RequestContext("one", i, 0)).port for i in range(50)}
    assert len(ports) > 40 and all(10000 <= port <= 10999 for port in ports)
    assert proxy("proxy-seller").session_policy == "per_request"
    assert proxy("proxy-seller", session_policy="gateway").resolve(CONTEXT).port == 10000
    with pytest.raises(ValueError):
        GrammarProxy("soax", "unexpected", SECRET, parameters={"country": "us"})
    assert proxy("byteful", parameters={"country": "us", "city": "new york"}).resolve(
        CONTEXT).username == "fixture1_c_us_city_new_york"
    assert proxy("iproyal", parameters={"country": "us", "city": "New York"}).resolve(
        CONTEXT).password.endswith("_city-newyork")
    assert GrammarProxy("iproyal", "shop_session", SECRET).account == "shop_session"
    rotating = proxy("geonode", port=9000, session_policy="rotate")
    assert rotating.resolve(CONTEXT).username == "geonode_fixture1-type-residential"
    assert proxy("shifter", gateway="fra").resolve(CONTEXT).host == "fra.p.shifter.io"
    assert proxy("rayobyte", gateway="la").resolve(CONTEXT).host == "la.residential.rayobyte.com"


@pytest.mark.parametrize("provider", sorted(GOLDEN))
def test_run_configs_load_with_private_credentials(provider, tmp_path):
    grammar = load_grammar(provider)
    table = {"password": SECRET} | ({"account": "fixture1"} if grammar.account_required else {})
    path = tmp_path / "credentials.toml"
    lines = [f'["{provider}"]'] + [f'{key} = "{value}"' for key, value in table.items()]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    config = ROOT / f"configs/{provider}.toml"
    loaded = load_provider(config, credentials_file=path)
    assert loaded.id == provider
    # Every commented example in the public config must validate once uncommented.
    examples = {}
    section = None
    for line in config.read_text().splitlines():
        if line.startswith("["):
            section = line
        elif section == "[parameters]" and (match := re.match(r'# (\w+) = ("[^"]*"|\S+)', line)):
            examples[match[1]] = tomllib.loads(f"v = {match[2]}")["v"]
    for key, value in examples.items():
        params = {"country": "us"} if "country" in grammar.flags else {}
        for needed in grammar.flags[key].requires if key in grammar.flags else ():
            params[needed] = examples[needed]
        params[key] = value
        if key == grammar.ttl or (key in grammar.flags and grammar.flags[key].requires_session):
            load_provider(config, credentials_file=path, parameters=params,
                          session_policy="per_request")
        else:
            load_provider(config, credentials_file=path, parameters=params)


@pytest.mark.parametrize("change", [
    {"schema_version": 2}, {"unknown": True}, {"credential": {"flags_in": "port"}},
    {"flags": {"country": {"kind": "unknown", "format": "c-{}"}}},
    {"flags": {"region": {"kind": "name", "format": "r-{}", "requires": ["nothing"]}}},
    {"session": {"flag": "session", "generate": "emoji"}},
    {"gateway": {"host": "bad/host", "ports": {"http": 1}}},
    {"gateway": {"host": "ok.example", "ports": {"https": 1}}},
    {"gateway": {"host": "ok.example"}}, {"credential": {"flags_in": "username", "typo": 1}},
    {"flags": {"country": {"kind": "country", "format": "c-{}", "require_session": True}}},
    {"session": {"flag": "session", "ttl_flag": "missing"}},
    {"session": {"flag": "missing"}}, {"session": {"flag": "session", "length": 0}},
    {"id": None},
    {"flags": {"country": {"kind": "token", "format": "c-{}", "pattern": "["}}},
    {"session": {"flag": "session", "generate": "alnum", "length": 300}},
    {"flags": {"country": {"kind": "country", "format": "c-{}", "preset": True}}},
    {"session": {"flag": "session", "cycle_flag": "session"}},  # Port grammars only.
    {"session": {"flag": "session", "no_session_policy": "per_request"}},
])
def test_grammar_schema_is_validated(change):
    data = tomllib.loads((ROOT / "src/proxy_index/providers/grammars/massive.toml").read_text())
    data.update(change)
    with pytest.raises(ValueError):
        Grammar.parse(data)


def test_requested_country_is_canonical_lowercase():
    for provider in ("massive", "oxylabs", "evomi", "infatica", "rayobyte", "proxy-cheap"):
        p = proxy(provider, parameters={"country": "us"})
        assert RunConfig("generic_http", p, requested_country="us").requested_country == "us"


def test_migrated_providers_keep_session_ids_ports_and_ttl_metadata():
    import hashlib
    seed = hashlib.sha256(b"one:per_request:3").hexdigest()
    shifter = proxy("shifter", session_policy="per_request").resolve(RequestContext("one", 3, 0))
    assert shifter.username == f"customer-fixture1-sid-{seed[:32]}"
    geonode = proxy("geonode", session_policy="per_request").resolve(RequestContext("one", 3, 0))
    assert geonode.username.endswith(f"-session-{seed[:25]}")
    socks = proxy("geonode", scheme="socks5")
    assert socks.port == 12000 and socks.session_policy == "gateway"
    lifetime = proxy("geonode", parameters={"ttl": 10})
    assert lifetime.public()["provider_config"]["effective_ttl_seconds"] == 600
    city = proxy("geonode", parameters={"country": "us", "city": "Los Angeles"})
    assert city.resolve(CONTEXT).username.endswith("country-us-city-losangeles")


def test_answer_classes_from_testing_table():
    from proxy_index.models import AnswerClasses
    from proxy_index.providers.grammar import answer_classes

    assert answer_classes("geonode") == AnswerClasses(("429", "469"), ("465",))
    assert answer_classes("dataimpulse") == AnswerClasses(("429", "proxy_connect"), ())
    assert answer_classes("soax") == AnswerClasses(("429",), ())
    data = tomllib.loads((ROOT / "src/proxy_index/providers/grammars/massive.toml").read_text())
    data["testing"] = {"rate_limit_statuses": [430, 599], "no_inventory_statuses": [400],
                       "rate_limit_connect": False}
    parsed = Grammar.parse(data)
    assert (parsed.rate_limit_statuses, parsed.no_inventory_statuses) == ((430, 599), (400,))
    assert parsed.rate_limit_connect is False


@pytest.mark.parametrize("testing", [
    {"rate_limit_statuses": [429]}, {"rate_limit_statuses": [399]},
    {"rate_limit_statuses": [600]}, {"rate_limit_statuses": [469, 469]},
    {"rate_limit_statuses": 469}, {"rate_limit_statuses": [True]},
    {"rate_limit_statuses": ["469"]}, {"no_inventory_statuses": [429]},
    {"rate_limit_statuses": [465], "no_inventory_statuses": [465]},
    {"rate_limit_connect": 1}, {"rate_limit_connect": "yes"}, {"rate_limits": [469]},
])
def test_testing_table_rejects_bad_answer_classes(testing):
    data = tomllib.loads((ROOT / "src/proxy_index/providers/grammars/massive.toml").read_text())
    data["testing"] = testing
    with pytest.raises(ValueError, match="Grammar|grammar"):
        Grammar.parse(data)


def test_geonode_campaign_concurrency_is_500():
    from proxy_index.campaign.planspec import network_concurrency

    assert load_grammar("geonode").max_concurrency == 500
    assert network_concurrency("geonode", 2000) == 500
    assert network_concurrency("geonode", 250) == 250
