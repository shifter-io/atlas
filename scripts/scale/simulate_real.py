"""REAL-2DAY-1: simulated days whose daily tests carry real hours' records (SIMULATE.md, "Real
hours").

`simulate.py --real-hours 12,0 --source <LC-ENRICH-1 out>`: day N's daily tests are one per
network x requested country of the network's real hour N (its hour-<date>T<HH>Z.jsonl.gz), each
with that country's records: the IP, countries[CC] as its request rows, the enrichment and the
DB-IP release. The rest is the model's (simulate_model.py): the formula hour, buckets, latency,
timeouts and the browser runs; the real IPs carry the overlap, so no synthetic shared lanes. A
network without the hour's file, a country its formula does not schedule and a source folder
without a formula get no test; Source.notes names them. The files are read where they are and
checked as slices.py checks them: the manifest's sha256 and counts, and every record's fields.

overlap() is `simulate_check.py real-overlap`: per pair of networks the distinct IPs both had,
counted straight from the same records. Counts only, never an IP.

`--real-hour-override byteful=16,none` (owner, 2026-11-10) gives one network its own real hours:
byteful's day 1 is its real 16:00 hour, and it has no test on day 2. The notes name every override.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from collections import Counter
from dataclasses import dataclass, field, fields, replace
from datetime import timedelta
from itertools import combinations
from pathlib import Path

import simulate_model
import slices
from replay_report import write_private
from replay_steps import ReplaySteps

from proxy_index.estimates import compute
from proxy_index.formula.aggregate import FormulaError, load_manifest, read_hour_file

BASIS = (
    "distinct IPs of the daily tests' records (each network's countries its formula schedules, "
    "request rows > 0): 7d and 30d over every real hour, 24h the last one's"
)


def hours(text: str) -> tuple[int, ...]:
    """'12,0' as (12, 0): the real hour of each simulated day."""
    found = tuple(int(part) for part in text.split(","))
    if not found or not all(0 <= hour <= 23 for hour in found):
        raise ValueError(f"real hours are 0-23, comma separated: {text!r}")
    return found


def override(text: str) -> tuple[str, tuple[int | None, ...]]:
    """'byteful=16,none' as ('byteful', (16, None)): a network's own real hour of each day,
    none for no test that day."""
    network, sep, given = text.partition("=")
    found = tuple(None if part == "none" else int(part) for part in given.split(","))
    if not (sep and network) or not all(hour is None or 0 <= hour <= 23 for hour in found):
        raise ValueError(f"an override is <network>=<hour>,<hour|none>: {text!r}")
    return network, found


def formulas(path) -> dict:
    """The formulas of a folder by network, as simulate_model.read_plan loads them."""
    names = sorted(file.stem for file in Path(path).glob("*.json"))
    return {f["network"]: f for f in (compute.load_formula(Path(path), n) for n in names)}


def count(folder: Path, slot: dict) -> Counter:
    """IPs per requested country (request rows > 0) of an hour file, checked against its
    manifest slot as aggregate.py checks it: sha256, distinct IPs and request rows."""
    found, ips, rows = Counter(), set(), 0
    for ip, seen, countries in read_hour_file(folder / slot["file"], slot["sha256"]):
        if sum(countries.values()) != seen:
            raise FormulaError(f"{slot['file']}: country counts differ from seen_count")
        ips.add(ip)
        rows += seen
        found.update(code for code, n in countries.items() if n > 0)
    if len(ips) != slot["unique_ips"] or rows != slot["request_rows"]:
        raise FormulaError(f"{slot['file']}: {len(ips)} IPs / {rows} rows, not the manifest's")
    return found


@dataclass(frozen=True)
class Source:
    """The LC-ENRICH-1 output (read only), the real hour of each day, and per (network, day)
    the IPs of each scheduled country; it pickles to the worker processes."""

    folder: str
    hours: tuple[int, ...]
    counts: dict = field(repr=False)  # (network, day) -> {country: IPs}
    releases: dict = field(repr=False)  # network -> its manifest's dbip_release
    notes: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=dict)  # network -> its own hour of each day or None

    @classmethod
    def read(cls, folder, hours: tuple[int, ...], formulas: dict, overrides=None) -> Source:
        """Index the source for the formulas' networks, each on its override's hours if it has
        one; ValueError, KeyError or OSError if it is not readable or does not match its
        manifests."""
        root, counts, releases, overrides = Path(folder), {}, {}, dict(overrides or {})
        if unknown := sorted(set(overrides) - set(formulas)):
            raise FormulaError(f"--real-hour-override: no formula for {unknown}")
        found = {path.parent.name: path.parent for path in root.glob("*/manifest.json")}
        if not set(found) & set(formulas):  # a mistyped --source, not every network missing
            raise FormulaError(f"{root.name}: no network folder of the formulas")
        missing, unscheduled = {str(day): [] for day in range(1, len(hours) + 1)}, set()
        for network in sorted(formulas):
            manifest = load_manifest(found[network])[1] if network in found else {"slots": {}}
            releases[network] = manifest.get("dbip_release") or {}
            schedule = formulas[network]["schedule"]
            for day, hour in enumerate(overrides.get(network, hours), 1):
                if hour is None:  # the override's day without a test
                    continue
                slot = manifest["slots"].get(f"{hour:02d}", {})
                if slot.get("status") != "present":
                    missing[str(day)].append(network)
                    continue
                own = count(found[network], slot)
                counts[network, day] = {code: n for code, n in own.items() if code in schedule}
                unscheduled |= {f"{network}/{code}" for code in own if code not in schedule}
        ignored = sorted(set(found) - set(formulas))
        notes = {"hours": list(hours), "missing": missing, "unscheduled": sorted(unscheduled)}
        notes["ignored"] = ignored
        if overrides:
            notes["overrides"] = {network: list(own) for network, own in sorted(overrides.items())}
        return cls(str(root), hours, counts, releases, notes, overrides)

    def hour(self, network: str, day: int) -> int | None:
        """The network's real hour of the day: its override's, else the day's."""
        return self.overrides.get(network, self.hours)[day - 1]

    def size(self, network: str, country: str, day: int) -> int:
        return self.counts.get((network, day), {}).get(country, 0)

    def records(self, network: str, countries, day: int) -> dict[str, list[dict]]:
        """slices.py's rows (ip, count = countries[CC], enrichment, dbip_release) of the day's
        hour file, by country."""
        folder = Path(self.folder, network)
        own = slices.Network(network, folder, load_manifest(folder)[1], {}, {})
        return slices.by_country(own, f"{self.hour(network, day):02d}", set(countries))


def model(profile: dict, plan, seed):
    """The model of a plan: Model with the plan's real source, else simulate_model's."""
    return (Model if plan.real else simulate_model.Model)(profile, plan, seed)


class Model(simulate_model.Model):
    """simulate_model.Model whose daily tests are the real hours' (Daily)."""

    def __init__(self, profile: dict, plan, seed):
        super().__init__(profile, plan, seed)
        self.pools, self.cache = {}, (None, {})  # the real IPs carry the overlap

    def size(self, lane, day: int) -> int:
        return self.plan.real.size(lane.network, lane.country, day)

    def daily(self, lane, day: int):
        """The lane's real test of the day; None without records."""
        if not self.size(lane, day):
            return None
        test = super().daily(lane, day)
        return Daily(*(getattr(test, own.name) for own in fields(test)))

    def hour(self, day: int, hour: int):
        """The model's hour with only the tests that have records, in cells of their own."""
        found = super().hour(day, hour)
        daily = tuple(test for test in found.daily if test)
        have = {(test.lane.network, test.lane.country) for test in daily}
        cells = [
            replace(cell, countries=tuple(c for c in cell.countries if (cell.network, c) in have))
            for cell in found.cells
        ]
        return replace(found, daily=daily, cells=tuple(cell for cell in cells if cell.countries))

    def records(self, lane, day: int) -> list[dict]:
        """A test's records; one read of the hour file for the network's countries due at the
        same formula hour (the tests a worker writes in a row)."""
        key = (lane.network, lane.hour, day)
        if self.cache[0] != key:
            due = [own.country for own in self.lanes.values() if (own.network, own.hour) == key[:2]]
            self.cache = (key, self.plan.real.records(lane.network, due, day))
        return self.cache[1][lane.country]


@dataclass(frozen=True)
class Daily(simulate_model.Daily):
    """A daily test of real records: the model spreads each IP's request rows over buckets."""

    def ips(self):
        """(position, position, request rows, record) of each real IP."""
        for p, row in enumerate(self.model.records(self.lane, self.day)):
            yield p, p, row["count"], row

    def observed(self) -> tuple[list, str, dict]:
        """(ips row, the record's enrichment) per (bucket, IP), the records' DB-IP release and
        its manifest entry."""
        lane, rows = self.lane, self.model.records(self.lane, self.day)
        releases = {row["dbip_release"] for row in rows}
        if len(rows) != self.size or len(releases) != 1:
            where = f"{lane.network} {lane.country} day {self.day}"
            raise ValueError(f"{where}: {len(rows)} records, {len(releases)} DB-IP releases")
        observed, release = [], releases.pop()
        for _, row, b, count in self.rows():
            moment = self.started_at + timedelta(minutes=5 * b)
            own = ReplaySteps.ips_row(self.run_id, row["ip"], count, moment)
            observed.append((own, row["enrichment"]))
        meta = dict(self.model.plan.real.releases[lane.network])
        return observed, release, meta if meta.get("sha256") == release else {}


def overlap(folder, hours: tuple[int, ...], formulas: dict, overrides=None) -> dict:
    """Per pair of networks the distinct IPs both had over every real hour (the 7d and 30d
    windows) and over the last (24h), from the records the daily tests carry; each network's
    own. Counts only."""
    source = Source.read(folder, hours, formulas, overrides)
    seen = {network: [set() for _ in hours] for network in sorted(formulas)}
    for (network, day), counts in sorted(source.counts.items()):
        for rows in source.records(network, counts, day).values():
            seen[network][day - 1] |= {str(ipaddress.ip_address(row["ip"])) for row in rows}
    every = {network: set().union(*own) for network, own in seen.items()}
    networks = {
        network: {"product": formulas[network].get("product", "residential")}
        | {"24h": len(seen[network][-1]), "7d": len(every[network]), "30d": len(every[network])}
        for network in seen
    }
    pairs = []
    for one, other in combinations(sorted(seen), 2):
        both = len(every[one] & every[other])
        pairs.append({"networks": [one, other], "24h": len(seen[one][-1] & seen[other][-1])})
        pairs[-1] |= {"7d": both, "30d": both}
    found = {"hours": list(hours), "basis": BASIS, "networks": networks, "pairs": pairs}
    names = ("missing", "unscheduled", "ignored", "overrides")
    return found | {key: source.notes[key] for key in names if key in source.notes}


def main(argv=None) -> int:
    """`simulate_check.py real-overlap`: overlap() as a JSON file (mode 600)."""
    parser = argparse.ArgumentParser(prog="simulate_check.py")
    command = parser.add_subparsers(dest="command", required=True).add_parser("real-overlap")
    command.add_argument("--source", required=True, help="the LC-ENRICH-1 output (read only)")
    command.add_argument("--hours", required=True, type=hours, help="the real hours, as 12,0")
    command.add_argument("--out", required=True, help="the JSON file of the counts")
    command.add_argument(
        "--real-hour-override", type=override, action="append", help="as byteful=16,none"
    )
    command.add_argument("--formulas", default=str(simulate_model.FORMULAS))
    args = parser.parse_args(argv)
    try:
        given = dict(args.real_hour_override or ())
        found = overlap(args.source, args.hours, formulas(args.formulas), given)
    except (ValueError, KeyError, OSError) as exc:
        print(f"real-overlap: {exc!r}", file=sys.stderr)
        return 2
    write_private(Path(args.out), json.dumps(found, indent=2))
    return 0
