#!/usr/bin/env python3
"""SLICES-1: one simulated day of daily tests in 24 hourly slices, from the real hour files.

Usage:
  slices.py --source <folder> --best-hours formulas/best-hours --formulas formulas --out <dir>

<folder> is the LC-ENRICH-1 output: one folder per network with manifest.json (schema 2) and its
hour-<YYYYMMDD>T<HH>Z.jsonl.gz files. It is read only. <dir> gets mode 700 and every file 600:

- slice-<HH>.jsonl.gz: one line per (network, country, ip) of the daily tests whose best hour is
  HH (formulas/best-hours/<network>.json): network, country, ip, count (the record's
  countries[CC], its request rows for that country in that hour), enrichment, dbip_release and
  modelled (false, or "byteful-formula").
- slice-<HH>.json: one entry per daily test (network, country, best hour, unique IPs, request
  rows, the source file's name and sha256, and its modelled share of the manifest slot's
  requests and errors: the slot's totals times the country's share of its request rows), plus
  the slice's totals.
- report.md, report.json: rows, unique IPs, bytes, countries and networks per slice, the hardest
  and the lightest slice, and the empty hours. Counts only, never an IP.

Checks (the build fails and names the file): every hour file is read through the formula
builder's reader (proxy_index.formula.aggregate: sha256, counts against the manifest slot); an
hour file the manifest does not list; a record missing one of FIELDS. The best hour of every
(network, country) is recomputed from the source with best_hours.build_all and must equal the
committed file, else the build fails with the list.

Byteful (a formula with `borrowed`): its rows are the real records of its one hour with IPs,
placed at each country's borrowed best hour and sized to the formula's
countries.<CC>.estimated_unique_ips[best hour]: fewer by keeping the IPs with the most request
rows, more by adding synthetic addresses (synthetic.address: 240.0.0.0/4 and 2001:db8::/32) that
copy the enrichment of the country's real records in turn. All of them are "byteful-formula".

The output is deterministic: rows are sorted and gzip's mtime is 0.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from synthetic import address

from proxy_index.formula.aggregate import (
    FormulaError,
    aggregate_network,
    load_manifest,
    parse_record,
)
from proxy_index.formula.best_hours import build_all

FIELDS = ("ip", "seen_count", "regions", "countries", "enrichment", "dbip_release")
MODELLED = "byteful-formula"
SHARE_BASIS = "modelled: slot requests and errors times the country's share of its request rows"


class SliceError(ValueError):
    """The source, the formulas or the best hours do not allow a correct build."""


@dataclass
class Network:
    name: str
    folder: Path
    manifest: dict
    best: dict[str, int]  # country -> committed best hour
    formula: dict

    @property
    def borrowed(self) -> bool:
        return "borrowed" in self.formula


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise SliceError(f"{path}: {error}") from None


def load_networks(source: Path, best_dir: Path, formulas: Path) -> list[Network]:
    networks = []
    for folder in sorted(path.parent for path in source.glob("*/manifest.json")):
        try:
            _, manifest = load_manifest(folder)
        except (FormulaError, KeyError) as error:
            raise SliceError(f"{folder.name}/manifest.json: {error}") from None
        best = _json(best_dir / f"{folder.name}.json")["countries"]
        formula = _json(formulas / f"{folder.name}.json")
        hours = {code: entry["hour"] for code, entry in best.items()}
        networks.append(Network(folder.name, folder, manifest, hours, formula))
        check_listed(networks[-1])
    if not networks:
        raise SliceError(f"{source}: no network folder with a manifest.json")
    return networks


def check_listed(network: Network) -> None:
    listed = {slot.get("file") for slot in network.manifest["slots"].values()}
    for path in sorted(network.folder.glob("hour-*.jsonl.gz")):
        if path.name not in listed:
            raise SliceError(f"{network.name}/{path.name}: not listed in its manifest")


def check_best_hours(networks: list[Network]) -> None:
    """Recompute every best hour from the source; fail with the list where it differs."""
    documents = []
    for network in networks:
        try:
            counts = aggregate_network(network.folder)
        except (FormulaError, OSError, KeyError) as error:
            raise SliceError(f"{network.name}: {error}") from None
        for code, entry in counts["countries"].items():
            formula_entry = network.formula["countries"].get(code, {})
            entry["peak_hour"] = formula_entry.get("peak_hour", 0)
        document = {"network": network.name, "countries": counts["countries"]}
        if network.borrowed:
            document["borrowed"] = network.formula["borrowed"]
        documents.append(document)
    differences = []
    for network, computed in zip(networks, build_all(documents), strict=True):
        source = {code: entry["hour"] for code, entry in computed["countries"].items()}
        for code in sorted(set(source) | set(network.best)):
            if source.get(code) != network.best.get(code):
                differences.append(
                    f"{network.name} {code}: source {source.get(code)}, "
                    f"committed {network.best.get(code)}"
                )
    if differences:
        raise SliceError("best hours differ from the source:\n  " + "\n  ".join(differences))


def read_records(network: Network, key: str, wanted: set[str]):
    """Yield the full records of hour `key` that hold a wanted country; check every line."""
    path = network.folder / network.manifest["slots"][key]["file"]
    markers = [f'"{field}":'.encode() for field in FIELDS]
    with gzip.open(path, "rb") as lines:
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            where = f"{network.name}/{path.name}:{number}"
            if not all(marker in line for marker in markers):
                raise SliceError(f"{where}: a record field is missing")
            try:
                if not wanted & set(parse_record(line)[2]):
                    continue
                record = json.loads(line)
            except (FormulaError, ValueError) as error:
                raise SliceError(f"{where}: {error}") from None
            if not all(field in record for field in FIELDS):
                raise SliceError(f"{where}: a record field is missing")
            yield record


def _row(network: str, code: str, record: dict, modelled) -> dict:
    return {
        "network": network,
        "country": code,
        "ip": record["ip"],
        "count": record["countries"][code],
        "enrichment": record["enrichment"],
        "dbip_release": record["dbip_release"],
        "modelled": modelled,
    }


def by_country(network: Network, key: str, wanted: set[str]) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {code: [] for code in wanted}
    modelled = MODELLED if network.borrowed else False
    for record in read_records(network, key, wanted):
        for code in wanted & set(record["countries"]):
            if record["countries"][code] > 0:
                rows[code].append(_row(network.name, code, record, modelled))
    return rows


def size_rows(rows: list[dict], target: int, next_index: int) -> tuple[list[dict], int]:
    """`target` rows: the most request rows first, or padded with synthetic addresses."""
    ranked = sorted(rows, key=lambda row: (-row["count"], row["ip"]))
    if target <= len(ranked) or not ranked:
        return ranked[:target], next_index
    padded = list(ranked)
    for offset in range(target - len(ranked)):
        donor = ranked[offset % len(ranked)]
        padded.append({**donor, "ip": address(next_index + offset)})
    return padded, next_index + target - len(ranked)


@dataclass
class Test:
    network: Network
    country: str
    source_key: str  # the hour whose records and slot it uses
    rows: list[dict]
    source_rows: int  # the country's real request rows in that hour

    def entry(self, hour: int) -> dict:
        slot = self.network.manifest["slots"][self.source_key]
        share = self.source_rows / slot["request_rows"] if slot["request_rows"] else 0.0
        errors = slot.get("errors", 0)
        errors = sum(errors.values()) if isinstance(errors, dict) else int(errors)
        return {
            "network": self.network.name,
            "country": self.country,
            "best_hour": hour,
            "unique_ips": len({row["ip"] for row in self.rows}),
            "request_rows": sum(row["count"] for row in self.rows),
            "slot_requests": round(slot["requests"] * share),
            "slot_errors": round(errors * share),
            "share_basis": SHARE_BASIS,
            "modelled": MODELLED if self.network.borrowed else False,
            "source_file": slot["file"],
            "source_sha256": slot["sha256"],
        }


def hour_tests(network: Network, hour: int) -> list[Test]:
    """The daily tests of a measured network whose best hour is `hour`; checks the hour's file."""
    wanted = {code for code, best in network.best.items() if best == hour}
    key = f"{hour:02d}"
    if network.manifest["slots"].get(key, {}).get("status") != "present":
        return []  # check_best_hours: no best hour falls on an hour without IPs
    rows = by_country(network, key, wanted)
    return [
        Test(network, code, key, rows[code], sum(row["count"] for row in rows[code]))
        for code in sorted(wanted)
    ]


def borrowed_tests(network: Network) -> dict[int, list[Test]]:
    """A borrowed network's tests by best hour: its one hour's records, sized to the formula."""
    present = [k for k, slot in network.manifest["slots"].items() if slot["status"] == "present"]
    if len(present) != 1:
        raise SliceError(f"{network.name}: a borrowed network needs exactly one hour with IPs")
    rows = by_country(network, present[0], set(network.best))
    tests: dict[int, list[Test]] = {hour: [] for hour in range(24)}
    next_index = 0
    for code, hour in sorted(network.best.items()):
        try:
            estimate = network.formula["countries"][code]["estimated_unique_ips"][hour]
        except (KeyError, IndexError, TypeError):
            raise SliceError(f"{network.name}: no estimated_unique_ips for {code}") from None
        sized, next_index = size_rows(rows[code], round(estimate), next_index)
        total = sum(row["count"] for row in rows[code])
        tests[hour].append(Test(network, code, present[0], sized, total))
    return tests


def write_file(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(data)


def dumps(value) -> bytes:
    return (json.dumps(value, indent=1, sort_keys=True) + "\n").encode()


def write_slice(out: Path, hour: int, tests: list[Test]) -> dict:
    rows = sorted(
        (row for test in tests for row in test.rows),
        key=lambda row: (row["network"], row["country"], row["ip"]),
    )
    raw = b"".join(json.dumps(row, sort_keys=True).encode() + b"\n" for row in rows)
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as handle:
        handle.write(raw)
    write_file(out / f"slice-{hour:02d}.jsonl.gz", buffer.getvalue())
    totals = {
        "hour": hour,
        "tests": len(tests),
        "rows": len(rows),
        "unique_ips": len({row["ip"] for row in rows}),
        "bytes": len(raw),
        "gzip_bytes": len(buffer.getvalue()),
        "countries": len({row["country"] for row in rows}),
        "networks": len({row["network"] for row in rows}),
        "modelled_rows": sum(1 for row in rows if row["modelled"]),
    }
    entries = [test.entry(hour) for test in tests]
    write_file(out / f"slice-{hour:02d}.json", dumps({"tests": entries, "totals": totals}))
    return totals


def report(slices: list[dict]) -> dict:
    full = [totals for totals in slices if totals["rows"]]

    def weight(totals: dict) -> tuple[int, int, int]:
        return (totals["rows"], totals["unique_ips"], -totals["hour"])

    return {
        "slices": slices,
        "hardest": max(full, key=weight)["hour"] if full else None,
        "lightest": min(full, key=weight)["hour"] if full else None,
        "empty_hours": [totals["hour"] for totals in slices if not totals["rows"]],
    }


def report_markdown(summary: dict) -> str:
    lines = [
        "# Daily-test slices",
        "",
        "| Hour | Tests | Rows | Unique IPs | Bytes | Gzip bytes | Countries | Networks |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for t in summary["slices"]:
        lines.append(
            f"| {t['hour']:02d} | {t['tests']} | {t['rows']} | {t['unique_ips']} | {t['bytes']} "
            f"| {t['gzip_bytes']} | {t['countries']} | {t['networks']} |"
        )
    lines += [
        "",
        f"- Hardest slice: {summary['hardest']}",
        f"- Lightest slice: {summary['lightest']}",
        f"- Empty hours: {', '.join(f'{h:02d}' for h in summary['empty_hours']) or 'none'}",
    ]
    return "\n".join(lines) + "\n"


def build(source: Path, best_dir: Path, formulas: Path, out: Path) -> dict:
    networks = load_networks(source, best_dir, formulas)
    check_best_hours(networks)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    out.chmod(0o700)
    measured = [network for network in networks if not network.borrowed]
    borrowed = [borrowed_tests(network) for network in networks if network.borrowed]
    slices = []
    for hour in range(24):
        tests = [test for network in measured for test in hour_tests(network, hour)]
        tests += [test for by_hour in borrowed for test in by_hour[hour]]
        slices.append(write_slice(out, hour, tests))
    summary = report(slices)
    write_file(out / "report.json", dumps(summary))
    write_file(out / "report.md", report_markdown(summary).encode())
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--best-hours", type=Path, required=True)
    parser.add_argument("--formulas", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        summary = build(args.source, args.best_hours, args.formulas, args.out)
    except (SliceError, OSError) as error:
        print(f"slices: {error}", file=sys.stderr)
        return 1
    print(f"slices: wrote 24 slices, empty hours {summary['empty_hours']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
