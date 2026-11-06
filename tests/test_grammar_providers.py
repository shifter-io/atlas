"""Provider-specific grammar rules: Proxy-Cheap, Proxy-Seller, invalid configurations, the
per-provider mechanics and the migrated providers."""

import pickle
import re

import pytest
from grammars_cases import CONTEXT, ROOT, SECRET, proxy

from proxy_index import GrammarProxy, RequestContext, RunConfig, load_provider
from proxy_index.providers import NoCountryList


def test_proxy_cheap_renders_the_dashboard_credential_shape():
    # The dashboard generator emits {password}_country-US_session-<8 digits>_ttl-<n>.
    rotating = proxy("proxy-cheap", parameters={"country": "us"})
    assert rotating.resolve(CONTEXT).password == SECRET + "_country-US"
    sessions = proxy(
        "proxy-cheap", parameters={"country": "us", "ttl": 10}, session_policy="per_request"
    )
    passwords = {sessions.resolve(RequestContext("run-one", i, 0)).password for i in range(100)}
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
    assert (resolved.username, resolved.password, resolved.port) == ("fixture1_c_GB", SECRET, 10000)
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
    path.write_text(
        f'["proxy-seller"]\naccount = "fixture1"\npassword = "{SECRET}"\n'
        f'account_gb = "fixturegb"\npassword_gb = "{SECRET}-gb"\n'
    )
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
    assert (
        pickle.loads(pickle.dumps(seller)).resolve(RequestContext("run-one", 1999, 0))
        == (resolved[1999])
    )
    fixed = proxy("proxy-seller", parameters={"session": 7}).resolve(CONTEXT)
    assert (fixed.port, fixed.username) == (10007, "fixture1_s_7")
    worker = proxy("proxy-seller", session_policy="per_worker").resolve(CONTEXT)
    assert worker.username == f"fixture1_s_{worker.port - 10000}"
    assert proxy("proxy-seller", session_policy="gateway").resolve(CONTEXT).username == ("fixture1")
    with pytest.raises(ValueError, match="slot is set from the session slot"):
        proxy("proxy-seller", parameters={"slot": "7"})


@pytest.mark.parametrize(
    "provider,options",
    [
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
    ],
)
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
    assert (
        proxy("byteful", parameters={"country": "us", "city": "new york"}).resolve(CONTEXT).username
        == "fixture1_c_us_city_new_york"
    )
    assert (
        proxy("iproyal", parameters={"country": "us", "city": "New York"})
        .resolve(CONTEXT)
        .password.endswith("_city-newyork")
    )
    assert GrammarProxy("iproyal", "shop_session", SECRET).account == "shop_session"
    rotating = proxy("geonode", port=9000, session_policy="rotate")
    assert rotating.resolve(CONTEXT).username == "geonode_fixture1-type-residential"
    assert proxy("shifter", gateway="fra").resolve(CONTEXT).host == "fra.p.shifter.io"
    assert proxy("rayobyte", gateway="la").resolve(CONTEXT).host == "la.residential.rayobyte.com"


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
