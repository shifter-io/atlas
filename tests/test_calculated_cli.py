import json

import pytest

from atlas import cli
from atlas.calculated import api, calculator, store
from atlas.calculated import cli as calculated_cli
from atlas.cli import main


@pytest.mark.parametrize(
    ("argv", "name", "expected"),
    [
        (["calculate-init"], "initialize", ("db",)),
        (["snapshot-publish", "s1"], "publish", ("db", "s1")),
        (["snapshot-export", "out.json"], "export", ("db", "out.json", "current")),
        (["snapshot-export", "out.json", "--snapshot", "s2"], "export", ("db", "out.json", "s2")),
        (["run-role", "r1", "--role", "monitor"], "assign_role", ("db", "r1", "monitor")),
    ],
)
def test_store_commands_print_the_store_result(monkeypatch, capsys, argv, name, expected):
    seen = []
    monkeypatch.setattr(store, name, lambda *a: seen.append(a) or {"done": name})
    assert main([*argv, "--database-file", "db"]) == 0
    assert seen == [expected]
    assert capsys.readouterr().out == json.dumps({"done": name}, indent=2) + "\n"


def test_metric_evidence_reads_the_record_file(monkeypatch, capsys, tmp_path):
    record = tmp_path / "evidence.json"
    record.write_text('{"metric": "m"}')
    monkeypatch.setattr(store, "evidence", lambda db, rec: {"db": db, **rec})
    assert main(["metric-evidence", str(record), "--database-file", "db"]) == 0
    assert json.loads(capsys.readouterr().out) == {"db": "db", "metric": "m"}


def test_calculate_prints_only_the_snapshot_summary(monkeypatch, capsys):
    calls = []
    payload = {"snapshot_id": "s", "as_of": "t", "readiness": "ready", "rows": [1]}
    monkeypatch.setattr(calculator, "calculate", lambda *a, **k: calls.append((a, k)) or payload)
    argv = ["calculate", "--database-file", "db", "--as-of", "t", "--publish", "--full"]
    assert main(argv) == 0
    options = {"as_of": "t", "publish": True, "mode": "full"}
    assert calls == [(("db", "configs/methodology.toml"), options)]
    summary = {"snapshot_id": "s", "as_of": "t", "readiness": "ready"}
    assert json.loads(capsys.readouterr().out) == summary


def test_serve_prints_nothing(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(api, "serve", lambda *a: seen.append(a))
    assert main(["serve", "--database-file", "db", "--port", "9"]) == 0
    assert seen == [("db", "127.0.0.1", 9)]
    assert capsys.readouterr().out == ""


def test_the_main_cli_routes_every_calculated_command_here():
    assert set(calculated_cli.COMMANDS) == set(cli._CALCULATED)
