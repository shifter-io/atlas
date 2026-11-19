"""SEC-UPLOAD-1: an upload's metadata headers, summary.json and artifact rows are bounded before
they are held in memory; SEC-FOLLOWUP-3: so are a run of metadata headers and the row counts a
summary declares."""

import gzip
import io
import tarfile

import pytest

from atlas.orchestrator import archive as run_archive
from atlas.orchestrator.archive import (
    MAX_CHAIN,
    MAX_HEADER,
    MAX_SUMMARY,
    ArchiveError,
    check_rows,
    unpack,
)
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


def chained(path, count, *, format=tarfile.PAX_FORMAT):
    """A tar.gz of summary.json preceded by `count` metadata headers (pax, or GNU long names)."""
    info = tarfile.TarInfo("summary.json" if format == tarfile.PAX_FORMAT else "s" * 101)
    info.size, info.pax_headers = 2, {"comment": "x"}
    blocks = info.tobuf(format)
    metadata, header = blocks[:-512], blocks[-512:]  # one metadata header: two blocks
    body = b"{}".ljust(512, b"\0")
    path.write_bytes(gzip.compress(metadata * count + header + body + b"\0" * 1024))
    return path


def test_metadata_headers_up_to_the_chain_limit_are_read(tmp_path):
    source = chained(tmp_path / "a.tar.gz", MAX_CHAIN)
    assert unpack(source, empty(tmp_path / "out"), max_bytes=1 << 20) == ["summary.json"]


@pytest.mark.parametrize("count", [MAX_CHAIN + 1, 5000])
@pytest.mark.parametrize("format", [tarfile.PAX_FORMAT, tarfile.GNU_FORMAT])
def test_a_run_of_metadata_headers_past_the_limit_is_an_archive_error(tmp_path, count, format):
    source = chained(tmp_path / "a.tar.gz", count, format=format)
    with pytest.raises(ArchiveError, match="metadata headers in a row"):
        unpack(source, empty(tmp_path / "out"), max_bytes=1 << 20)
    assert list((tmp_path / "out").iterdir()) == []


def declaring(ips=None, geo=None):
    """A summary whose IP and geo artifacts declare these row counts (None: no such key)."""
    artifacts = {"ips": {"sha256": "0" * 64}, "geo": {"file": "geo.jsonl.gz"}}
    for name, rows in (("ips", ips), ("geo", geo)):
        if rows is not None:
            artifacts[name]["rows"] = rows
    return {"artifacts": artifacts}


@pytest.mark.parametrize(
    "summary",
    [declaring(0, 0), declaring(10, 10), declaring(10), {}, {"artifacts": None}],
)
def test_declared_rows_within_the_bound_pass(summary):
    check_rows(summary, 10)


@pytest.mark.parametrize(
    ("summary", "name"),
    [
        (declaring(11), "ips"),
        (declaring(10, 11), "geo"),
        (declaring(-1), "ips"),
        (declaring("5"), "ips"),
        (declaring(True), "ips"),
        (declaring(1, 2.0), "geo"),
    ],
)
def test_declared_rows_outside_the_bound_are_refused(summary, name):
    with pytest.raises(ArchiveError, match=f"{name} rows are outside the allowed range"):
        check_rows(summary, 10)
