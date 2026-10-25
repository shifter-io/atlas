import json
import pickle
import re
import tomllib
from pathlib import Path

import pytest

from proxy_index import GrammarProxy, RequestContext, RunConfig, load_provider
from proxy_index.providers import NoCountryList, registry
from proxy_index.providers.grammar import Grammar, grammar_ids, load_grammar
from proxy_index.regions import load_countries
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
    "proxy-seller": ({"country": "gb", "session": 7}, "res.proxy-seller.com:10007",
                     "fixture1_c_GB_s_7", SECRET),
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
    if load_grammar(provider).session_mode != "port":  # A port is taken by request index.
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


def test_proxy_seller_targets_every_served_country_with_one_login(tmp_path):
    # One "API tool" login (PSL-4): `_c_<CC>` picks the country; a country Proxy-Seller does not
    # serve raises NoCountryList before traffic (census rounds record no_list).
    path = tmp_path / "credentials.toml"
    path.write_text(f'["proxy-seller"]\naccount = "fixture1"\npassword = "{SECRET}"\n')
    path.chmod(0o600)
    config = ROOT / "configs/proxy-seller.toml"
    seller = load_provider(config, credentials_file=path, parameters={"country": "gb"})
    resolved = seller.resolve(CONTEXT)
    assert (resolved.username, resolved.password, resolved.port) == (
        "fixture1_c_GB", SECRET, 10000)
    assert seller.public()["requested_country"] == "gb"
    assert pickle.loads(pickle.dumps(seller)).public() == seller.public()
    assert RunConfig("generic_http", seller, requested_country="gb").requested_country == "gb"
    for country in ("US", "tr", "ZA", "XK"):
        load_provider(config, credentials_file=path, parameters={"country": country})
    plain = load_provider(config, credentials_file=path)
    assert plain.resolve(CONTEXT).username == "fixture1"
    for country in ("kp", "AQ"):
        with pytest.raises(NoCountryList, match=f"^no proxy-seller exits in {country.upper()}$"):
            load_provider(config, credentials_file=path, parameters={"country": country})
    # PSL-3's per-country list logins are gone: extra keys are refused as before.
    path.write_text(f'["proxy-seller"]\naccount = "fixture1"\npassword = "{SECRET}"\n'
                    f'account_gb = "fixturegb"\npassword_gb = "{SECRET}-gb"\n')
    with pytest.raises(ValueError, match="must match the provider configuration"):
        load_provider(config, credentials_file=path, parameters={"country": "gb"})


def test_proxy_seller_rotates_ports_and_sticky_sessions_carry_their_slot():
    # Per request (the default) takes the next port of 10000-10999 and sends no session, so each
    # request gets a new exit; a port alone keeps no exit, so sticky sessions add `_s_<slot>`.
    seller = proxy("proxy-seller", parameters={"country": "us"})
    assert seller.session_policy == "per_request"
    resolved = [seller.resolve(RequestContext("run-one", i, i % 7)) for i in range(2500)]
    assert [r.port for r in resolved] == [10000 + i % 1000 for i in range(2500)]
    assert {r.username for r in resolved} == {"fixture1_c_US"}
    assert pickle.loads(pickle.dumps(seller)).resolve(RequestContext("run-one", 1999, 0)) == (
        resolved[1999])
    fixed = proxy("proxy-seller", parameters={"session": 7}).resolve(CONTEXT)
    assert (fixed.port, fixed.username) == (10007, "fixture1_s_7")
    worker = proxy("proxy-seller", session_policy="per_worker").resolve(CONTEXT)
    assert worker.username == f"fixture1_s_{worker.port - 10000}"
    assert proxy("proxy-seller", session_policy="gateway").resolve(CONTEXT).username == (
        "fixture1")
    with pytest.raises(ValueError, match="slot is set from the session slot"):
        proxy("proxy-seller", parameters={"slot": "7"})


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
    ("proxy-seller", {"parameters": {"country": "kp"}}),  # Not served (NoCountryList).
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


@pytest.mark.parametrize("provider", sorted(GOLDEN))
def test_every_country_code_loads_even_when_it_equals_a_flag_key(provider):
    # Oxylabs' cc/st and DataImpulse's cr are also ISO codes (Cocos Islands, Sao Tome, Costa
    # Rica); a census round refused its whole region over one such country.
    if "country" not in load_grammar(provider).flags:
        pytest.skip("no country flag")
    for code in load_countries():
        try:
            proxy(provider, parameters={"country": code.lower()})
        except NoCountryList:
            continue


def test_country_codes_equal_to_a_flag_key_render_in_the_country_slot():
    for code, username in (("st", "customer-fixture1-cc-ST"), ("cc", "customer-fixture1-cc-CC")):
        assert proxy("oxylabs", parameters={"country": code}).resolve(CONTEXT).username == username
    assert proxy("dataimpulse", parameters={"country": "cr"}).resolve(CONTEXT).username == (
        "fixture1__cr.cr")
    with pytest.raises(ValueError, match="ambiguous flag delimiter"):  # Other kinds still refuse.
        proxy("oxylabs", parameters={"country": "us", "city": "st"})


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
    {"session": {"flag": "session", "slot_flag": "session"}},  # Port grammars only.
    {"session": {"flag": "session", "no_session_policy": "per_request"}},
])
def test_grammar_schema_is_validated(change):
    data = tomllib.loads((ROOT / "src/proxy_index/providers/grammars/massive.toml").read_text())
    data.update(change)
    with pytest.raises(ValueError):
        Grammar.parse(data)


@pytest.mark.parametrize("flag,countries", [("country", ["us"]), ("region", ["US"])])
def test_served_countries_are_upper_case_iso_codes_of_a_country_flag(flag, countries):
    data = tomllib.loads((ROOT / "src/proxy_index/providers/grammars/massive.toml").read_text())
    data["flags"][flag]["countries"] = countries
    with pytest.raises(ValueError, match="countries must be ISO codes"):
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
