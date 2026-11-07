"""CI-90-6: pytest's rewrite cache kept between tier1 runs (scripts/ci/rewrite_cache.py)."""

import os
import py_compile
import runpy
import sys
import tarfile
from importlib.util import MAGIC_NUMBER
from pathlib import Path

import pytest
from test_ci_timing import steps

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "rewrite_cache.py"


@pytest.fixture
def cache():
    return runpy.run_path(str(SCRIPT))


def checkout(root, body="def test_a():\n    assert 1\n"):
    """A checkout with one test module, as `actions/checkout` leaves it: mtimes of now."""
    (root / "tests" / "nested").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (root / "tests" / "test_a.py").write_text(body)
    (root / "tests" / "nested" / "helper.py").write_text("VALUE = 1\n")
    return root


def compile_tests(root):
    """What the rewrite pass leaves: a .pyc per test source, stamped with its mtime and size."""
    for source in (root / "tests").rglob("*.py"):
        pyc = source.parent / "__pycache__" / f"{source.stem}.cpython-312-pytest-9.pyc"
        py_compile.compile(str(source), cfile=str(pyc), doraise=True)


def valid(source):
    """pytest's and Python's check of a .pyc: its header's mtime and size are the source's."""
    (pyc,) = (source.parent / "__pycache__").glob(f"{source.stem}.*.pyc")
    header, stat = pyc.read_bytes()[:16], source.stat()
    return header[:4] == MAGIC_NUMBER and header[8:16] == (
        (int(stat.st_mtime) & 0xFFFFFFFF).to_bytes(4, "little")
        + (stat.st_size & 0xFFFFFFFF).to_bytes(4, "little")
    )


def test_the_key_follows_the_tests_pyproject_pytest_and_python(cache, tmp_path):
    root = checkout(tmp_path)
    first = cache["key"](root)
    os.utime(root / "tests" / "test_a.py", (5, 5))
    assert cache["key"](root) == first
    (root / "tests" / "nested" / "helper.py").write_text("VALUE = 2\n")
    second = cache["key"](root)
    assert second != first
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts = '-q'\n")
    third = cache["key"](root)
    assert third != second
    python, pytest_version = sys.version, cache["metadata"].version("pytest")
    assert cache["key"](root, (python, pytest_version)) == third
    assert cache["key"](root, (python, "0.0")) != third
    assert cache["key"](root, ("3.99", pytest_version)) != third


def test_a_saved_cache_is_restored_valid_in_the_next_checkout(cache, tmp_path, monkeypatch):
    compiled = []
    monkeypatch.setattr(cache["compileall"], "compile_dir", lambda d, **kw: compiled.append(d))
    root, store = checkout(tmp_path / "run"), tmp_path / "cache"
    (root / "src").mkdir()
    assert cache["restore"](root, store) is False and compiled == []
    source = root / "tests" / "test_a.py"
    assert source.stat().st_mtime == cache["MTIME"]
    compile_tests(root)
    cache["save"](root, store)
    (archive,) = store.iterdir()
    assert archive.name == f"{cache['key'](root)}.tar"

    # The next run's checkout: the same files, new mtimes, no __pycache__.
    for directory in cache["caches"](root):
        cache["shutil"].rmtree(directory)
    for path in cache["sources"](root):
        os.utime(path, None)
    assert cache["restore"](root, store) is True
    assert valid(source) and valid(root / "tests" / "nested" / "helper.py")
    # src/, which the tests import proxy_index from, is compiled once, for the workers.
    assert compiled == [root / "src"]

    # Another test module: another key, a miss, the pass runs again.
    source.write_text("def test_a():\n    assert 2\n")
    assert cache["restore"](root, store) is False


def test_an_unreadable_archive_is_a_miss_and_leaves_nothing(cache, tmp_path, monkeypatch):
    monkeypatch.setattr(cache["compileall"], "compile_dir", lambda d, **kw: None)
    root, store = checkout(tmp_path / "run"), tmp_path / "cache"
    compile_tests(root)
    cache["save"](root, store)
    archive = store / f"{cache['key'](root)}.tar"
    whole = archive.read_bytes()
    with tarfile.open(archive) as tar:
        # Cut inside the last member, and cut after the first one (the rest looks like the end).
        last = tar.getmembers()[-1]
        cuts = (last.offset_data + 10, tar.getmembers()[1].offset)
    for cut in (*cuts, 0):
        archive.write_bytes(whole[:cut] if cut else b"not a tar")
        for directory in cache["caches"](root):
            cache["shutil"].rmtree(directory)
        assert cache["restore"](root, store) is False
        assert cache["caches"](root) == []
    archive.write_bytes(whole)
    assert cache["restore"](root, store) is True


def test_an_archive_with_files_outside_the_test_caches_is_refused(cache, tmp_path):
    root, store = checkout(tmp_path / "run"), tmp_path / "cache"
    store.mkdir()
    planted = tmp_path / "planted.pyc"
    planted.write_bytes(b"x")
    with tarfile.open(store / f"{cache['key'](root)}.tar", "w") as tar:
        tar.add(planted, "src/__pycache__/planted.pyc")
    assert cache["restore"](root, store) is False
    assert not (root / "src").exists()
    assert cache["_cached"]("tests/__pycache__/a.pyc")
    assert cache["_cached"]("tests/nested/__pycache__/a.pyc")
    assert not cache["_cached"]("tests/../__pycache__/a.pyc")
    assert not cache["_cached"]("tests/a.pyc")


def test_a_failed_save_leaves_no_archive(cache, tmp_path, monkeypatch):
    root, store = checkout(tmp_path / "run"), tmp_path / "cache"
    compile_tests(root)

    def broken(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tarfile.TarFile, "add", broken)
    with pytest.raises(OSError, match="disk full"):
        cache["save"](root, store)
    assert list(store.iterdir()) == []


def test_the_cache_keeps_the_newest_archives_and_drops_dead_saves(cache, tmp_path):
    for index in range(cache["KEEP"] + 2):
        archive = tmp_path / f"{index:02d}.tar"
        archive.write_bytes(b"")
        os.utime(archive, (1000 + index, 1000 + index))
    dead, running = tmp_path / ".tmp-dead.part", tmp_path / ".tmp-running.part"
    dead.write_bytes(b"")
    running.write_bytes(b"")
    os.utime(dead, (1000, 1000))
    cache["prune"](tmp_path)
    kept = sorted(path.name for path in tmp_path.iterdir())
    assert kept == [".tmp-running.part"] + [f"{i:02d}.tar" for i in range(2, cache["KEEP"] + 2)]


def test_the_command_exits_0_on_a_hit_and_1_on_a_miss(cache, tmp_path, monkeypatch):
    monkeypatch.setattr(cache["compileall"], "compile_dir", lambda d, **kw: None)
    root, store = checkout(tmp_path / "run"), tmp_path / "cache"
    arguments = ["--root", str(root)]
    assert cache["main"](["restore", str(store), *arguments]) == 1
    compile_tests(root)
    assert cache["main"](["save", str(store), *arguments]) == 0
    assert cache["main"](["restore", str(store), *arguments]) == 0


def test_tier1_restores_before_any_test_import_and_saves_only_a_clean_pass():
    run = next(s for s in steps("ci.yml", "tier1") if s.get("id") == "tests")["run"]
    restore = run.index("scripts/ci/rewrite_cache.py restore")
    # Before every background check and the serial collection, which import test modules.
    assert restore < run.index("background build") < run.index("--collect-only -q \\\n")
    passed = run.index('if [ "$rewritten" = false ]; then')
    assert restore < passed < run.index("|| rewrite=$?")
    save = run.index("scripts/ci/rewrite_cache.py save")
    assert run.index('if [ "$rewrite" -eq 0 ]; then') < save < run.index("CI_TIMELINE=1")
