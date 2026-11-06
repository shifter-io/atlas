"""Hour formulas: verified hour-file counts and the `aggregate` and `build` CLI.

The curves are in `test_formula_build.py`, the published and borrowed ones in
`test_formula_borrow.py`.
"""

from __future__ import annotations

import gzip
import hashlib
import json

import pytest
from formula_cases import A, B, C, D, _hour, _network, _record, _rewrite

from proxy_index.formula import __main__ as cli
from proxy_index.formula.aggregate import (
    FormulaError,
    aggregate_network,
    parse_record,
    read_hour_file,
)


def test_parse_record_compact_and_spaced_layouts_agree():
    compact = parse_record(_record(A, {"US": 2, "DE": 1}).encode())
    spaced = parse_record(_record(A, {"US": 2, "DE": 1}, compact=False).encode())
    assert compact == spaced == (A.encode(), 3, {"US": 2, "DE": 1})


def test_parse_record_rejects_garbage():
    with pytest.raises(FormulaError, match="unreadable"):
        parse_record(b'{"seen_count":1}')


def test_read_hour_file_checks_sha256(tmp_path):
    slot = _hour(tmp_path, "h.jsonl.gz", [(A, {"US": 1})])
    assert list(read_hour_file(tmp_path / "h.jsonl.gz", slot["sha256"])) == [
        (A.encode(), 1, {"US": 1})
    ]
    with pytest.raises(FormulaError, match="sha256"):
        list(read_hour_file(tmp_path / "h.jsonl.gz", "0" * 64))
    data = gzip.compress(f"\n{_record(B, {'DE': 2})}\n\n".encode())
    (tmp_path / "blank.gz").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    assert list(read_hour_file(tmp_path / "blank.gz", digest)) == [(B.encode(), 2, {"DE": 2})]


def test_aggregate_counts_hours_countries_and_day_union(tmp_path):
    directory = _network(
        tmp_path,
        {
            "03": [(A, {"US": 2}), (B, {"US": 1, "DE": 1})],
            "04": [(A, {"US": 1}), (C, {"DE": 4}), (D, {"US": 1})],
        },
        extra_slots={"05": {"status": "missing"}},
    )
    counts = aggregate_network(directory)
    assert counts["network"] == "demo"
    assert counts["day_unique_ips"] == 4
    assert [hour["unique_ips"] for hour in counts["hours"]][3:6] == [2, 3, 0]
    assert counts["hours"][5]["status"] == "missing"
    assert counts["hours"][0]["status"] == "requests_no_ips"
    assert counts["hours"][0]["attempted_requests"] == 3
    assert counts["hours"][4]["requests"] == 6
    us, de = counts["countries"]["US"], counts["countries"]["DE"]
    assert (us["unique_ips"][3], us["unique_ips"][4], us["day_unique_ips"]) == (2, 2, 3)
    assert (us["requests"][3], us["requests"][4]) == (3, 2)
    assert (de["unique_ips"][3], de["unique_ips"][4], de["day_unique_ips"]) == (1, 1, 2)
    assert (
        counts["manifest_sha256"]
        == hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    )
    assert "192.0.2" not in json.dumps(counts)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [("unique_ips", 5, "manifest"), ("request_rows", 99, "manifest"), ("sha256", "0" * 64, "sha")],
)
def test_aggregate_rejects_manifest_mismatch(tmp_path, field, value, message):
    directory = _network(tmp_path, {"01": [(A, {"US": 1})]})
    _rewrite(directory, lambda manifest: manifest["slots"]["01"].update({field: value}))
    with pytest.raises(FormulaError, match=message):
        aggregate_network(directory)


def test_aggregate_rejects_country_sum_and_duplicate_ip(tmp_path):
    directory = tmp_path / "bad"
    directory.mkdir()
    line = json.dumps(
        {"ip": A, "seen_count": 2, "regions": {}, "countries": {"US": 1}}, separators=(",", ":")
    )
    data = gzip.compress((line + "\n").encode())
    (directory / "h.gz").write_bytes(data)
    slot = {
        "status": "present",
        "file": "h.gz",
        "sha256": hashlib.sha256(data).hexdigest(),
        "unique_ips": 1,
        "request_rows": 2,
    }
    manifest = {
        "schema_version": 2,
        "network": "bad",
        "product": "residential",
        "dbip_release": {"sha256": "d" * 64},
        "slots": {"00": slot},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(FormulaError, match="country counts"):
        aggregate_network(directory)
    duplicate = _network(tmp_path, {"02": [(A, {"US": 1}), (A, {"US": 1})]}, name="dup")
    _rewrite(duplicate, lambda manifest: manifest["slots"]["02"].update(unique_ips=2))
    with pytest.raises(FormulaError, match="1 IPs"):
        aggregate_network(duplicate)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda m: m.update(schema_version=1), "schema"),
        (lambda m: m["slots"].update({"24": {"status": "missing"}}), "outside"),
        (lambda m: m["slots"]["00"].update(status="odd"), "unknown slot"),
        (lambda m: m["slots"]["00"].update(unique_ips=1), "has IPs"),
    ],
)
def test_aggregate_rejects_bad_manifest(tmp_path, change, message):
    directory = _network(tmp_path, {"01": [(A, {"US": 1})]})
    _rewrite(directory, change)
    with pytest.raises(FormulaError, match=message):
        aggregate_network(directory)


def test_cli_aggregate_then_build(tmp_path, capsys):
    hours = {f"{hour:02d}": [(A, {"US": 1}), (B, {"US": 2})] for hour in range(10)}
    hours["10"] = [(A, {"US": 1}), (B, {"US": 1}), (C, {"US": 1})]
    enrich = tmp_path / "enrich"
    enrich.mkdir()
    _network(enrich, hours)
    counts, out = tmp_path / "counts", tmp_path / "formulas"
    assert cli.main(["aggregate", "--enrich-dir", str(enrich), "--out", str(counts)]) == 0
    assert "3 day IPs" in capsys.readouterr().out
    assert (
        cli.main(
            ["aggregate", "--enrich-dir", str(enrich), "--out", str(counts), "--network", "demo"]
        )
        == 0
    )
    assert "kept demo.json" in capsys.readouterr().out
    assert (
        cli.main(["aggregate", "--enrich-dir", str(enrich), "--out", str(counts), "--force"]) == 0
    )
    assert (
        cli.main(
            [
                "build",
                "--aggregates",
                str(counts),
                "--out",
                str(out),
                "--version",
                "1",
                "--min-peak-ips",
                "2",
                "--min-hours",
                "5",
            ]
        )
        == 0
    )
    document = json.loads((out / "demo.json").read_text())
    assert document["network_curve"]["peak_hour"] == 10
    assert document["schedule"] == {"US": 10}
    assert "| demo |" in (out / "SUMMARY.md").read_text()
    assert not list(out.glob(".*partial"))
    assert all(ip not in (out / "demo.json").read_text() for ip in (A, B, C))


def test_cli_build_without_counts_fails(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    code = cli.main(
        [
            "build",
            "--aggregates",
            str(tmp_path / "empty"),
            "--out",
            str(tmp_path / "out"),
            "--version",
            "1",
        ]
    )
    assert code == 1 and "no counts files" in capsys.readouterr().err
