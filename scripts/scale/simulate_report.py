"""SIM-LOAD-1: the loader's figures, its plan (--plan-only) and its report (SIMULATE.md, "The
report"): report.md and report.json, mode 600, with names and counts only, never an address,
a host name or a path."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from profile_reference import TABLES
from replay_report import cell, mb, table, write_private
from simulate_browser import dig

GB = 2**30
KINDS = ("daily", "browser", "pulse")
BYTES = ("table_bytes", "index_bytes")
CADENCE = (
    "one daily test per network × country per day; one 100-try browser run per network × "
    "target per day; no HTTP pulse fleet, so no pulses, census or load waves unless switched on"
)
BASIS = {
    "growth": "the day's change of count(*), pg_table_size and pg_indexes_size, partitions "
    "included",
    "expected": "the profile's reference: each kind's mean rows and bytes per run "
    "(reference.<kind>.rows_per_run and bytes.per_run; `kinds` names each kind's basis, real or "
    "modelled) × the day's runs of that kind. The shared tables (ip_addresses, ip_enrichment, "
    "product_ips, pool_ips) count a run's IPs, so their growth is lower by the IPs seen before",
    "real_24h": "the profile's real_24h: the 24-hour onboarding run's measured totals over its "
    "networks (one run per network, region and hour); bundle bytes of the sampled bundles only",
}
# (label, the generated day's figure, real_24h's figure)
REAL = (
    ("unique IPs", "unique_ips", "unique_ips_hour_sum"),
    ("requests", "requests", "attempted_requests"),
    ("browser runs", "browser", "browser_runs"),
    ("browser tries", "tries", "browser_tries"),
    ("bundle bytes", "bundle_bytes", "bundle_bytes_sampled"),
)


def figures(hour, found: dict) -> dict:
    """An hour's runs as the report and the size check read them: per daily test its network,
    rows per table (TABLES' order), unique IPs and requests; the browser runs' totals."""
    daily = [
        [t.lane.network, [found[t.run_id]["tables"][name] for name in TABLES], t.size]
        + [found[t.run_id]["requests"]]
        for t in hour.daily
    ]
    tries = sum(len(run.tries) for run in hour.browser)
    bundles = sum(run.bundle_bytes for run in hour.browser)
    return {"daily": daily, "tries": tries, "bundle_bytes": bundles}


@dataclass(frozen=True)
class Stored:
    """A stored daily test as simulate_check.size reads one: its lane's network and rows."""

    network: str
    rows: tuple

    @property
    def lane(self):
        return SimpleNamespace(network=self.network)

    def tables(self) -> dict[str, int]:
        return dict(zip(TABLES, self.rows, strict=True))


def stored(model, records: list[dict]) -> list:
    """The stored hours as simulate_check.size reads them: the daily tests' recorded figures and
    the model's browser runs."""
    hours = []
    for record in records:
        day, hour = record["day"] + 1, datetime.fromisoformat(record["start"]).hour
        daily = tuple(Stored(test[0], tuple(test[1])) for test in record["figures"]["daily"])
        hours.append(SimpleNamespace(day=day, daily=daily, browser=model.hour(day, hour).browser))
    return hours


def expected(profile: dict, runs: dict) -> dict:
    """Per table, the reference's mean rows and bytes per run × the runs of each kind."""
    found = {}
    for kind, count in runs.items():
        reference = dig(profile, "reference", kind)
        per_run = dig(reference, "bytes", "per_run")
        for name, spread in dig(reference, "rows_per_run").items():
            entry = found.setdefault(name, dict.fromkeys(("rows", *BYTES), 0))
            entry["rows"] += round((spread.get("mean") or 0) * count)
            for field in BYTES:
                entry[field] += round((dig(per_run, name).get(field) or 0) * count)
    return found


def plan(model, profile: dict, days: int, hours: int, pulses: bool) -> dict:
    """Per simulated day its runs by kind (pulses only with `pulses`), and the rows and bytes
    the reference gives them."""
    found, totals = [], Counter()
    for day in range(1, days + 1):
        own = [model.hour(day, h) for h in range(hours)]
        runs = {"daily": sum(len(h.daily) for h in own), "browser": sum(len(h.browser) for h in own)}
        runs |= {"pulse": sum(len(h.pulses) for h in own)} if pulses else {}
        found.append({"day": day, "runs": runs, "tables": expected(profile, runs)})
        totals.update(runs)
    return {"days": found, "totals": {"runs": dict(totals), "tables": expected(profile, totals)}}


def plan_table(found: dict) -> str:
    """The plan as text: per day its runs, then its rows and GB per table; then the totals."""
    lines = []
    for title, entry in [(f"day {d['day']}", d) for d in found["days"]] + [
        (f"total of {len(found['days'])} days", found["totals"])
    ]:
        runs = ", ".join(f"{kind} {n}" for kind, n in entry["runs"].items())
        lines += [f"{title}: {runs}", f"  {'table':<20}{'rows':>14}{'table GB':>11}{'index GB':>11}"]
        for name, size in sorted(entry["tables"].items()):
            gbs = "".join(f"{size[field] / GB:>11.3f}" for field in BYTES)
            lines.append(f"  {name:<20}{size['rows']:>14}{gbs}")
    return "\n".join(lines) + "\n"


def growth(before: dict, after: dict, wanted: dict) -> dict:
    """Each table whose rows or bytes changed: its figures, its growth and the expected one."""
    found = {}
    for name, now in sorted(after["tables"].items()):
        was = before["tables"].get(name, dict.fromkeys(("rows", *BYTES), 0))
        grew = {field: now[field] - was[field] for field in ("rows", *BYTES)}
        if any(grew.values()):
            short = name.removeprefix("public.")
            found[name] = now | {"growth": grew, "expected": wanted.get(short)}
    return found


def seconds(records: list[dict]) -> dict[str, float]:
    """Wall seconds per ingest step over the records' hours."""
    found = Counter()
    for record in records:
        for name, step in record["ingest"]["steps"].items():
            found[name] += sum(part.get("seconds") or 0 for part in step["parts"])
    return {name: round(value, 3) for name, value in found.items()}


def generated(records: list[dict]) -> dict[str, int]:
    """The day's figures that real_24h has too."""
    tests = [test for record in records for test in record["figures"]["daily"]]
    found = {"unique_ips": sum(test[2] for test in tests), "requests": sum(t[3] for t in tests)}
    found["browser"] = sum(record["runs"]["browser"] for record in records)
    for name in ("tries", "bundle_bytes"):
        found[name] = sum(record["figures"][name] for record in records)
    return found


def day_row(state: dict, profile: dict, day: int, before: dict) -> dict:
    records = [r for r in state["hours"] if r["day"] == day - 1 and "figures" in r]
    runs = Counter()
    for record in records:
        runs.update(record["runs"])
    row = {"day": day, "hours": len(records), "runs": dict(runs), "seconds": seconds(records)}
    after = state["sizes"].get(str(day))
    if after:
        row["tables"] = growth(before, after, expected(profile, runs))
        rows = sum(entry["growth"]["rows"] for entry in row["tables"].values())
        took = sum(row["seconds"].values())
        row |= {"rows": rows, "rows_per_second": round(rows / took) if took else None}
    real = dig(profile, "real_24h")
    found = generated(records)
    row["real_24h"] = [
        {"metric": label, "generated": found[mine], "real_24h": sum(
            (entry.get(theirs) or 0) for entry in real.values()
        )}
        for label, mine, theirs in REAL
    ]
    return row


def calculation(record: dict) -> dict | None:
    work = record["calculate"]
    if not work.get("done"):
        return None
    last = work["attempts"][-1]
    found = {"kind": work["kind"], "seconds": last["seconds"], "peak_rss_kb": last["peak_rss_kb"]}
    return found | {"attempts": len(work["attempts"]), "siteapi": work.get("siteapi")}


def build(state: dict, profile: dict) -> dict:
    """The report of a load's state (written after a stop too)."""
    days, before = [], state["baseline"]["sizes"]
    for day in range(1, state["days"]):
        days.append(day_row(state, profile, day, before))
        before = state["sizes"].get(str(day)) or before
    kinds = {kind: dig(profile, "reference", kind).get("basis") for kind in KINDS}
    report = {"cadence": CADENCE, "basis": BASIS | {"kinds": kinds}, "days": days}
    report |= {"first_day": state["first_day"], "simulated_days": state["days"] - 1}
    report["resumed"] = len(state["resumed"])
    runs = Counter()
    for day in days:
        runs.update(day["runs"])
    if "final" in state["sizes"]:
        found = growth(state["baseline"]["sizes"], state["sizes"]["final"], expected(profile, runs))
        report["final"] = {"runs": dict(runs), "tables": found}
    hours = state["hours"]
    report["calculation"] = [calculation(r) for r in hours[-2:]] if len(hours) > 1 else []
    report["counts"] = state["counts"]
    report["checks"] = state.get("checks")
    return report


def size_lines(title: str, found: dict) -> list[str]:
    head = ("Table", "Rows", "Table MB", "Index MB", "+Rows", "+Table MB", "+Index MB")
    head += ("Expected rows", "Expected table MB", "Expected index MB")
    rows = []
    for name, entry in found.items():
        grew, wanted = entry["growth"], entry["expected"] or {}
        rows.append(
            [name, entry["rows"], mb(entry["table_bytes"]), mb(entry["index_bytes"]), grew["rows"]]
            + [mb(grew["table_bytes"]), mb(grew["index_bytes"]), cell(wanted.get("rows"))]
            + [mb(wanted.get("table_bytes")), mb(wanted.get("index_bytes"))]
        )
    return ["", f"## {title}", *table(head, rows)]


def check_lines(checks: dict | None) -> list[str]:
    if not checks:
        return []
    lines = ["", "## Checks", ""]
    networks = {}
    for name in ("size", "stats"):
        found = checks[name]
        lines.append(f"- {name}: {'pass' if found['pass'] else 'FAIL'}, {found['compared']} compared")
        for check in found["checks"]:
            entry = networks.setdefault(check.get("network") or "(all)", Counter())
            entry[name, check["pass"]] += 1
    rows = [
        [network] + [f"{c[name, True]}/{c[name, True] + c[name, False]}" for name in ("size", "stats")]
        for network, c in sorted(networks.items())
    ]
    return lines + [*table(("Network", "Size passed", "Stats passed"), rows)]


def markdown(report: dict) -> str:
    lines = ["# Generated days loaded into staging (SIM-LOAD-1)", "", f"Cadence: {report['cadence']}."]
    head = ("Day", "Hours", "Daily", "Browser", "Pulses", "Rows", "Rows/s", "Ingest s")
    rows = [
        [d["day"], d["hours"], *(d["runs"].get(kind, 0) for kind in KINDS), cell(d.get("rows"))]
        + [cell(d.get("rows_per_second")), cell(round(sum(d["seconds"].values()), 1))]
        for d in report["days"]
    ]
    lines += ["", "## Days", *table(head, rows)]
    steps = Counter()
    for day in report["days"]:
        steps.update(day["seconds"])
        if day.get("tables"):
            lines += size_lines(f"Size report, day {day['day']}", day["tables"])
        real = [[r["metric"], r["generated"], r["real_24h"]] for r in day["real_24h"]]
        lines += [*table(("Metric", f"Day {day['day']}", "real_24h"), real)]
    if report.get("final"):
        lines += size_lines("Size report after the days and the calculations", report["final"]["tables"])
    lines += ["", "## Wall time per step", *table(("Step", "Seconds"), sorted(steps.items()))]
    calculated = [c for c in report["calculation"] if c]
    rows = [[c["kind"], c["seconds"], mb((c["peak_rss_kb"] or 0) * 1024), c["attempts"]] for c in calculated]
    lines += ["", "## Calculation", *table(("Kind", "Seconds", "Peak MB", "Attempts"), rows)]
    lines += check_lines(report["checks"])
    lines += ["", "## Basis", ""] + [f"- {key}: {text}" for key, text in report["basis"].items()]
    return "\n".join(lines) + "\n"


def write(out: Path, state: dict, profile: dict) -> None:
    report = build(state, profile)
    write_private(Path(out) / "report.json", json.dumps(report, indent=2, default=str))
    write_private(Path(out) / "report.md", markdown(report))
