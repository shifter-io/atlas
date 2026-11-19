"""scripts/latency_basket_table.py: the LATENCY-BASKET-1 before/after table of a build (#1385)."""

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "latency_basket_table.py"
_spec = importlib.util.spec_from_file_location("latency_basket_table", SCRIPT)
table_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(table_script)


def network(name, before, after, covered=38, unserved=(), kind="residential"):
    basket = {"size": 38, "covered": covered, "unserved": list(unserved)}
    basket["pooled_ms"] = {"p50": before}
    metrics = {"latency_ms": {"p50": after}, "latency_basket": basket}
    window = {"targets": {"generic_http": {"metrics": metrics}}}
    return {"name": name, "kind": kind, "windows": {"7d": window}}


def payload():
    networks = [
        network("Fast", 300, 700, 30, ["RU", "PK"]),
        network("Even", 400, 500),
        network("Old", None, None),
    ]
    networks.append({"name": "Pooled", "kind": "residential", "windows": {"7d": {"targets": {}}}})
    balanced = {"speed": 14, "reliability": 86}
    return {"methodology": {"settings": {"profiles": {"balanced": balanced}}}, "networks": networks}


def test_the_table_gives_before_and_after_with_speed_and_its_score_change():
    rows = table_script.table(payload()["networks"], 14)
    assert rows == [
        ["7d", "Even", "residential", 400, 500, "38 / 38", "", 75.0, 100.0, 3.5],
        ["7d", "Fast", "residential", 300, 700, "30 / 38", "RU PK", 100.0, 71.4, -4.0],
        ["7d", "Old", "residential", None, None, "38 / 38", "", None, None, None],
    ]
    text = table_script.markdown(rows)
    assert text.splitlines()[0].startswith("| Window | Network |")
    assert "| 7d | Fast | residential | 300 | 700 | 30 / 38 | RU PK | 100.0 | 71.4 | -4.0 |" in text


def test_main_reads_a_saved_payload(tmp_path, monkeypatch, capsys):
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload()))
    monkeypatch.setattr(sys, "argv", ["latency_basket_table.py", "--payload", str(path)])
    table_script.main()
    assert "| 7d | Even | residential | 400 | 500 |" in capsys.readouterr().out
