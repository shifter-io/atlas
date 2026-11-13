"""COV-6: `python -m atlas.formula` run as a module (its exit status and message)."""

from __future__ import annotations

import runpy
import sys
import warnings

import pytest


def run_module(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["python -m atlas.formula", *argv])
    with warnings.catch_warnings():
        # Another test may already have imported the module: runpy warns, then runs it again.
        warnings.simplefilter("ignore", RuntimeWarning)
        with pytest.raises(SystemExit) as raised:
            runpy.run_module("atlas.formula", run_name="__main__")
    return raised.value.code


def test_the_module_exits_1_with_the_error_when_there_are_no_counts(tmp_path, monkeypatch, capsys):
    (tmp_path / "counts").mkdir()
    argv = ("build", "--aggregates", str(tmp_path / "counts"), "--out", str(tmp_path / "out"))
    assert run_module(monkeypatch, *argv, "--version", "1") == 1
    assert capsys.readouterr().err == f"error: no counts files in {tmp_path / 'counts'}\n"
    assert not list((tmp_path / "out").iterdir())


def test_the_module_exits_0_after_counting_no_networks(tmp_path, monkeypatch, capsys):
    (tmp_path / "enriched").mkdir()
    argv = ("aggregate", "--enrich-dir", str(tmp_path / "enriched"), "--out", str(tmp_path / "c"))
    assert run_module(monkeypatch, *argv) == 0
    assert capsys.readouterr() == ("", "")
    assert (tmp_path / "c").is_dir()
