"""Hour formulas: verified hour-file counts and the public formula built from them."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from proxy_index.formula import __main__ as cli
from proxy_index.formula.aggregate import (
    FormulaError,
    aggregate_network,
    parse_record,
    read_hour_file,
)
from proxy_index.formula.build import build_formula, curve, dumps, summary_markdown

RUN = "20261025t160000z-0000000000aa"


def _record(ip, countries, compact=True):
    record = {
        "ip": ip,
        "seen_count": sum(countries.values()),
        "regions": {"r": 1},
        "countries": countries,
        "enrichment": {"asn": 64500},
        "dbip_release": "x",
    }
    if compact:
        return json.dumps(record, separators=(",", ":"))
    return json.dumps(record)


def _hour(directory, name, records, compact=True):
    """Write one hour file and return its manifest slot."""
    data = gzip.compress(("\n".join(_record(ip, c, compact) for ip, c in records) + "\n").encode())
    (directory / name).write_bytes(data)
    return {
        "status": "present",
        "file": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "unique_ips": len(records),
        "request_rows": sum(sum(c.values()) for _, c in records),
        "requests": sum(sum(c.values()) for _, c in records) + 1,
        "successes": sum(sum(c.values()) for _, c in records),
        "runs": [RUN],
    }


def _network(tmp_path, hours, name="demo", extra_slots=None):
    """A network directory: hours maps 'HH' to its records; other hours are requests_no_ips."""
    directory = tmp_path / name
    directory.mkdir()
    slots = {}
    for key in (f"{hour:02d}" for hour in range(24)):
        if key in hours:
            slots[key] = _hour(directory, f"hour-20261025T{key}Z.jsonl.gz", hours[key])
        else:
            slots[key] = {
                "status": "requests_no_ips",
                "unique_ips": 0,
                "request_rows": 0,
                "requests": 3,
                "runs": [RUN],
            }
    slots.update(extra_slots or {})
    manifest = {
        "schema_version": 2,
        "network": name,
        "product": "residential",
        "dbip_release": {"sha256": "d" * 64},
        "slots": slots,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


def _rewrite(directory, change):
    manifest = json.loads((directory / "manifest.json").read_text())
    change(manifest)
    (directory / "manifest.json").write_text(json.dumps(manifest))


A, B, C, D = "192.0.2.1", "198.51.100.2", "203.0.113.3", "2001:db8::4"


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


def _counts(hour_ips, countries, day=None, status=None):
    status = status or ["present" if ips else "requests_no_ips" for ips in hour_ips]
    return {
        "network": "demo",
        "product": "residential",
        "manifest_sha256": "m" * 64,
        "dbip_release_sha256": "d" * 64,
        "hours": [
            {
                "status": state,
                "unique_ips": ips,
                "requests": ips * 2,
                "attempted_requests": ips * 2 + 1,
                "runs": [RUN] if state != "missing" else [],
            }
            for state, ips in zip(status, hour_ips, strict=True)
        ],
        "day_unique_ips": day if day is not None else sum(hour_ips),
        "countries": countries,
    }


def _country(ips, day=None):
    return {
        "unique_ips": ips,
        "requests": [value * 3 for value in ips],
        "day_unique_ips": day if day is not None else max(ips),
    }


def test_factors_reproduce_counts_and_peak_is_one():
    hour_ips = [97 + 13 * hour for hour in range(24)]
    us = [40 + (hour * 7) % 31 for hour in range(24)]
    document = build_formula(_counts(hour_ips, {"US": _country(us, day=500)}, day=2345), 3, 30, 20)
    network = document["network_curve"]
    peak = network["unique_ips"][network["peak_hour"]]
    assert network["hour_factors"][network["peak_hour"]] == 1.0
    assert [round(factor * peak) for factor in network["hour_factors"]] == hour_ips
    assert round(network["daily_factor"] * peak) == 2345
    entry = document["countries"]["US"]
    own_peak = us[entry["peak_hour"]]
    assert entry["curve"] == "country" and entry["fallback_reason"] is None
    assert entry["hour_factors"][entry["peak_hour"]] == 1.0
    assert [round(factor * own_peak) for factor in entry["hour_factors"]] == us
    assert round(entry["daily_factor"] * own_peak) == 500
    assert document["schedule"] == {"US": entry["peak_hour"]} == {"US": entry["test_hour"]}
    assert document["formula_version"] == 3
    assert document["latency"]["available"] is False


def test_tie_breaks_prefer_requests_then_earliest_hour():
    assert curve([5, 5, 5, 1, 1], [1, 4, 4, 2, 1], [True] * 5, 9)["peak_hour"] == 1
    result = curve([5, 1, 1], [1, 2, 2], [True] * 3, 6)
    assert (result["peak_hour"], result["low_hour"]) == (0, 1)


def test_missing_hours_are_excluded_and_no_ip_hours_flagged():
    hour_ips = [0] * 24
    hour_ips[16] = 50
    status = ["requests_no_ips"] * 24
    status[16] = "present"
    status[2] = "missing"
    document = build_formula(
        _counts(hour_ips, {"US": _country(hour_ips)}, status=status), 1, 30, 20
    )
    network = document["network_curve"]
    assert network["peak_hour"] == 16 and network["low_hour"] == 0
    assert network["hour_factors"][2] is None and network["hour_factors"][0] == 0.0
    assert network["hours_with_ips"] == 1 and network["status"][0] == "requests_no_ips"
    # The hour floor is capped at the network's own hours with IPs.
    assert document["countries"]["US"]["curve"] == "country"


def test_sparse_countries_fall_back_to_the_network_curve():
    hour_ips = [100 + hour for hour in range(24)]
    few_ips = [3] * 24
    few_hours = [0] * 12 + [80] * 12
    countries = {"FR": _country(few_ips), "JP": _country(few_hours)}
    document = build_formula(_counts(hour_ips, countries), 1, 30, 20)
    network = document["network_curve"]
    for code, reason in (("FR", "peak hour has 3"), ("JP", "IPs in 12 hours")):
        entry = document["countries"][code]
        assert entry["curve"] == "network" and reason in entry["fallback_reason"]
        assert entry["hour_factors"] == network["hour_factors"]
        assert entry["test_hour"] == network["peak_hour"] == 23
        assert entry["unique_ips"] == countries[code]["unique_ips"]
    assert document["countries"]["JP"]["peak_hour"] == 12


def test_a_country_hour_without_requests_or_with_too_few_takes_the_network_curve():
    hour_ips = [1000 + 10 * hour for hour in range(24)]
    us = [200 + hour for hour in range(24)]
    us[8] = 0  # no request row of US in hour 8 (REAL-CYCLE-1: factor 0.0)
    ca = [300] * 24
    ca[18], ca[5] = 20, 120  # request rows 60 and 360 against a median of 900
    countries = {"US": _country(us), "CA": _country(ca)}
    document = build_formula(_counts(hour_ips, countries), 2, 30, 20)
    assert document["rules"]["min_sample_share"] == 0.25
    assert document["rules"]["low_sample_share"] == 0.5
    entry = document["countries"]["US"]
    assert entry["curve"] == "country" and entry["hour_basis"][8] == "network_missing"
    kept = [hour for hour in range(24) if hour != 8]
    level = sum(us[hour] for hour in kept) / sum(hour_ips[hour] for hour in kept)
    assert entry["hour_factors"][8] == pytest.approx(hour_ips[8] * level / us[23])
    assert entry["low_hour"] == 0 and entry["peak_hour"] == entry["test_hour"] == 23
    entry = document["countries"]["CA"]
    assert entry["hour_basis"][18] == "network_low_sample"
    assert entry["hour_basis"][5] == "country_low_sample"
    assert entry["hour_factors"][5] == pytest.approx(120 / 300)  # flagged, own count kept
    kept = [hour for hour in range(24) if hour != 18]
    level = sum(ca[hour] for hour in kept) / sum(hour_ips[hour] for hour in kept)
    assert entry["hour_factors"][18] == pytest.approx(hour_ips[18] * level / 300)
    assert entry["low_hour"] == 5  # a fallback hour is neither peak nor low
    assert entry["hour_basis"].count("country") == 22
    # Every factor of a covered hour rests on a measurement: none is 0.
    for code in ("US", "CA"):
        assert all(factor > 0 for factor in document["countries"][code]["hour_factors"])


def test_hour_basis_follows_the_network_and_the_country_curve_used():
    hour_ips = [0] * 24
    hour_ips[16] = 50
    status = ["requests_no_ips"] * 24
    status[16], status[2] = "present", "missing"
    countries = {"US": _country(hour_ips), "FR": _country([1] * 24)}
    document = build_formula(_counts(hour_ips, countries, status=status), 1, 30, 20)
    basis = [None] * 24
    basis[16] = "country"
    assert document["countries"]["US"]["hour_basis"] == basis
    basis[16] = "network"
    assert document["countries"]["FR"]["hour_basis"] == basis


@pytest.mark.parametrize("path", sorted(Path("formulas").glob("*.json")), ids=lambda p: p.stem)
def test_no_published_factor_is_zero_for_a_covered_hour(path):
    document = json.loads(path.read_text())
    covered = [state == "present" for state in document["network_curve"]["status"]]
    for code, entry in document["countries"].items():
        for hour, factor in enumerate(entry["hour_factors"]):
            assert not covered[hour] or (factor or 0) > 0, (code, hour, factor)
        peak = entry["unique_ips"][entry["peak_hour"]]
        for hour, basis in enumerate(entry["hour_basis"]):
            if basis in ("country", "country_low_sample"):
                assert round(entry["hour_factors"][hour] * peak) == entry["unique_ips"][hour]


def test_build_rejects_short_or_empty_curves():
    with pytest.raises(FormulaError, match="24 hours"):
        build_formula({**_counts([1] * 24, {}), "hours": []}, 1, 30, 20)
    with pytest.raises(FormulaError, match="no unique IP"):
        curve([0, 0], [1, 1], [True, True], 0)
    with pytest.raises(FormulaError, match="no measured"):
        curve([1], [1], [False], 0)


def test_dumps_keeps_scalar_lists_on_one_line():
    text = dumps({"a": [1, 2, None], "b": [[RUN], []], "c": {"d": "x"}})
    assert '"a": [1, 2, null]' in text
    assert f'["{RUN}"]' in text
    assert json.loads(text) == {"a": [1, 2, None], "b": [[RUN], []], "c": {"d": "x"}}
    assert text.endswith("}\n")


def test_summary_lists_each_network():
    document = build_formula(
        _counts([10 + h for h in range(24)], {"US": _country([1] * 24)}), 2, 30, 20
    )
    text = summary_markdown([document])
    row = [line for line in text.splitlines() if line.startswith("| demo |")][0]
    assert "| 2 | 24 | 0 | 0 | 23 | 00 |" in row and RUN in row


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
