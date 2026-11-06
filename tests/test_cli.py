import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from proxy_index import cli, enrichment, storage
from proxy_index.calculated import store
from proxy_index.cli import main
from proxy_index.errors import UserError


def test_every_command_has_one_handler():
    parser, handlers = cli._build_parser()
    commands = next(a for a in parser._actions if a.dest == "command")
    assert set(commands.choices) == set(handlers)


@pytest.fixture
def backends(monkeypatch):
    calls = []

    def record(name, result):
        def call(*args, **kwargs):
            calls.append((name, args))
            return result

        return call

    monkeypatch.setattr(storage, "ingest", record("ingest", {"runs": 1}))
    monkeypatch.setattr(storage, "initialize", record("initialize", {"schema": 7}))
    monkeypatch.setattr(storage, "migrate", record("migrate", {"migrated": True}))
    monkeypatch.setattr(storage, "status", record("status", {"runs": 0}))
    monkeypatch.setattr(store, "initialize", record("calculated", {"calculated": 1}))
    monkeypatch.setattr(enrichment, "enrich_run", record("enrich", {"enriched": 2}))
    return calls


@pytest.mark.parametrize(
    "argv, called, printed",
    [
        (["ingest", "run-dir", "--database-file", "db"], ["ingest"], {"runs": 1}),
        (
            ["db-init", "--database-file", "db"],
            ["initialize", "calculated"],
            {"schema": 7, "calculated": 1},
        ),
        (["db-migrate", "--database-file", "db"], ["migrate"], {"migrated": True}),
        (["db-status", "--database-file", "db"], ["status"], {"runs": 0}),
        (["enrich", "run-dir"], ["enrich"], {"enriched": 2}),
    ],
)
def test_data_commands_print_their_backend_result(argv, called, printed, backends, capsys):
    assert main(argv) == 0
    assert [name for name, _ in backends] == called
    assert json.loads(capsys.readouterr().out) == printed


def test_enrich_imports_after_saving(backends, capsys):
    assert main(["enrich", "run-dir", "--mmdb", "m.mmdb", "--database-file", "db"]) == 0
    assert backends == [("enrich", ("run-dir", "m.mmdb")), ("ingest", ("run-dir", "db"))]
    first, second = capsys.readouterr().out.split("\n}\n")
    assert json.loads(first + "}") == {"enriched": 2}
    assert json.loads(second) == {"database_import": {"runs": 1}}


def test_db_info_prints_the_release(monkeypatch, capsys):
    @contextmanager
    def enricher(path):
        yield SimpleNamespace(release={"path": path, "sha256": "00"})

    monkeypatch.setattr(enrichment, "LocalEnricher", enricher)
    assert main(["db-info", "--mmdb", "m.mmdb"]) == 0
    assert json.loads(capsys.readouterr().out) == {"path": "m.mmdb", "sha256": "00"}


@pytest.mark.parametrize(
    "error, message",
    [
        (KeyboardInterrupt(), ""),
        (UserError("Database file is missing"), "Command failed: Database file is missing\n"),
        (
            RuntimeError("http://user:secret@proxy.invalid"),
            "Command failed (RuntimeError). Check configuration and file paths.\n",
        ),
    ],
)
def test_handler_errors_become_exit_codes(error, message, monkeypatch, capsys):
    def fail(path):
        raise error

    monkeypatch.setattr(storage, "status", fail)
    assert main(["db-status", "--database-file", "db"]) == (
        130 if isinstance(error, KeyboardInterrupt) else 2
    )
    assert capsys.readouterr().err == message
