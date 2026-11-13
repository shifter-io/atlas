"""browser.geo: the per-country table both engines draw a try's place from."""

from __future__ import annotations

import ast
import random
from pathlib import Path

import pytest

from atlas.browser import chrome, geo
from atlas.browser.geo import GeoRefused
from atlas.errors import UserError

US_ZONES = ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles")


def test_the_us_entry():
    us = geo.country("US")
    assert (us.locale, us.languages) == ("en-US", ("en-US", "en"))
    assert us.accept_language == "en-US,en;q=0.9"
    assert tuple(us.zones) == US_ZONES
    assert geo.country("us") is us


def test_every_entry_is_whole_and_every_zone_has_one_city():
    zones = [zone for entry in geo.COUNTRIES.values() for zone in entry.zones]
    assert len(zones) == len(set(zones))
    for code, entry in geo.COUNTRIES.items():
        assert len(code) == 2 and code.isupper() and entry.languages[0] == entry.locale
        for latitude, longitude in entry.zones.values():
            assert -90 <= latitude <= 90 and -180 <= longitude <= 180
    assert geo.country("DE").accept_language == "de-DE,de;q=0.9,en;q=0.8"


def test_a_country_without_an_entry_is_refused():
    with pytest.raises(GeoRefused, match="'ZZ'") as caught:
        geo.country("ZZ")
    assert isinstance(caught.value, UserError)
    with pytest.raises(GeoRefused):
        geo.place(random.Random(0), "zz")
    with pytest.raises(GeoRefused, match="Mars/Olympus"):
        geo.geolocation("Mars/Olympus")


def test_the_place_is_drawn_from_the_seed():
    places = [geo.place(geo.seeded(f"run:{i}"), "us") for i in range(100)]
    assert {p.timezone_id for p in places} == set(US_ZONES)
    assert geo.place(geo.seeded("run:7"), "US") == places[7]
    assert all(p.locale == "en-US" and p.accept_language == "en-US,en;q=0.9" for p in places)


def test_camoufox_options_carry_the_languages_zone_and_city():
    place = geo.place(geo.seeded("run:0"), "US")
    latitude, longitude = geo.country("US").zones[place.timezone_id]
    assert place.camoufox_options() == {
        "locale": "en-US,en",
        "timezone_id": place.timezone_id,
        "geolocation": {"latitude": latitude, "longitude": longitude, "accuracy": geo.ACCURACY_M},
        "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
    }


def test_chrome_draws_from_the_shared_table():
    assert not hasattr(chrome, "COUNTRIES")
    fingerprint = chrome.fingerprint("run:3", "US")
    options = fingerprint.context_options()
    assert options["geolocation"] == geo.geolocation(fingerprint.timezone_id)
    assert fingerprint.timezone_id in US_ZONES and options["locale"] == "en-US"


def test_geo_imports_no_browser():
    tree = ast.parse(Path(geo.__file__).read_text())
    nodes = list(ast.walk(tree))
    names = [alias.name for node in nodes if isinstance(node, ast.Import) for alias in node.names]
    names += [node.module or "" for node in nodes if isinstance(node, ast.ImportFrom)]
    assert not any(name.split(".")[0] in ("playwright", "camoufox") for name in names)
