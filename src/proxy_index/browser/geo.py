"""The exit country's browser geography: locale, languages, time zones and geolocation (D7;
docs/browser.html, Runner).

Both engines draw a try's place from its seed: Chrome's fingerprint (chrome.fingerprint) and
Camoufox's context (driver.Browser.context). A country without a table entry is refused.
Nothing here is looked up: the table is all there is.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Any

from ..errors import UserError

# Meters: a place is its zone's main city, as a coarse network location would give it.
ACCURACY_M = 100


class GeoRefused(UserError):
    """A country without a table entry."""


@dataclass(frozen=True, slots=True)
class Country:
    """One country's locale, its languages (navigator.languages, best first) and, per time
    zone a user there has, its main city's latitude and longitude."""

    locale: str
    languages: tuple[str, ...]
    zones: dict[str, tuple[float, float]]

    @property
    def accept_language(self) -> str:
        """The Accept-Language of `languages`: the first unweighted, then q=0.9, 0.8, ..."""
        weighted = (f"{tag};q={(10 - i) / 10:.1f}" for i, tag in enumerate(self.languages[1:], 1))
        return ",".join((self.languages[0], *weighted))


COUNTRIES: dict[str, Country] = {
    "US": Country(
        "en-US",
        ("en-US", "en"),
        {
            "America/New_York": (40.7128, -74.0060),
            "America/Chicago": (41.8781, -87.6298),
            "America/Denver": (39.7392, -104.9903),
            "America/Los_Angeles": (34.0522, -118.2437),
        },
    ),
    "CA": Country(
        "en-CA",
        ("en-CA", "en"),
        {"America/Toronto": (43.6532, -79.3832), "America/Vancouver": (49.2827, -123.1207)},
    ),
    "GB": Country("en-GB", ("en-GB", "en"), {"Europe/London": (51.5074, -0.1278)}),
    "DE": Country("de-DE", ("de-DE", "de", "en"), {"Europe/Berlin": (52.5200, 13.4050)}),
    "FR": Country("fr-FR", ("fr-FR", "fr", "en"), {"Europe/Paris": (48.8566, 2.3522)}),
    "NL": Country("nl-NL", ("nl-NL", "nl", "en"), {"Europe/Amsterdam": (52.3676, 4.9041)}),
    "AU": Country(
        "en-AU",
        ("en-AU", "en"),
        {"Australia/Sydney": (-33.8688, 151.2093), "Australia/Melbourne": (-37.8136, 144.9631)},
    ),
}


@dataclass(frozen=True, slots=True)
class Place:
    """One try's draw: its country's locale and languages, and one of its time zones."""

    locale: str
    languages: tuple[str, ...]
    accept_language: str
    timezone_id: str

    def camoufox_options(self) -> dict[str, Any]:
        """Camoufox's context options: its `locale` takes the language list, which becomes
        navigator.languages (a single locale would leave one language)."""
        return {
            "locale": ",".join(self.languages),
            "timezone_id": self.timezone_id,
            "geolocation": geolocation(self.timezone_id),
            "extra_http_headers": {"Accept-Language": self.accept_language},
        }


def country(code: str) -> Country:
    """The table entry of an alpha-2 `code` (either case); refuses (GeoRefused) one without."""
    entry = COUNTRIES.get(code.upper())
    if entry is None:
        raise GeoRefused(f"no browser geography for country {code!r}")
    return entry


def place(draw: random.Random, code: str) -> Place:
    """A time zone of `code` drawn with `draw` (one choice)."""
    entry = country(code)
    zone = draw.choice(tuple(entry.zones))
    return Place(entry.locale, entry.languages, entry.accept_language, zone)


def geolocation(zone: str) -> dict[str, float]:
    """Playwright's geolocation option for a table `zone`: its city. A page sees it only once
    the geolocation permission is given, which no try does."""
    for entry in COUNTRIES.values():
        if zone in entry.zones:
            latitude, longitude = entry.zones[zone]
            return {"latitude": latitude, "longitude": longitude, "accuracy": ACCURACY_M}
    raise GeoRefused(f"no city for time zone {zone!r}")


def seeded(seed: str) -> random.Random:
    """The reproducible draw of `seed` (a run id and try index; not secret)."""
    return random.Random(hashlib.sha256(seed.encode()).digest())
