"""SIM-LOAD-1: the loader's figures, its plan (--plan-only) and its report (SIMULATE.md, "The
report"), with names and counts only, never an address, a host name or a path."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace

from profile_reference import TABLES
from replay_report import mb, table, write_private
from simulate_browser import dig

GB = 2**30
KINDS = ("daily", "browser", "pulse")
FIELDS = ("rows", "table_bytes", "index_bytes")
CADENCE = (
    "one daily test per network × country per day; one 100-try browser run per network × "
    "target per day; no HTTP pulse fleet, so no pulses, census or load waves unless switched on"
)
BASIS = {
    "growth": "the day's change of count(*), pg_table_size and pg_indexes_size (partitions in)",
    "expected": "the reference's mean rows and bytes per run (its basis: kinds) × the day's runs",
    "real_24h": "real_24h summed over its networks (one run per network, region and hour)",
}
# (label, the generated day's figure, real_24h's figure)
REAL = (("unique IPs", "unique_ips", "unique_ips_hour_sum"), ("tries", "tries", "browser_tries"))
REAL += (
    ("requests", "requests", "attempted_requests"),
    ("browser runs", "browser", "browser_runs"),
)
REAL += (("bundle bytes", "bundle_bytes", "bundle_bytes_sampled"),)


def figures(hour, found: dict) -> dict:
    """An hour's runs as the report and the size check read them: per daily test its network,
    rows per table (TABLES' order), unique IPs and requests; the browser runs' totals."""
    daily = []
    for test in hour.daily:
        own = found[test.run_id]
        rows = [own["tables"][name] for name in TABLES]
        daily.append([test.lane.network, rows, test.size, own["requests"]])
    tries = sum(len(run.tries) for run in hour.browser)
    return {
        "daily": daily,
        "tries": tries,
        "bundle_bytes": sum(r.bundle_bytes for r in hour.browser),
    }


def stored(model, records: list[dict]) -> list:
    """The stored hours as simulate_check.size reads them: each daily test's recorded rows and
    unique IPs with the model's lane, its blocks (the IPs it shares) and the model (its releases;
    the figures keep the hour's test order), and the model's browser runs."""
    hours = []
    for record in records:
        day, hour = record["day"] + 1, datetime.fromisoformat(record["start"]).hour
        own, daily = model.hour(day, hour), []
        for test, (_, rows, size, *_) in zip(own.daily, record["figures"]["daily"], strict=True):
            tables = partial(dict, zip(TABLES, rows, strict=True))  # a call reads the zip once
            own_test = {"lane": test.lane, "blocks": test.blocks, "model": model}
            daily.append(SimpleNamespace(tables=tables, size=size, **own_test))
        hours.append(SimpleNamespace(day=day, daily=daily, browser=own.browser))
    return hours


def expected(profile: dict, runs: dict) -> dict:
    """Per table, the reference's mean rows and bytes per run × the runs of each kind."""
    found = {}
    for kind, count in runs.items():
        reference = dig(profile, "reference", kind)
        for name, spread in dig(reference, "rows_per_run").items():
            per_run = dig(reference, "bytes", "per_run", name) | {"rows": spread.get("mean")}
            entry = found.setdefault(name, dict.fromkeys(FIELDS, 0))
            for field in FIELDS:
                entry[field] += round((per_run.get(field) or 0) * count)
    return found


def plan(model, profile: dict, days: int, hours: int, pulses: bool) -> dict:
    """Per simulated day its runs by kind (pulses only with `pulses`) and the rows and bytes the
    reference gives them, then the totals."""
    found, totals = [], Counter()
    for day in range(1, days + 1):
        own = [model.hour(day, h) for h in range(hours)]
        runs = {
            "daily": sum(len(h.daily) for h in own),
            "browser": sum(len(h.browser) for h in own),
        }
        runs |= {"pulse": sum(len(h.pulses) for h in own)} if pulses else {}
        found.append({"title": f"day {day}", "runs": runs, "tables": expected(profile, runs)})
        totals.update(runs)
    total = {"title": f"total of {days} days", "runs": dict(totals)}
    return {"days": found, "totals": total | {"tables": expected(profile, totals)}}


def plan_table(found: dict) -> str:
    lines, head = [], f"  {'table':<20}{'rows':>14}{'table GB':>11}{'index GB':>11}"
    for entry in [*found["days"], found["totals"]]:
        runs = ", ".join(f"{kind} {n}" for kind, n in entry["runs"].items())
        lines += [f"{entry['title']}: {runs}", head]
        for name, size in sorted(entry["tables"].items()):
            gbs = "".join(f"{size[field] / GB:>11.3f}" for field in FIELDS[1:])
            lines.append(f"  {name:<20}{size['rows']:>14}{gbs}")
    return "\n".join(lines + real_lines(found.get("real"))) + "\n"


def real_lines(real: dict | None) -> list[str]:
    """--real-hours: each day's real hour and the networks without its file; the countries no
    formula schedules and the source's networks without a formula (no test)."""
    if not real:
        return []
    lines = ["", "## Real hours", ""]
    for day, hour in enumerate(real["hours"], 1):
        missing = ", ".join(real["missing"][str(day)]) or "none"
        lines.append(f"- day {day}: real hour {hour:02d}, networks without it: {missing}")
    lines.append(f"- not in a formula's schedule: {', '.join(real['unscheduled']) or 'none'}")
    return lines + [f"- networks without a formula: {', '.join(real['ignored']) or 'none'}"]


def growth(before: dict, after: dict, wanted: dict) -> dict:
    """Each table whose rows or bytes changed: its figures, its growth and the expected one."""
    found = {}
    for name, now in sorted(after["tables"].items()):
        was = before["tables"].get(name, dict.fromkeys(FIELDS, 0))
        grew = {field: now[field] - was[field] for field in FIELDS}
        if any(grew.values()):
            mine = wanted.get(name.removeprefix("public."))
            found[name] = now | {"growth": grew, "expected": mine}
    return found


def day_row(state: dict, profile: dict, day: int, before: dict) -> dict:
    """A simulated day: its runs, wall seconds per step, sizes and real_24h figures."""
    records = [r for r in state["hours"] if r["day"] == day - 1 and "figures" in r]
    runs, took, tests = Counter(), Counter(), []
    for record in records:
        runs.update(record["runs"])
        tests += record["figures"]["daily"]
        for name, step in record["ingest"]["steps"].items():
            took[name] += sum(part.get("seconds") or 0 for part in step["parts"])
    row = {"day": day, "hours": len(records), "runs": dict(runs)}
    row["seconds"] = {name: round(value, 3) for name, value in took.items()}
    if after := state["sizes"].get(str(day)):
        row["tables"] = growth(before, after, expected(profile, runs))
        rows = sum(entry["growth"]["rows"] for entry in row["tables"].values())
        row |= {"rows": rows, "rows_per_second": round(rows / t) if (t := took.total()) else None}
    mine = {"unique_ips": sum(t[2] for t in tests), "requests": sum(t[3] for t in tests)}
    mine["browser"] = runs["browser"]
    mine |= {name: sum(r["figures"][name] for r in records) for name in ("tries", "bundle_bytes")}
    real = dig(profile, "real_24h").values()
    for label, ours, theirs in REAL:
        found = {"metric": label, "real_24h": sum(n.get(theirs) or 0 for n in real)}
        row.setdefault("real_24h", []).append(found | {"generated": mine[ours]})
    return row


def build(state: dict, profile: dict) -> dict:
    """The report of a load's state (written after a stop too)."""
    days, before, runs = [], state["baseline"]["sizes"], Counter()
    for day in range(1, state["days"]):
        days.append(day_row(state, profile, day, before))
        before = state["sizes"].get(str(day)) or before
        runs.update(days[-1]["runs"])
    kinds = {kind: dig(profile, "reference", kind).get("basis") for kind in KINDS}
    report = {"cadence": CADENCE, "basis": BASIS | {"kinds": kinds}, "days": days}
    report |= {"first_day": state["first_day"], "resumed": len(state["resumed"])}
    report["real"] = state.get("real")
    if "final" in state["sizes"]:
        found = growth(state["baseline"]["sizes"], state["sizes"]["final"], expected(profile, runs))
        report["final"] = {"runs": dict(runs), "tables": found}
    report["calculation"] = []
    for work in (record["calculate"] for record in state["hours"][-2:]):
        if work.get("done"):
            last = work["attempts"][-1]
            found = {"kind": work["kind"], "attempts": len(work["attempts"])}
            found |= {key: last[key] for key in ("seconds", "peak_rss_kb")}
            report["calculation"].append(found | {"siteapi": work.get("siteapi")})
    return report | {"counts": state["counts"], "checks": state.get("checks")}


def size_lines(title: str, found: dict) -> list[str]:
    head = ("Table", "Rows", "Table MB", "Index MB", "+Rows", "+Table MB", "+Index MB")
    head += ("Expected rows", "Expected table MB", "Expected index MB")
    rows = []
    for name, entry in found.items():
        mine = [entry[f] for f in FIELDS] + [entry["growth"][f] for f in FIELDS]
        mine += [(entry["expected"] or {}).get(f) for f in FIELDS]
        rows.append([name] + [n if i % 3 == 0 else mb(n) for i, n in enumerate(mine)])
    return ["", f"## {title}", *table(head, rows)]


def markdown(report: dict) -> str:
    lines = [
        "# Generated days loaded into staging (SIM-LOAD-1)",
        "",
        f"Cadence: {report['cadence']}.",
    ]
    head = ("Day", "Hours", "Daily", "Browser", "Pulses", "Rows", "Rows/s", "Ingest s")
    rows = [
        [d["day"], d["hours"], *(d["runs"].get(kind, 0) for kind in KINDS), d.get("rows")]
        + [d.get("rows_per_second"), round(sum(d["seconds"].values()), 1)]
        for d in report["days"]
    ]
    lines += ["", "## Days", *table(head, rows)]
    steps = Counter()
    for day in report["days"]:
        steps.update(day["seconds"])
        lines += size_lines(f"Size report, day {day['day']}", day.get("tables") or {})
        real = [[r["metric"], r["generated"], r["real_24h"]] for r in day["real_24h"]]
        lines += table(("Metric", f"Day {day['day']}", "real_24h"), real)
    if report.get("final"):
        lines += size_lines("Size report after the calculations", report["final"]["tables"])
    lines += ["", "## Wall time per step", *table(("Step", "Seconds"), sorted(steps.items()))]
    head = ("Kind", "Seconds", "Peak MB", "Attempts")
    found = report["calculation"]
    rows = [[c["kind"], c["seconds"], mb(c["peak_rss_kb"] * 1024), c["attempts"]] for c in found]
    lines += ["", "## Calculation", *table(head, rows), "", "## Checks"]
    for name, found in (report["checks"] or {}).items():
        failed = sorted({c.get("network") or "all" for c in found["checks"] if not c["pass"]})
        lines.append(
            f"- {name}: {found['compared']} compared, failed: {', '.join(failed) or 'none'}"
        )
    lines += ["", "## Basis", ""] + [f"- {key}: {text}" for key, text in report["basis"].items()]
    lines += real_lines(report.get("real"))
    return "\n".join(lines) + "\n"


def write(out: Path, state: dict, profile: dict) -> None:
    report = build(state, profile)
    write_private(Path(out) / "report.json", json.dumps(report, indent=2, default=str))
    write_private(Path(out) / "report.md", markdown(report))
