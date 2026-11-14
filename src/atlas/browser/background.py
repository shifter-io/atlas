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
"""

from __future__ import annotations

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
