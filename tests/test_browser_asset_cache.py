"""browser.asset_cache: what a public file may be kept for, memory and disk, expiry, limits."""

from __future__ import annotations

import json
import os

import pytest

from atlas.browser import asset_cache
from atlas.browser.asset_cache import AssetCache

PUBLIC = {"Cache-Control": "public, max-age=86400", "Content-Encoding": "br", "X-Kept": "1"}
URL = "https://static.example/js/app.js"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("status", "headers", "seconds"),
    [
        (200, {"Cache-Control": "public, max-age=60"}, 60),
        (200, {"cache-control": "max-age=86400"}, asset_cache.DEFAULT_AGE_S),
        (200, {"Cache-Control": "public, max-age=60", "Vary": "Accept-Encoding, Origin"}, 60),
        (200, {"Cache-Control": "public, max-age=60", "Vary": "Cookie"}, 0),
        (200, {"Cache-Control": "private, max-age=60"}, 0),
        (200, {"Cache-Control": "public, no-cache, max-age=60"}, 0),
        (200, {"Cache-Control": "no-store"}, 0),
        (200, {"Cache-Control": "public"}, 0),
        (200, {"Cache-Control": "public, max-age=60", "Set-Cookie": "a=b"}, 60),
        (200, {"Cache-Control": "max-age=60", "Set-Cookie": "a=b"}, 0),
        (304, {"Cache-Control": "public, max-age=60"}, 0),
    ],
)
def test_only_a_public_200_is_kept_and_a_cookie_only_on_a_public_file(status, headers, seconds):
    assert asset_cache.cache_seconds(status, headers) == seconds


def test_the_longest_life_is_the_hosts_cap():
    headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    assert asset_cache.cache_seconds(200, headers, 24 * 3600) == 24 * 3600


def test_memory_entries_expire_lose_cookies_and_encoding_and_keep_their_byte_limit(monkeypatch):
    monkeypatch.setattr(asset_cache, "MEMORY_BYTES", 8)
    clock = Clock()
    cache = AssetCache(clock=clock)
    assert cache.put("a", 200, PUBLIC | {"Set-Cookie": "cf=1"}, b"1234")
    assert cache.get("a").headers == {"Cache-Control": "public, max-age=86400", "X-Kept": "1"}
    cache.put("b", 200, PUBLIC, b"5678")
    cache.get("a")  # now the most recent: "b" goes first
    cache.put("c", 200, PUBLIC, b"9")
    assert cache.get("b") is None and cache.get("a") is not None and cache.get("c") is not None
    cache.put("a", 200, PUBLIC, b"12")  # replaced, not counted twice
    assert cache._size == 3
    assert not cache.put("big", 200, PUBLIC, b"x" * (asset_cache.ENTRY_BYTES + 1))
    assert not cache.put("private", 200, {"Cache-Control": "private"}, b"x")
    clock.now += asset_cache.DEFAULT_AGE_S
    assert cache.get("a") is None and cache._size == 1


def test_an_app_bundle_of_several_megabytes_is_kept():
    # BROWSER-BYTES-4: Gemini's main bundle is about 6.2 MB decoded; a 5 MB limit refused it.
    bundle = b"x" * 6_200_000
    cache = AssetCache()
    assert cache.put(URL, 200, PUBLIC, bundle) and cache.get(URL).body == bundle


def test_a_directory_keeps_entries_for_the_next_process_until_they_expire(tmp_path):
    clock, directory = Clock(), tmp_path / "net.residential"
    first = AssetCache(directory, 24 * 3600, clock)
    assert first.put(URL, 200, PUBLIC | {"Set-Cookie": "cf=1"}, b"body")
    files = sorted(directory.iterdir())
    assert [path.suffix for path in files] == [".body", ".json"]
    assert {path.stat().st_mode & 0o777 for path in files} == {0o600}
    assert "cf=1" not in files[1].read_text()
    later = AssetCache(directory, 24 * 3600, clock)  # a new process: memory empty
    entry = later.get(URL)
    assert entry is not None and entry.body == b"body" and entry.status == 200
    clock.now += 24 * 3600
    assert AssetCache(directory, 24 * 3600, clock).get(URL) is None
    assert list(directory.iterdir()) == []


def test_a_lower_cache_hours_applies_at_once_to_files_kept_under_a_higher_one(tmp_path):
    assert AssetCache(tmp_path, 24 * 3600).put(URL, 200, PUBLIC, b"body")
    assert AssetCache(tmp_path, 24 * 3600).get(URL) is not None
    body = next(tmp_path.glob("*.body"))
    hour_ago = body.stat().st_mtime - 3600
    os.utime(body, (hour_ago, hour_ago))
    assert AssetCache(tmp_path, 900).get(URL) is None


def test_a_damaged_or_foreign_meta_file_is_a_miss(tmp_path):
    cache = AssetCache(tmp_path, 3600, Clock())
    cache.put(URL, 200, PUBLIC, b"body")
    meta = next(tmp_path.glob("*.json"))
    meta.write_text(json.dumps({"url": "https://other.example/x.js"}))
    assert AssetCache(tmp_path, 3600, Clock()).get(URL) is None
    meta.write_text("not json")
    assert AssetCache(tmp_path, 3600, Clock()).get(URL) is None


def test_the_directory_keeps_its_byte_limit_dropping_the_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(asset_cache, "DISK_BYTES", 10)
    cache = AssetCache(tmp_path, 3600, Clock())
    for index in range(4):
        cache.put(f"{URL}?{index}", 200, PUBLIC, b"1234")
    assert asset_cache._usage(tmp_path) <= 10
    assert len(list(tmp_path.glob("*.body"))) == len(list(tmp_path.glob("*.json")))


def test_a_directory_that_cannot_be_written_still_serves_from_memory(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("x")
    cache = AssetCache(blocked / "cache", 3600, Clock())
    assert cache.put(URL, 200, PUBLIC, b"body") and cache.get(URL).body == b"body"


def test_configure_sets_each_network_its_own_directory_and_life(tmp_path):
    asset_cache.configure(tmp_path, 24)
    cache = asset_cache.network_cache("net-a", "residential")
    assert cache.directory == tmp_path / "net-a.residential" and cache.max_age_s == 24 * 3600
