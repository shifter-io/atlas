"""SCORE-DATA-1b: the report of `simulate.py --add-scored-inputs`, of --plan-only and of the run:
score-inputs.md in the load's --out (mode 600), and its totals for the command's JSON on stdout.
Per day and network: the batches, runs and rows by kind (a pulse target's or the load), the
outage plan, the uptime each window will show, the load factor and the pulse days; and the
minutes. Counts and the plan only: never an IP, host or path."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean

import simulate_scored as model
from replay_report import table, write_private

NAME = "score-inputs.md"
ROWS = 3  # a run's test_runs, batch_runs and run_buckets rows; a load run adds its IP rows


@dataclass(frozen=True)
class Given:
    """What the report is about: the mode's inputs, plan, seed, load_references (day -> slug ->
    country), pulse-day sources ((network, day) -> day) and --pulse-days (network -> days)."""

    inputs: model.Inputs
    plan: list
    seed: str
    references: dict
    source: dict
    pulse_days: dict


def kind_of(run: model.Run) -> str:
    return "load" if run.kind == "load" else f"pulse {run.target}"


def tally(given: Given) -> tuple[dict, dict]:
    """day -> network -> kind -> batches, runs and rows; day -> network -> its load factor."""
    inputs, found, factors = given.inputs, {}, {}
    for day in range(1, inputs.days + 1):
        found[day], loads = {}, set()
        args = (inputs, given.plan, given.seed, given.references.get(day, {}), day)
        for run in (r for hour in range(24) for r in model.hour_runs(*args, hour)):
            own = found[day].setdefault(run.network, {}).setdefault(kind_of(run), [set(), 0, 0])
            own[0].add(run.batch)
            own[1] += 1
            own[2] += ROWS + (min(run.successes, model.LOAD_IPS) if run.kind == "load" else 0)
            if run.kind == "load":
                loads.add(run.network)
        factors[day] = {n: round(model.load_factor(given.seed, n, day), 3) for n in sorted(loads)}
    return found, factors


def build(given: Given, mode: str, rate: float | None, minutes: float | None) -> dict:
    """The report: rows per kind, outages, uptimes, load factors, pulse days and the minutes
    (estimated at the load's insert rate, rows a second; taken, for a run)."""
    found, factors = tally(given)
    inputs, days, totals, batches = given.inputs, [], {}, set()
    for day, networks in found.items():
        for network, kinds in sorted(networks.items()):
            for kind, (own, runs, rows) in sorted(kinds.items()):
                days.append({"day": day, "network": network, "kind": kind, "batches": len(own)})
                days[-1] |= {"runs": runs, "rows": rows}
                total = totals.setdefault(kind, {"runs": 0, "rows": 0})
                total["runs"], total["rows"] = total["runs"] + runs, total["rows"] + rows
                batches |= own
    rows = sum(total["rows"] for total in totals.values()) + len(batches)
    estimate = round(rows / rate / 60, 1) if rate else None
    report = {"mode": mode, "batches": len(batches), "rows": rows, "kinds": totals, "days": days}
    report["load_factor"] = factors
    report["outages"] = [outage(inputs, o) for o in given.plan]
    report["uptime"] = {
        n: {f"{h}h": model.uptime(inputs, given.plan, n, h) for h in model.WINDOWS}
        for n in inputs.networks
    }
    report["pulse_days"] = {"given": given.pulse_days or "default"} | {
        n: {d: given.source.get((n, d), d) for d in inputs.pulse_days(n)} for n in inputs.networks
    }
    report["minutes"] = {"estimated": estimate, "rows_per_second": rate, "taken": minutes}
    return report


def outage(inputs: model.Inputs, o: model.Outage) -> dict:
    start, end = o.span(inputs)
    found = {"network": o.network, "day": o.day, "tier": o.tier, "countries": len(o.countries)}
    found |= {"start": f"{start:%H:%M}", "minutes": round((end - start).total_seconds() / 60)}
    return found | {"open": o.slots is None}


def load_rate(out: Path) -> float | None:
    """The load's insert rate: the mean of report.json's days[].rows_per_second."""
    path = out / "report.json"
    found = json.loads(path.read_text())["days"] if path.exists() else []
    rates = [day["rows_per_second"] for day in found if day.get("rows_per_second")]
    return round(fmean(rates)) if rates else None


def markdown(report: dict) -> str:
    minutes, pulse = report["minutes"], report["pulse_days"]
    lines = ["# Score inputs added to a load (SCORE-DATA-1)", "", f"Mode: {report['mode']}."]
    lines += [f"Batches: {report['batches']}; rows: {report['rows']}."]
    lines += [f"Minutes: estimated {minutes['estimated']} at {minutes['rows_per_second']} rows/s"]
    lines[-1] += f"; taken {minutes['taken']}." if minutes["taken"] is not None else "."
    lines += [f"Pulse days: {pulse['given']}."]
    keys = ("day", "network", "kind", "batches", "runs", "rows")
    rows = [[d[k] for k in keys] for d in report["days"]]
    lines += ["", "## Batches, runs and rows", *table([k.capitalize() for k in keys], rows)]
    rows = [[day, n, f] for day, own in report["load_factor"].items() for n, f in own.items()]
    lines += ["", "## Load factor", *table(("Day", "Network", "Load factor"), rows)]
    keys = ("network", "day", "tier", "countries", "start", "minutes", "open")
    rows = [[o[k] for k in keys] for o in report["outages"]]
    head = ("Network", "Day", "Tier", "Countries", "Start (UTC)", "Minutes", "Open at the end")
    lines += ["", "## Outage plan", *table(head, rows)]
    rows = [[n, *own.values()] for n, own in report["uptime"].items()]
    lines += ["", "## Expected uptime (%)", *table(("Network", "24h", "168h", "720h"), rows)]
    rows = [[n, d, s] for n, own in pulse.items() if n != "given" for d, s in own.items()]
    lines += ["", "## Pulse days", *table(("Network", "Day", "Countries and IPs of day"), rows)]
    return "\n".join(lines) + "\n"


def write(out: Path, report: dict) -> dict:
    """score-inputs.md in --out (mode 600); returns the totals for stdout."""
    write_private(out / NAME, markdown(report))
    open_now = sum(o["open"] for o in report["outages"])
    found = {k: report[k] for k in ("mode", "batches", "rows", "kinds", "minutes")}
    return found | {"outages": len(report["outages"]), "open": open_now, "report": NAME}
