"""browser.chrome: detection over fake executables, fingerprints and user-agent helpers."""

from __future__ import annotations

import json
import logging
import re

import pytest

from proxy_index.browser import chrome
from proxy_index.browser.chrome import ChromeRefused
from proxy_index.errors import UserError

OK = f"{chrome.CHROME_MIN_MAJOR}.0.7922.34"


def fake_chrome(tmp_path, output: str, *, mode: int = 0o755, status: int = 0):
    path = tmp_path / "Google Chrome"
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\nexit {status}\n")
    path.chmod(mode)
    return path


def test_detect_ok(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert chrome.detect(fake_chrome(tmp_path, f"Google Chrome {OK} ")) == OK
    assert not caplog.records


def test_detect_newer_warns_once(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(chrome, "_warned", set())
    newer = f"{chrome.CHROME_TESTED_MAJOR + 1}.0.1.2"
    path = fake_chrome(tmp_path, f"Google Chrome {newer}")
    with caplog.at_level(logging.WARNING):
        assert chrome.detect(path) == newer
        assert chrome.detect(path) == newer
    assert [r.getMessage() for r in caplog.records] == [
        f"Chrome {newer} is newer than the tested {chrome.CHROME_TESTED_MAJOR}"
    ]


@pytest.mark.parametrize(
    ("output", "mode", "status", "reason"),
    [
        (None, 0o755, 0, "missing or not executable"),
        (f"Google Chrome {OK}", 0o644, 0, "missing or not executable"),
        ("Chromium 151.0.1.2", 0o755, 0, "no 'Google Chrome <version>' line"),
        ("Google Chrome 151.0", 0o755, 0, "no 'Google Chrome <version>' line"),
        (f"Google Chrome {OK}", 0o755, 3, "--version failed (CalledProcessError)"),
        (f"Google Chrome {chrome.CHROME_MIN_MAJOR - 1}.0.1.2", 0o755, 0, "older than"),
    ],
)
def test_detect_refuses(tmp_path, output, mode, status, reason):
    if output is None:
        path = tmp_path / "absent"
    else:
        path = fake_chrome(tmp_path, output, mode=mode, status=status)
    with pytest.raises(ChromeRefused, match=re.escape(reason)) as caught:
        chrome.detect(path)
    assert isinstance(caught.value, UserError)


def test_fingerprint_is_drawn_from_the_seed_and_the_tables():
    prints = [chrome.fingerprint(f"run:{i}", "US") for i in range(100)]
    assert len({p.record()["sha256"] for p in prints}) >= 50
    assert chrome.fingerprint("run:7", "US") == prints[7]
    for p in prints:
        assert (p.screen_width, p.screen_height, p.device_scale_factor) in chrome.SCREENS
        assert p.hardware_concurrency in chrome.HARDWARE_CONCURRENCY
        assert p.device_memory in chrome.DEVICE_MEMORY
        assert (p.locale, p.accept_language) == chrome.COUNTRIES["US"][:2]
        assert p.timezone_id in chrome.COUNTRIES["US"][2]
    assert len({p.timezone_id for p in prints}) == 4 and len({p.screen_width for p in prints}) == 5


def test_fingerprint_refuses_a_country_without_a_table():
    with pytest.raises(ChromeRefused, match="'ZZ'"):
        chrome.fingerprint("run:0", "ZZ")


def test_fingerprint_record_options_and_script():
    p = chrome.fingerprint("seed", "DE")
    record = p.record()
    assert record["values"]["timezone_id"] == "Europe/Berlin"
    text = json.dumps(record["values"], sort_keys=True, separators=(",", ":"))
    assert record["sha256"] == chrome.hashlib.sha256(text.encode()).hexdigest()
    options = p.context_options()
    assert options["screen"] == {"width": p.screen_width, "height": p.screen_height}
    assert options["viewport"]["height"] == p.screen_height - chrome.CHROME_HEIGHT
    assert options["extra_http_headers"] == {"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}
    assert options["locale"] == "de-DE" and options["device_scale_factor"] == p.device_scale_factor
    script = p.init_script()
    values = {"hardwareConcurrency": p.hardware_concurrency, "deviceMemory": p.device_memory}
    assert f"const values = {json.dumps(values)};" in script
    assert "Navigator.prototype" in script and "platform" not in script


def test_user_agent_helpers(monkeypatch):
    ua = chrome.user_agent(OK, "macos")
    assert ua == (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        f" (KHTML, like Gecko) Chrome/{chrome.CHROME_MIN_MAJOR}.0.0.0 Safari/537.36"
    )
    assert chrome.user_agent_major(ua) == chrome.CHROME_MIN_MAJOR
    assert "X11; Linux x86_64" in chrome.user_agent(OK, "linux")
    assert chrome.user_agent_major(ua.replace("Chrome/", "HeadlessChrome/")) is None
    assert chrome.user_agent_major(None) is None and chrome.user_agent_major("Firefox/1.0") is None
    monkeypatch.setattr(chrome.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(chrome.platform, "mac_ver", lambda: ("15.1.0", "", ""))
    meta = chrome.user_agent_metadata(OK, "macos")
    assert chrome.brands_major(meta["brands"]) == chrome.CHROME_MIN_MAJOR
    assert {"brand": "Google Chrome", "version": OK} in meta["fullVersionList"]
    assert (meta["platform"], meta["platformVersion"], meta["architecture"]) == (
        "macOS",
        "15.1.0",
        "arm",
    )
    monkeypatch.setattr(chrome.platform, "machine", lambda: "x86_64")
    linux = chrome.user_agent_metadata(OK, "linux")
    assert (linux["platform"], linux["platformVersion"], linux["architecture"]) == (
        "Linux",
        "",
        "x86",
    )


@pytest.mark.parametrize(
    ("brands", "major"),
    [
        ([{"brand": "Chromium", "version": "9"}, {"brand": chrome.BRAND, "version": "151"}], 151),
        ([{"brand": "HeadlessChrome", "version": "151"}], None),
        ([{"brand": "Google Chrome", "version": "x"}], None),
        ([], None),
        (None, None),
        (["Google Chrome"], None),
    ],
)
def test_brands_major(brands, major):
    assert chrome.brands_major(brands) == major
