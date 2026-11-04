"""Browser bundle ingest, archive side: the rebuild of browser bundles from Spaces (CALC-INC
Q5), Spaces.put_verified on the local archive and unpacking a bundle's browser/ members."""

import io
import json
import tarfile
from datetime import date, timedelta

import pytest
from test_archive import empty_database
from test_browser_ingest import KEY, MEMBERS, browser_run, make_bundle, upload

from proxy_index.archive import ArchiveError
from proxy_index.archive.export import file_digests
from proxy_index.archive.rawfiles import rebuild_browser
from proxy_index.archive.spaces import ArchiveChanged, Spaces
from proxy_index.browser import contracts as bc
from proxy_index.cli import main
from proxy_index.orchestrator import browser_ingest
from proxy_index.orchestrator.archive import ArchiveError as UploadError
from proxy_index.orchestrator.archive import unpack
from proxy_index.orchestrator.browser_ingest import LocalArchive
from proxy_index.storage import connect

# The fixtures (db, client, scratch, world) are test_browser_ingest's.
pytest_plugins = ["test_storage", "test_browser_ingest"]


# --- the rebuild from Spaces (CALC-INC Q5) ---------------------------------------------------


async def test_archive_rebuild_replays_the_browser_bundles_from_spaces(
    database, databases, client, world, scratch, capsys
):
    run_ids = []
    for target in bc.PAGE_TARGETS[:3]:
        batch_id = browser_run(database, "alpha", target=target)
        data, run_id = await make_bundle(world["tmp"], batch_id, target=target)
        assert (await upload(client, world["tokens"]["alpha"], batch_id, data, run_id))[0] == 200
        run_ids.append(run_id)
    world["bucket"].objects["/browser-files/README.txt"] = (b"x", [], "private")  # not a bundle
    world["bucket"].objects["/raw-files/2026/10/01/2026-10-01T12.tar"] = (b"x", [], "private")
    target = empty_database(databases)
    spaces = Spaces.from_file(world["file"])
    day = date(2026, 10, 1)
    report = rebuild_browser(spaces, target, "browser-files", day, day)
    assert report["runs"] == report["imported"] == 3 and len(report["unlinked"]) == 3
    with connect(target) as conn:
        found = conn.execute("SELECT run_id::text FROM test_runs ORDER BY 1").fetchall()
    assert [row[0] for row in found] == sorted(run_ids)
    # The run linked to its batch where the target has it; a replay imports nothing.
    assert rebuild_browser(spaces, database, "browser-files", day, day) == {
        "runs": 3,
        "imported": 0,
        "unlinked": [],
    }
    args = ["archive", "rebuild", "--spaces-file", str(world["file"]), "--from", "2026-10-01"]
    assert main([*args, "--to", "2026-10-01", "--database-file", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["browser"]["imported"] == 0
    assert rebuild_browser(spaces, target, "browser-files", day + timedelta(1), day)["runs"] == 0
    assert (
        main(
            [
                "archive",
                "rebuild",
                "--from",
                "2026-10-01",
                "--to",
                "2026-10-01",
                "--database-file",
                str(target),
            ]
        )
        == 2
    )
    world["bucket"].objects["/" + KEY] = (b"not a bundle", [], "private")
    with pytest.raises(ArchiveError, match="not a readable browser bundle"):
        rebuild_browser(spaces, target, "browser-files", day, day)


# --- the archive helpers ----------------------------------------------------------------------


def test_put_verified_writes_once_and_refuses_other_bytes(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    archive = LocalArchive(tmp_path / "archive")
    stored = archive.put_verified("p/k.tar.gz", first, file_digests(first), {"run-id": "r"})
    assert stored["key"] == "p/k.tar.gz" and stored["sha256"] == file_digests(first)["sha256"]
    assert archive.put_verified("p/k.tar.gz", first, file_digests(first), {"run-id": "r"})
    with pytest.raises(ArchiveChanged, match="archive_changed: p/k.tar.gz .* the local file"):
        archive.put_verified("p/k.tar.gz", second, file_digests(second))
    assert (tmp_path / "archive" / "p/k.tar.gz").read_bytes() == b"first"
    # Only the named fields of an existing object are compared.
    archive.put_verified("p/k.tar.gz", first, file_digests(first), {"run-id": "s"}, existing=())
    assert archive.head("missing") is None


def test_unpack_admits_only_the_nested_names_asked_for(tmp_path):
    count = iter(range(100))

    def fresh():
        path = tmp_path / f"out{next(count)}"
        path.mkdir()
        return path

    def archive_of(*names):
        path = tmp_path / f"a{next(count)}.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name in ("summary.json", *names):
                info = tarfile.TarInfo(name)
                info.size = 2
                archive.addfile(info, io.BytesIO(b"{}"))
        return path

    nested = browser_ingest.bundle_member
    out = fresh()
    names = unpack(archive_of(MEMBERS.tries), out, max_bytes=100, nested=nested)
    assert names == ["summary.json", MEMBERS.tries] and (out / MEMBERS.tries).is_file()
    for name in ("browser/../evil", "browser/other.json", "browser/screenshots/1.jpg"):
        with pytest.raises(UploadError, match="plain, unique"):
            unpack(archive_of(name), fresh(), max_bytes=100, nested=nested)
    with pytest.raises(UploadError, match="plain, unique"):  # no nesting without the option
        unpack(archive_of(MEMBERS.tries), fresh(), max_bytes=100)
    with pytest.raises(UploadError, match="Too many"):
        unpack(archive_of(MEMBERS.tries), fresh(), max_bytes=100, nested=nested, max_members=1)
