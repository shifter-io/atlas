"""scripts/postgres_local.py: only a new cluster made with --ephemeral skips crash safety."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "postgres_local.py"
_spec = importlib.util.spec_from_file_location("postgres_local", SCRIPT)
postgres_local = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(postgres_local)

SETTINGS = ("fsync = off", "synchronous_commit = off", "full_page_writes = off")


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    """postgres_local's paths under tmp_path, with an initdb that only writes the config."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(postgres_local, "STATE", state)
    monkeypatch.setattr(postgres_local, "DATA", state / "data")
    monkeypatch.setattr(postgres_local, "PRIVATE", tmp_path / "secrets")
    (tmp_path / "secrets").mkdir()
    calls = []

    def initdb(command, check):
        calls.append(command)
        (state / "data").mkdir()
        (state / "data" / "postgresql.conf").write_text("# initdb\n")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(postgres_local.subprocess, "run", initdb)
    return state / "data" / "postgresql.conf", calls


def init(ephemeral):
    admin = {"user": "proxy_admin", "password": "secret"}
    postgres_local.init_cluster(Path("bin"), admin, {"port": 55432}, ephemeral=ephemeral)


def test_ephemeral_cluster_turns_off_crash_safety(cluster):
    config, calls = cluster
    init(ephemeral=True)
    text = config.read_text()
    assert all(f"\n{setting}\n" in text for setting in SETTINGS)
    assert "port = 55432\n" in text and calls[0][0] == str(Path("bin") / "initdb")


def test_default_cluster_keeps_crash_safety(cluster):
    config, _ = cluster
    init(ephemeral=False)
    text = config.read_text()
    assert "password_encryption = 'scram-sha-256'\n" in text
    assert not any(setting in text for setting in SETTINGS)


@pytest.mark.parametrize(("argv", "ephemeral"), [([], False), (["--ephemeral"], True)])
def test_ephemeral_is_opt_in(monkeypatch, argv, ephemeral):
    monkeypatch.setattr(sys, "argv", ["postgres_local.py", "init", *argv])
    assert postgres_local.parse_args().ephemeral is ephemeral
