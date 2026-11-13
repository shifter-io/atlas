"""COV-6: the DB-IP paths the DBIP-DAILY-1 tests leave out (state checks, download limits)."""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest
from dbip_fakes import FakeCdn, tiny_mmdb

from atlas.dbip import files, source
from atlas.dbip.files import STATE, read_state
from atlas.dbip.source import UpdateError, download, newest
from atlas.enrichment import CURRENT

DAY = date(2026, 11, 2)
NAME = f"dbip-full-{DAY.isoformat()}.mmdb"


@pytest.fixture
def cdn():
    fake = FakeCdn()
    yield fake
    fake.close()


@pytest.mark.parametrize(
    ("state", "usable"),
    [
        ({"file": CURRENT, "size": 3}, True),
        ({"file": CURRENT, "size": 4}, False),  # the file was replaced behind the state
        ({"file": "other.mmdb", "size": 3}, False),
        ({"size": 3}, False),
    ],
)
def test_the_state_counts_only_when_it_describes_the_installed_file(tmp_path, state, usable):
    (tmp_path / CURRENT).write_bytes(b"abc")
    (tmp_path / STATE).write_text(json.dumps(state))
    assert read_state(tmp_path) == (state if usable else {})


def test_a_download_larger_than_the_limit_fails(tmp_path, cdn, monkeypatch):
    cdn.files[NAME] = tiny_mmdb()
    release = newest(cdn.url, DAY, DAY)
    assert release is not None and release.size == len(cdn.files[NAME])
    monkeypatch.setattr(source, "MAX_DOWNLOAD_BYTES", 10)
    monkeypatch.setattr(source, "CHUNK", 8)
    with pytest.raises(UpdateError, match=f"^download {NAME}: larger than expected$"):
        download(release, tmp_path / files.PARTIAL)


class Broken:
    """A response whose connection breaks after the first chunk."""

    def __init__(self):
        self.chunks = [b"first"]

    def read(self, size):
        if self.chunks:
            return self.chunks.pop()
        raise ConnectionResetError("peer reset")


class Sink:
    def __init__(self):
        self.data = b""

    def write(self, chunk):
        self.data += chunk


def test_a_broken_download_names_only_the_error_type():
    out = Sink()
    with pytest.raises(UpdateError) as raised:
        source._copy(Broken(), out, hashlib.sha256(), f"download {NAME}")
    assert str(raised.value) == f"download {NAME}: ConnectionResetError"
    assert raised.value.code == 3 and out.data == b"first"
