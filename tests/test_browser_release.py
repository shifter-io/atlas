"""The pinned Camoufox release and its verified fetch.

No test downloads the real browser: archives are small zips served by dbip_fakes.FakeCdn on
127.0.0.1, and each test pins its own build row.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile

import pytest
from dbip_fakes import FakeCdn

from atlas.browser import release
from atlas.browser.contracts import engine_major
from atlas.cli import main

PLATFORM = "linux-x86_64"
NAME = "camoufox.zip"


def bundle(*members: tuple[str, bytes, int]) -> bytes:
    """A zip of (name, data, unix mode) members; a name ending in / is a directory."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data, mode in members:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            archive.writestr(info, data)
    return out.getvalue()


GOOD = bundle(
    ("browser/", b"", stat.S_IFDIR | 0o755),
    ("camoufox-bin", b"#!/bin/sh\n", stat.S_IFREG | 0o755),
    ("browser/omni.ja", b"data", stat.S_IFREG | 0o644),
    ("application.ini", b"[App]\n", 0),
)


@pytest.fixture
def cdn():
    fake = FakeCdn()
    yield fake
    fake.close()


def pin(monkeypatch, cdn, data: bytes, *, sha256=None, size=None, executable="camoufox-bin"):
    """Serve `data` and pin it as this test's build; returns the pinned row."""
    cdn.files[NAME] = data
    build = release.Build(
        cdn.url + NAME,
        sha256 or hashlib.sha256(data).hexdigest(),
        len(data) if size is None else size,
        executable,
    )
    monkeypatch.setitem(release.BUILDS, PLATFORM, build)
    return build


def test_pins_cover_the_three_platforms_with_https_and_sha256():
    assert set(release.BUILDS) == {"darwin-arm64", "darwin-x86_64", "linux-x86_64"}
    tags = {"darwin-arm64": "mac.arm64", "darwin-x86_64": "mac.x86_64"} | {PLATFORM: "lin.x86_64"}
    for name, build in release.BUILDS.items():
        assert re.fullmatch(r"[0-9a-f]{64}", build.sha256)
        assert build.url.startswith("https://github.com/daijro/camoufox/releases/download/")
        assert build.url.endswith(f"camoufox-{release.RELEASE}-{tags[name]}.zip")
        assert build.bytes > 100 << 20
    assert engine_major(release.ENGINE_VERSION) == 158
    assert release.RELEASE.startswith(release.ENGINE_VERSION + "-")


def test_fetch_installs_the_verified_build(tmp_path, monkeypatch, cdn):
    pin(monkeypatch, cdn, GOOD)
    result = release.fetch(tmp_path, PLATFORM)
    target = tmp_path / release.RELEASE
    assert result["fetched"] is True
    assert result["executable"] == str(target / "camoufox-bin")
    assert (result["engine"], result["engine_version"]) == ("Firefox", release.ENGINE_VERSION)
    assert (target / "browser" / "omni.ja").read_bytes() == b"data"
    assert stat.S_IMODE((target / "camoufox-bin").stat().st_mode) == 0o755
    assert stat.S_IMODE((target / "application.ini").stat().st_mode) == 0o644
    assert sorted(p.name for p in tmp_path.iterdir()) == [release.RELEASE]
    assert release.executable(tmp_path, PLATFORM) == target / "camoufox-bin"


def test_a_verified_install_is_not_downloaded_again(tmp_path, monkeypatch, cdn):
    pin(monkeypatch, cdn, GOOD)
    release.fetch(tmp_path, PLATFORM)
    assert release.fetch(tmp_path, PLATFORM)["fetched"] is False
    assert cdn.downloads(NAME) == 1


def assert_refused(tmp_path, match: str) -> None:
    with pytest.raises(release.ReleaseError, match=match):
        release.fetch(tmp_path, PLATFORM)
    assert list(tmp_path.iterdir()) == []


def test_a_wrong_sha256_is_refused_and_leaves_nothing(tmp_path, monkeypatch, cdn):
    pin(monkeypatch, cdn, GOOD, sha256="0" * 64)
    assert_refused(tmp_path, "SHA-256 differs")


@pytest.mark.parametrize("delta", [1, -1])
def test_a_wrong_size_is_refused(tmp_path, monkeypatch, cdn, delta):
    pin(monkeypatch, cdn, GOOD, size=len(GOOD) + delta)
    assert_refused(tmp_path, "bytes")


@pytest.mark.parametrize(
    "member",
    [
        ("../x", b"x", stat.S_IFREG | 0o644),
        ("/etc/x", b"x", stat.S_IFREG | 0o644),
        ("a/../../x", b"x", stat.S_IFREG | 0o644),
        ("a\\..\\x", b"x", stat.S_IFREG | 0o644),
        (release.MARKER, b"{}", stat.S_IFREG | 0o644),
    ],
)
def test_a_traversal_member_is_refused(tmp_path, monkeypatch, cdn, member):
    dest = tmp_path / "dest"
    pin(monkeypatch, cdn, bundle(("camoufox-bin", b"", stat.S_IFREG | 0o755), member))
    with pytest.raises(release.ReleaseError, match="plain relative path"):
        release.fetch(dest, PLATFORM)
    assert list(dest.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dest"]


def test_links_and_devices_are_refused(tmp_path, monkeypatch, cdn):
    pin(monkeypatch, cdn, bundle(("camoufox-bin", b"/bin/sh", stat.S_IFLNK | 0o777)))
    assert_refused(tmp_path, "not a file or directory")


def test_an_archive_without_the_binary_is_refused(tmp_path, monkeypatch, cdn):
    pin(monkeypatch, cdn, GOOD, executable="Camoufox.app/Contents/MacOS/camoufox")
    assert_refused(tmp_path, "has no Camoufox.app")


def test_a_corrupt_archive_and_http_errors_are_refused(tmp_path, monkeypatch, cdn):
    pin(monkeypatch, cdn, b"not a zip")
    assert_refused(tmp_path, "unreadable archive")
    cdn.status[NAME] = 404
    assert_refused(tmp_path, f"download {NAME}: HTTP 404")


def test_unpack_limits(tmp_path, monkeypatch, cdn):
    pin(monkeypatch, cdn, GOOD)
    monkeypatch.setattr(release, "MAX_UNPACKED_BYTES", 5)
    assert_refused(tmp_path, "unpack limit")
    monkeypatch.setattr(release, "MAX_MEMBERS", 2)
    assert_refused(tmp_path, "too many members")


def test_another_install_in_the_target_is_refused(tmp_path, monkeypatch, cdn):
    pin(monkeypatch, cdn, GOOD)
    (tmp_path / release.RELEASE).mkdir()
    with pytest.raises(release.ReleaseError, match="not the verified"):
        release.fetch(tmp_path, PLATFORM)
    release.fetch(tmp_path / "other", PLATFORM)
    marker = tmp_path / "other" / release.RELEASE / release.MARKER
    marker.write_text(json.dumps({"release": release.RELEASE, "platform": "darwin-arm64"}))
    with pytest.raises(release.ReleaseError, match="not the verified"):
        release.executable(tmp_path / "other", PLATFORM)
    assert cdn.downloads(NAME) == 1


def test_missing_install_and_unpinned_platforms(tmp_path, monkeypatch):
    with pytest.raises(release.ReleaseError, match="not installed"):
        release.executable(tmp_path, PLATFORM)
    with pytest.raises(release.ReleaseError, match="no pinned Camoufox build for linux-arm64"):
        release.fetch(tmp_path, "linux-arm64")
    monkeypatch.setattr(release.sys, "platform", "linux")
    monkeypatch.setattr(release.platform, "machine", lambda: "aarch64")
    assert release.current_platform() == "linux-arm64"
    monkeypatch.setattr(release.platform, "machine", lambda: "AMD64")
    assert release.current_platform() == "linux-x86_64"


def test_cli_fetch(tmp_path, monkeypatch, cdn, capsys):
    pin(monkeypatch, cdn, GOOD)
    assert main(["browser", "fetch", "--dest", str(tmp_path), "--platform", PLATFORM]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["executable"] == str(tmp_path / release.RELEASE / "camoufox-bin")
    assert printed["engine_version"] == release.ENGINE_VERSION
    cdn.status[NAME] = 500
    assert main(["browser", "fetch", "--dest", str(tmp_path / "b"), "--platform", PLATFORM]) == 2
    assert "HTTP 500" in capsys.readouterr().err
