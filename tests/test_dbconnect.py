"""The database clients' TLS rules: a host that is not local must use TLS."""

import json

import psycopg
import pytest

from proxy_index import dbserver, storage_read
from proxy_index.orchestrator.db import Database
from proxy_index.providers.dbconnect import connect_options

REMOTE = {"host": "db.example", "dbname": "d", "user": "u", "password": "p"}


def toml(path, credentials, mode=0o600):
    path.write_text(
        "[postgres]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in credentials.items())
    )
    path.chmod(mode)
    return path


@pytest.fixture(autouse=True)
def _no_pghost(monkeypatch):
    monkeypatch.delenv("PGHOST", raising=False)


def test_a_remote_host_without_sslmode_requires_tls(tmp_path):
    assert connect_options(toml(tmp_path / "a.toml", REMOTE))["sslmode"] == "require"
    both = REMOTE | {"host": "127.0.0.1,db.example"}
    assert connect_options(toml(tmp_path / "b.toml", both))["sslmode"] == "require"


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer"])
def test_a_remote_host_may_not_allow_plain_text(tmp_path, mode):
    with pytest.raises(ValueError, match="not local"):
        connect_options(toml(tmp_path / "a.toml", REMOTE | {"sslmode": mode}))


def test_a_remote_host_keeps_a_verifying_mode_and_its_root_certificate(tmp_path):
    chosen = REMOTE | {"sslmode": "verify-full", "sslrootcert": "ca.pem"}
    assert connect_options(toml(tmp_path / "a.toml", chosen)) == chosen


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost", "/run/postgresql"])
def test_a_local_host_keeps_its_mode(tmp_path, host):
    plain = REMOTE | {"host": host}
    assert connect_options(toml(tmp_path / "a.toml", plain)) == plain
    disabled = plain | {"sslmode": "disable"}
    assert connect_options(toml(tmp_path / "b.toml", disabled)) == disabled


def test_without_a_host_pghost_decides(tmp_path, monkeypatch):
    socket = {k: v for k, v in REMOTE.items() if k != "host"} | {"sslmode": "disable"}
    path = toml(tmp_path / "a.toml", socket)
    assert connect_options(path) == socket
    monkeypatch.setenv("PGHOST", "db.example")
    with pytest.raises(ValueError, match="not local"):
        connect_options(path)


def test_unknown_fields_and_shared_files_are_refused(tmp_path):
    with pytest.raises(ValueError, match="Unsupported database connection field"):
        connect_options(toml(tmp_path / "a.toml", REMOTE | {"options": "-c x=y"}))
    with pytest.raises(ValueError, match="mode 600"):
        connect_options(toml(tmp_path / "b.toml", REMOTE, mode=0o640))


def test_every_client_applies_the_rules(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(psycopg, "connect", lambda **options: seen.append(options))
    path = toml(tmp_path / "a.toml", REMOTE)
    storage_read.connect(path)
    dbserver.connect(path)
    assert [options["sslmode"] for options in seen] == ["require", "require"]
    assert Database(str(path))._credentials["sslmode"] == "require"
    plain = toml(tmp_path / "b.toml", REMOTE | {"sslmode": "disable"})
    for client in (storage_read.connect, dbserver.connect, Database):
        with pytest.raises(ValueError, match="not local"):
            client(str(plain))
