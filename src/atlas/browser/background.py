"""Nothing a browser host sends goes outside the try's proxy, except to localhost (#1062;
docs/browser.html, Runner).

A browser left to itself talks to its vendor from the host's own address: Camoufox (Firefox)
downloads its media plug-ins (Widevine, OpenH264: about 18 MB per fresh profile) and reads
remote settings, update, captive-portal, safe-browsing and location services; Chrome's start-up
services call Google. Two settings stop that:

- `CAMOUFOX_PREFS`, the Firefox preferences that switch those services off at start-up;
- `NO_NETWORK`, a proxy that refuses everything: Camoufox's browser-wide proxy (each try's
  context still has its own), and the proxy of Chrome's launch-time user-agent check, whose
  page is fulfilled locally. Nothing listens on the discard port (9) as a proxy, so a request
  sent through it fails on this host; neither browser falls back to a direct connection
  (Firefox's `network.proxy.failover_direct` is off below, Chrome's proxy list has no DIRECT).

A Chrome try's whole browser runs through the try's proxy, so Chrome's own calls to Google
cost that network's bytes on every try (about 30 KB: the account check, autofill's form
lookups, Safe Browsing's key fetch and warm-up connections). A target with `chrome_services =
false` (lean.py) starts its Chrome with them off: `quiet_chrome` writes CHROME_PREFS into the
fresh profile and CHROME_ARGS are added to the launch (BROWSER-BYTES-5). None of them changes
what a page can see of the browser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import ProxyConfig

# "localhost", not 127.0.0.1: Camoufox warns about a launch proxy without geoip unless it is local.
NO_NETWORK = ProxyConfig("http", "localhost", 9)

CAMOUFOX_PREFS: dict[str, Any] = {
    # Media plug-ins (GMP): no Widevine or OpenH264 download.
    "media.gmp-manager.updateEnabled": False,
    "media.gmp-manager.url": "data:,",
    "media.gmp-gmpopenh264.autoupdate": False,
    "media.gmp-widevinecdm.autoupdate": False,
    # Remote settings and Normandy.
    "services.settings.server": "data:,#remote-settings-dummy/v1",
    "app.normandy.enabled": False,
    "app.normandy.api_url": "",
    # Captive portal and connectivity checks.
    "network.captive-portal-service.enabled": False,
    "network.connectivity-service.enabled": False,
    "captivedetect.canonicalURL": "",
    # Browser and add-on updates.
    "app.update.auto": False,
    "app.update.checkInstallTime": False,
    "app.update.disabledForTesting": True,
    "extensions.update.enabled": False,
    "extensions.getAddons.cache.enabled": False,
    # Safe browsing.
    "browser.safebrowsing.malware.enabled": False,
    "browser.safebrowsing.phishing.enabled": False,
    "browser.safebrowsing.downloads.enabled": False,
    "browser.safebrowsing.downloads.remote.enabled": False,
    "browser.safebrowsing.blockedURIs.enabled": False,
    # Network location (a context's geolocation is set, never looked up).
    "geo.provider.network.url": "",
    # A refused proxy is an error, never a direct connection.
    "network.proxy.failover_direct": False,
}

# Chrome's own services, off in a quiet try's profile: browser sign-in, autofill and saved
# passwords (no form lookups), Safe Browsing, and network prediction (no warm-up connections).
CHROME_PREFS: dict[str, Any] = {
    "signin": {"allowed": False, "allowed_on_next_startup": False},
    "autofill": {"profile_enabled": False, "credit_card_enabled": False},
    "credentials_enable_service": False,
    "safebrowsing": {"enabled": False},
    "net": {"network_prediction_options": 2},
    "omnibox": {"ai_mode_settings": 1},
}
# No account check for a profile without Google's cookies. Chrome keeps only the last
# --enable-features, so this one repeats the feature Playwright (1.62) enables with its own.
CHROME_ARGS = ("--enable-features=CDPScreenshotNewSurface,SkipListAccountsWithoutGaiaCookies",)


def quiet_chrome(profile: str | Path) -> None:
    """CHROME_PREFS as the preferences of the new, still empty, Chrome profile `profile`."""
    default = Path(profile) / "Default"
    default.mkdir(mode=0o700, exist_ok=True)
    (default / "Preferences").write_text(json.dumps(CHROME_PREFS), encoding="utf-8")
