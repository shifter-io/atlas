"""SLICES-1: 24 hourly slices of daily tests from hour files (generated synthetic fixture only)."""

from __future__ import annotations

import gzip
import hashlib
import importlib
import ipaddress
import json
import stat
import sys
from pathlib import Path

import pytest

from proxy_index.formula.aggregate import aggregate_network
from proxy_index.formula.best_hours import build_all

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scale"))
slices = importlib.import_module("slices")

NETWORKS = ("alpha", "beta", "gamma")
COUNTRIES = ("DE", "FR", "JP", "US")
RELEASE = "dbip-test"
# Byteful-like: real IPs per country in hour 16, and the formula's estimate at any hour.
BYTEFUL_IPS = {"DE": 5, "FR": 2, "JP": 3}
BYTEFUL_ESTIMATE = {"DE": 3.2, "FR": 4.6, "JP": 3.0}
SHARED = "2001:db8:2:9::1"


def best(j: int, k: int) -> int:
    return (5 * k + 7 * j) % 24


def spec(j: int, k: int, hour: int) -> tuple[int, int]:
    """(unique IPs, request rows per IP) of network j, country k at an hour."""
    if (j, k) == (0, 3):  # alpha US: hours 4 and 9 tie on IPs, 9 has more rows
        return {4: (6, 2), 9: (6, 3)}.get(hour, (2, 2))
    if (j, k) == (1, 3):  # beta US: hours 4 and 9 tie on both, the earlier wins
        return {4: (6, 2), 9: (6, 2)}.get(hour, (2, 2))
    return (6, 2) if hour == best(j, k) else (3, 2)


EXPECTED = {
    (NETWORKS[j], code): best(j, k) for j in range(3) for k, code in enumerate(COUNTRIES)
} | {("alpha", "US"): 9, ("beta", "US"): 4}


def _record(ip: str, countries: dict[str, int]) -> dict:
    seen = sum(countries.values())
    return {
        "ip": ip,
        "seen_count": seen,
        "regions": {"r1": seen},
        "countries": countries,
        "enrichment": {"country_code": next(iter(countries)), "asn": 64500},
        "dbip_release": RELEASE,
    }


def _write_hour(folder: Path, hour: int, records: list[dict]) -> dict:
    name = f"hour-20260101T{hour:02d}Z.jsonl.gz"
    lines = b"".join(json.dumps(record).encode() + b"\n" for record in records)
    data = gzip.compress(lines, mtime=0)
    (folder / name).write_bytes(data)
    rows = sum(record["seen_count"] for record in records)
    return {
        "status": "present",
        "file": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "unique_ips": len(records),
        "request_rows": rows,
        "requests": rows * 2,
        "errors": {"timeout": rows // 2, "protocol": 1},
    }


def _manifest(folder: Path, network: str, slots: dict) -> None:
    manifest = {
        "schema_version": 2,
        "network": network,
        "product": "residential",
        "dbip_release": {"sha256": "0" * 64},
        "slots": slots,
    }
    (folder / "manifest.json").write_text(json.dumps(manifest))


def _measured(source: Path, j: int) -> None:
    folder = source / NETWORKS[j]
    folder.mkdir(parents=True)
    slots = {}
    for hour in range(24):
        records = []
        for k, code in enumerate(COUNTRIES):
            ips, rows = spec(j, k, hour)
            records += [_record(f"2001:db8:{j}:{hour}:{k}::{i}", {code: rows}) for i in range(ips)]
        if j == 2 and hour == best(2, 0):  # one IP serving two countries (best hours unchanged)
            records.append(_record(SHARED, {"DE": 1, "FR": 4}))
        slots[f"{hour:02d}"] = _write_hour(folder, hour, records)
    _manifest(folder, NETWORKS[j], slots)


def _byteful(source: Path) -> None:
    folder = source / "byteful"
    folder.mkdir(parents=True)
    slots = {f"{hour:02d}": {"status": "requests_no_ips", "requests": 9} for hour in range(24)}
    records = [
        _record(f"2001:db8:ff:{k}::{i}", {code: i + 1})
        for k, (code, count) in enumerate(BYTEFUL_IPS.items())
        for i in range(count)
    ]
    slots["16"] = _write_hour(folder, 16, records)
    _manifest(folder, "byteful", slots)


@pytest.fixture
def fixture(tmp_path: Path) -> dict[str, Path]:
    source, formulas, best_dir = tmp_path / "source", tmp_path / "formulas", tmp_path / "best"
    for j in range(3):
        _measured(source, j)
    _byteful(source)
    formulas.mkdir()
    best_dir.mkdir()
    documents = []
    for network in (*NETWORKS, "byteful"):
        counts = aggregate_network(source / network)
        document = {"network": network, "countries": counts["countries"]}
        formula = {"network": network, "countries": {}}
        if network == "byteful":
            document["borrowed"] = formula["borrowed"] = {"source": "test"}
            for code, entry in counts["countries"].items():
                entry["peak_hour"] = 18
                curve = [BYTEFUL_ESTIMATE[code]] * 24
                formula["countries"][code] = {"peak_hour": 18, "estimated_unique_ips": curve}
        (formulas / f"{network}.json").write_text(json.dumps(formula))
        documents.append(document)
    for document in build_all(documents):
        (best_dir / f"{document['network']}.json").write_text(json.dumps(document))
    return {"source": source, "formulas": formulas, "best": best_dir, "out": tmp_path / "out"}


def _build(paths: dict[str, Path]) -> dict:
    return slices.build(paths["source"], paths["best"], paths["formulas"], paths["out"])


def _rows(out: Path, hour: int) -> list[dict]:
    with gzip.open(out / f"slice-{hour:02d}.jsonl.gz") as handle:
        return [json.loads(line) for line in handle]


def _slice(out: Path, hour: int) -> dict:
    return json.loads((out / f"slice-{hour:02d}.json").read_text())


def test_each_daily_test_goes_to_its_best_hour_under_the_tie_rule(fixture):
    _build(fixture)
    placed = {}
    for hour in range(24):
        for test in _slice(fixture["out"], hour)["tests"]:
            if test["network"] != "byteful":
                assert test["best_hour"] == hour
                placed[(test["network"], test["country"])] = hour
    assert placed == EXPECTED
    rows = [row for row in _rows(fixture["out"], 9) if row["network"] == "alpha"]
    us = [row for row in rows if row["country"] == "US"]
    assert len(us) == 6 and {row["count"] for row in us} == {3}
    assert all(row["modelled"] is False and row["dbip_release"] == RELEASE for row in us)


def test_a_two_country_record_counts_only_its_rows_for_that_country(fixture):
    _build(fixture)
    rows = [row for row in _rows(fixture["out"], best(2, 0)) if row["ip"] == SHARED]
    assert [(row["network"], row["country"], row["count"]) for row in rows] == [("gamma", "DE", 1)]


def test_byteful_is_sized_to_its_estimate_in_both_directions(fixture):
    _build(fixture)
    best_hours = json.loads((fixture["best"] / "byteful.json").read_text())["countries"]
    by_country = {}
    for code, entry in best_hours.items():
        rows = [r for r in _rows(fixture["out"], entry["hour"]) if r["network"] == "byteful"]
        by_country[code] = [row for row in rows if row["country"] == code]
    assert {code: len(rows) for code, rows in by_country.items()} == {"DE": 3, "FR": 5, "JP": 3}
    assert sorted(row["count"] for row in by_country["DE"]) == [3, 4, 5]  # most rows kept
    reserved = ipaddress.ip_network("240.0.0.0/4")
    synthetic = [row for row in by_country["FR"] if ipaddress.ip_address(row["ip"]) in reserved]
    assert len(synthetic) == 3
    assert all(row["enrichment"]["country_code"] == "FR" for row in synthetic)
    assert all(row["modelled"] == "byteful-formula" for rows in by_country.values() for row in rows)


def test_totals_and_the_report_count_rows_ips_and_bytes(fixture):
    summary = _build(fixture)
    out = fixture["out"]
    expected_rows = 0
    for hour in range(24):
        rows, document = _rows(out, hour), _slice(out, hour)
        totals = document["totals"]
        raw = gzip.decompress((out / f"slice-{hour:02d}.jsonl.gz").read_bytes())
        assert totals["rows"] == len(rows) == sum(t["unique_ips"] for t in document["tests"])
        assert totals["unique_ips"] == len({row["ip"] for row in rows})
        assert totals["bytes"] == len(raw)
        assert totals["gzip_bytes"] == (out / f"slice-{hour:02d}.jsonl.gz").stat().st_size
        assert totals["countries"] == len({row["country"] for row in rows})
        assert totals["networks"] == len({row["network"] for row in rows})
        assert totals["modelled_rows"] == sum(1 for row in rows if row["modelled"])
        assert totals["tests"] == len(document["tests"])
        assert rows == sorted(rows, key=lambda r: (r["network"], r["country"], r["ip"]))
        expected_rows += len(rows)
    assert expected_rows == 3 * (6 * 4) + 1 + 3 + 5 + 3
    report = json.loads((out / "report.json").read_text())
    assert report == summary and len(report["slices"]) == 24
    full = [t for t in report["slices"] if t["rows"]]
    assert report["hardest"] == max(full, key=lambda t: (t["rows"], t["unique_ips"]))["hour"]
    assert report["empty_hours"] == [t["hour"] for t in report["slices"] if not t["rows"]]
    assert "2001:db8" not in (out / "report.md").read_text()
    assert "| Hour |" in (out / "report.md").read_text()


def test_a_test_carries_its_modelled_share_of_the_slot_and_its_source_file(fixture):
    _build(fixture)
    hour = EXPECTED[("alpha", "DE")]
    test = next(t for t in _slice(fixture["out"], hour)["tests"] if t["network"] == "alpha")
    manifest = json.loads((fixture["source"] / "alpha" / "manifest.json").read_text())
    slot = manifest["slots"][f"{hour:02d}"]
    share = test["request_rows"] / slot["request_rows"]
    assert test["slot_requests"] == round(slot["requests"] * share)
    assert test["slot_errors"] == round(sum(slot["errors"].values()) * share)
    assert test["source_file"] == slot["file"] and test["source_sha256"] == slot["sha256"]
    assert test["share_basis"].startswith("modelled")


def test_the_same_input_gives_the_same_bytes(fixture, tmp_path):
    _build(fixture)
    first = {path.name: path.read_bytes() for path in fixture["out"].iterdir()}
    _build({**fixture, "out": tmp_path / "again"})
    second = {path.name: path.read_bytes() for path in (tmp_path / "again").iterdir()}
    assert first == second and len(first) == 24 * 2 + 2


def test_the_output_folder_and_files_are_private(fixture):
    _build(fixture)
    out = fixture["out"]
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert {stat.S_IMODE(path.stat().st_mode) for path in out.iterdir()} == {0o600}


def test_a_best_hour_that_differs_from_the_source_fails_with_the_list(fixture):
    path = fixture["best"] / "alpha.json"
    document = json.loads(path.read_text())
    document["countries"]["DE"]["hour"] = (document["countries"]["DE"]["hour"] + 1) % 24
    del document["countries"]["JP"]
    path.write_text(json.dumps(document))
    with pytest.raises(slices.SliceError, match="best hours differ") as error:
        _build(fixture)
    assert "alpha DE" in str(error.value) and "alpha JP" in str(error.value)
    assert not fixture["out"].exists()


def _rewrite_hour(folder: Path, change, hour: int = 5) -> None:
    manifest = json.loads((folder / "manifest.json").read_text())
    slot = manifest["slots"][f"{hour:02d}"]
    lines = gzip.decompress((folder / slot["file"]).read_bytes()).splitlines()
    records = [json.loads(line) for line in lines]
    change(records)
    manifest["slots"][f"{hour:02d}"] = _write_hour(folder, hour, records)
    (folder / "manifest.json").write_text(json.dumps(manifest))


def test_a_record_missing_a_field_fails_naming_the_file(fixture):
    _rewrite_hour(fixture["source"] / "beta", lambda records: records[-1].pop("enrichment"))
    with pytest.raises(slices.SliceError, match=r"beta/hour-20260101T05Z\.jsonl\.gz:\d+: a record"):
        _build(fixture)


def test_a_field_found_only_inside_another_fails_too(fixture):
    def nest(records):
        records[0]["enrichment"]["regions"] = records[0].pop("regions")

    _rewrite_hour(fixture["source"] / "beta", nest, hour=best(1, 0))
    with pytest.raises(slices.SliceError, match=r"beta/hour-20260101T07Z\.jsonl\.gz:1: a record"):
        _build(fixture)


def test_a_missing_best_hours_file_or_a_bad_manifest_fails(fixture):
    (fixture["best"] / "gamma.json").unlink()
    with pytest.raises(slices.SliceError, match="gamma.json"):
        _build(fixture)
    path = fixture["source"] / "alpha" / "manifest.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "schema_version": 1}))
    with pytest.raises(slices.SliceError, match="alpha/manifest.json: manifest schema"):
        _build(fixture)


def test_an_hour_file_missing_from_the_manifest_fails(fixture):
    (fixture["source"] / "gamma" / "hour-20260102T05Z.jsonl.gz").write_bytes(b"")
    with pytest.raises(slices.SliceError, match="gamma/hour-20260102T05Z.jsonl.gz: not listed"):
        _build(fixture)


def test_a_sha256_mismatch_fails_naming_the_file(fixture):
    path = fixture["source"] / "alpha" / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["slots"]["07"]["sha256"] = "f" * 64
    path.write_text(json.dumps(manifest))
    with pytest.raises(slices.SliceError, match=r"alpha: hour-20260101T07Z.jsonl.gz: sha256"):
        _build(fixture)


def test_a_borrowed_network_needs_one_hour_and_an_estimate(fixture):
    path = fixture["formulas"] / "byteful.json"
    formula = json.loads(path.read_text())
    del formula["countries"]["JP"]["estimated_unique_ips"]
    path.write_text(json.dumps(formula))
    with pytest.raises(slices.SliceError, match="no estimated_unique_ips for JP"):
        _build(fixture)


def test_the_command_line_reports_errors_and_success(fixture, capsys):
    args = ["--source", str(fixture["source"]), "--best-hours", str(fixture["best"])]
    args += ["--formulas", str(fixture["formulas"]), "--out", str(fixture["out"])]
    assert slices.main(args) == 0
    assert "wrote 24 slices" in capsys.readouterr().out
    assert slices.main([*args[:1], str(fixture["out"]), *args[2:]]) == 1
    assert "no network folder" in capsys.readouterr().err
