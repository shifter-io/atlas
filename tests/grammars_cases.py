"""Shared cases of the grammar tests: the fixture account, the proxy helper and the golden table."""

from pathlib import Path

from atlas import GrammarProxy, RequestContext

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = RequestContext("run-one", 0, 0)
SECRET = "private-password"


def proxy(provider, account="fixture1", **kwargs):
    return GrammarProxy(provider, None if provider == "soax" else account, SECRET, **kwargs)


# Formats verified against provider documentation and live probes (2026-10-22).
GOLDEN = {
    "byteful": (
        {"country": "US", "session": "abc123", "ttl": 30},
        "residential.byteful.com:8000",
        "fixture1_c_us_s_abc123_ttl_30m",
        SECRET,
    ),
    "dataimpulse": (
        {"country": "us", "region": "new york", "session": "abc123", "ttl": 30},
        "gw.dataimpulse.com:823",
        "fixture1__cr.us;state.new_york;sessid.abc123;sessttl.30",
        SECRET,
    ),
    "decodo": (
        {"country": "us", "city": "New York", "session": "abc123", "ttl": 30},
        "gate.decodo.com:10000",
        "user-fixture1-country-us-city-new_york-session-abc123-sessionduration-30",
        SECRET,
    ),
    "evomi": (
        {"country": "us", "city": "New York", "session": "abc123", "ttl": 30},
        "premium-residential.evomi.com:1000",
        "fixture1",
        SECRET + "_country-US_city-new.york_session-abc123_lifetime-30",
    ),
    "fleetproxy": (
        {"country": "US", "session": "abc123", "ttl": 30},
        "residential-v2.fleetproxy.io:5000",
        "fixture1-country-us-sid-abc123-ttl-30m",
        SECRET,
    ),
    "geonode": (
        {"country": "US", "strict": True, "session": "abc123", "ttl": 30},
        "proxy.geonode.io:10000",
        "geonode_fixture1-type-residential-country-us-strict-on-session-abc123-lifetime-30",
        SECRET,
    ),
    "infatica": (
        {"country": "us", "city": "New York", "session": "abc123", "ttl": 30},
        "pool.infatica.io:10000",
        "fixture1_c_US_city_New-York_s_abc123_ttl_30m",
        SECRET,
    ),
    "iproyal": (
        {"country": "US", "session": "abcd1234", "ttl": 30},
        "geo.iproyal.com:12321",
        "fixture1",
        SECRET + "_country-us_session-abcd1234_lifetime-30m",
    ),
    "massive": (
        {"country": "us", "city": "New York", "session": "abc123", "ttl": 30},
        "network.joinmassive.com:65534",
        "fixture1-country-US-city-New York-session-abc123-sessionttl-30",
        SECRET,
    ),
    "nodemaven": (
        {
            "country": "US",
            "region": "california",
            "session": "abc123",
            "ttl": 30,
            "filter": "medium",
        },
        "gate.nodemaven.com:8080",
        "fixture1-country-us-region-california-sid-abc123-ttl-30m-filter-medium",
        SECRET,
    ),
    "oxylabs": (
        {"country": "us", "region": "california", "session": "abc123", "ttl": 30},
        "pr.oxylabs.io:7777",
        "customer-fixture1-cc-US-st-us_california-sessid-abc123-sesstime-30",
        SECRET,
    ),
    "proxy-cheap": (
        {"country": "US", "session": "12345678", "ttl": 10},
        "thehub.proxy-cheap.com:8080",
        "fixture1",
        SECRET + "_country-US_session-12345678_ttl-10",
    ),
    "proxy-seller": (
        {"country": "gb", "session": 7},
        "res.proxy-seller.com:10007",
        "fixture1_c_GB_s_7",
        SECRET,
    ),
    "proxyempire": (
        {"country": "US", "session": "abc123", "ttl": 30},
        "v2.proxyempire.io:5000",
        "fixture1-country-us-sid-abc123-ttl-30m",
        SECRET,
    ),
    "rayobyte": (
        {"country": "us", "session": "abc123", "ttl": 30},
        "us-east.gw.rayobyte.com:8000",
        "fixture1",
        SECRET + "-country-US-session-abc123-duration-30",
    ),
    "shifter": (
        {"country": "US", "strict": True, "session": "abc-123", "ttl": 2},
        "p.shifter.io:443",
        "customer-fixture1-country-us-strict-true-sid-abc-123-ttl-120",
        SECRET,
    ),
    "soax": (
        {"country": "US", "network": "res_mob", "session": "abc_123", "ttl": 5},
        "proxy.soax.com:1337",
        "country-us-network-res_mob-session-abc_123-rotate-timed_300",
        SECRET,
    ),
    "webshare": (
        {"country": "US", "session": "1234"},
        "p.webshare.io:80",
        "fixture1-us-1234",
        SECRET,
    ),
}
