"""SEC-UPLOAD-1: an upload's metadata headers, summary.json and artifact rows are bounded before
they are held in memory."""

import io
import tarfile

import pytest

from atlas.orchestrator import archive as run_archive
from atlas.orchestrator.archive import MAX_HEADER, MAX_SUMMARY, ArchiveError, unpack
from atlas.storage_read import MAX_LINE, declared_lines


def archive(path, members, *, format=tarfile.PAX_FORMAT):
    """A tar.gz of (TarInfo, data) members."""
    with tarfile.open(path, "w:gz", format=format) as out:
        for info, data in members:
            info.size = len(data)
            out.addfile(info, io.BytesIO(data))
    return path


def empty(path):
    path.mkdir()
    return path


def member(name, data=b"{}", **pax):
    info = tarfile.TarInfo(name)
    info.pax_headers = pax
    return info, data


def test_a_metadata_header_within_the_limit_is_read(tmp_path):
    source = archive(tmp_path / "a.tar.gz", [member("summary.json", note="x" * 1000)])
    assert unpack(source, empty(tmp_path / "out"), max_bytes=1 << 20) == ["summary.json"]


def test_a_pax_header_over_the_limit_is_refused_before_extraction(tmp_path):
    big = member("ips.jsonl.gz", note="x" * MAX_HEADER)
    source = archive(tmp_path / "a.tar.gz", [member("summary.json"), big])
    with pytest.raises(ArchiveError, match="metadata header"):
        unpack(source, empty(tmp_path / "out"), max_bytes=1 << 20)
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["summary.json"]


def test_a_gnu_long_name_over_the_limit_is_refused(tmp_path):
    long = member("a" * (MAX_HEADER + 1))
    source = archive(tmp_path / "a.tar.gz", [long], format=tarfile.GNU_FORMAT)
    with pytest.raises(ArchiveError, match="metadata header"):
        unpack(source, empty(tmp_path / "out"), max_bytes=1 << 20)


def test_sparse_members_are_refused(tmp_path):
    old = tarfile.TarInfo("summary.json")
    old.type = tarfile.GNUTYPE_SPARSE
    source = archive(tmp_path / "a.tar.gz", [(old, b"")], format=tarfile.GNU_FORMAT)
    with pytest.raises(ArchiveError, match="only regular files"):
        unpack(source, empty(tmp_path / "old"), max_bytes=1 << 20)
    sparse = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0", "GNU.sparse.name": "summary.json"}
    source = archive(tmp_path / "b.tar.gz", [member("summary.json", **sparse)])
    with pytest.raises(ArchiveError, match="only regular files"):
        unpack(source, empty(tmp_path / "pax"), max_bytes=1 << 20)


def test_the_summary_cap_is_far_above_real_summaries():
    assert MAX_SUMMARY == 64 << 20  # the largest real summary.json (a daily run's) is 6.3 MB


@pytest.mark.parametrize(("size", "refused"), [(1000, False), (1001, True)])
def test_summary_json_is_capped(tmp_path, monkeypatch, size, refused):
    monkeypatch.setattr(run_archive, "MAX_SUMMARY", 1000)
    source = archive(tmp_path / "a.tar.gz", [member("summary.json", b" " * size)])
    if refused:
        with pytest.raises(ArchiveError, match="summary.json is larger"):
            unpack(source, empty(tmp_path / "out"), max_bytes=1 << 30)
        assert not (tmp_path / "out" / "summary.json").exists()
    else:
        assert unpack(source, empty(tmp_path / "out"), max_bytes=1 << 30) == ["summary.json"]


def test_rows_up_to_the_declared_count_are_read():
    assert list(declared_lines(io.StringIO("a\nb\n"), 2)) == ["a\n", "b\n"]
    assert list(declared_lines(io.StringIO(""), 0)) == []


def test_reading_stops_once_rows_pass_the_declared_count():
    read = []
    with pytest.raises(ValueError, match="declared count"):
        read.extend(declared_lines(io.StringIO("a\nb\nc\nd\n"), 2))
    assert read == ["a\n", "b\n"]


def test_a_row_past_the_line_limit_is_refused():
    assert len(next(declared_lines(io.StringIO("x" * MAX_LINE), 1))) == MAX_LINE
    with pytest.raises(ValueError, match="line limit"):
        next(declared_lines(io.StringIO("x" * (MAX_LINE + 1)), 1))
