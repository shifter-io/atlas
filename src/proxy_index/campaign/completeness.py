"""Per-cell completeness of a campaign: one row per network, region and hour, with no IPs.

Each pulled round is checked by `onboarding.rounds.verify_round` (chunk SHA-256, rows, time
bounds and counter reconciliation); a cell without one is judged from the hour ledger. The
outputs extend DATA-1's per-hour format with the campaign's columns.
"""

from __future__ import annotations

from pathlib import Path

from ..onboarding.files import atomic_bytes, atomic_json, read_json
from ..onboarding.manifest import load, moment
from ..onboarding.rounds import TERMINAL, round_directory, verify_round
from ..providers.grammar import grammar_ids, load_grammar
from .contracts import DropletRecord
from .merge import HOURS, cell_status, ledger_droplets, load_spec, manifest_path

DATA1_COLUMNS = ("net", "region", "round", "scheduled", "started", "finished", "completed",
                 "delay", "duration", "countries", "jobs_ok", "missing", "dup", "badchunk",
                 "no_success", "requests", "errors", "observations")
COLUMNS = DATA1_COLUMNS + ("cell", "size", "start_lateness_s", "create_to_ready_s", "overlap",
                           "bytes_up", "bytes_down", "protocol")
STATUSES = ("complete", "partial", "failed_ours", "uncollected", "running", "pending")
MARKS = {"failed_ours": "✗ ours", "uncollected": "⚠ data", "running": "running", "pending": "·"}


def _seconds(start: str | None, end: str | None) -> float | None:
    return round((moment(end) - moment(start)).total_seconds(), 3) if start and end else None


def protocol(provider: str) -> str:
    """The network's echo-test protocol from its grammar; `default` for other adapters."""
    return load_grammar(provider).test_protocol if provider in grammar_ids() else "default"


def _droplet_columns(droplets: list[DropletRecord] | None) -> dict:
    """Timing columns of the cell's last worker (a replacement supersedes the first)."""
    if not droplets:
        return {"size": None, "start_lateness_s": None, "create_to_ready_s": None, "overlap": None}
    last = droplets[-1]
    return {"size": last.size or None, "start_lateness_s": last.start_lateness_seconds,
            "create_to_ready_s": _seconds(last.requested_at, last.ssh_ready_at),
            "overlap": last.overlap}


def _verified(path: Path, region: str, hour: int, state: dict) -> tuple[dict, list[str], bool]:
    """Measured columns, issues and completeness of one pulled round."""
    try:
        result = verify_round(path, region, hour)
    except (OSError, ValueError, KeyError) as exc:
        return {}, [f"round failed verification ({type(exc).__name__})"], False
    countries = result["countries"]
    labels = {f"{c['country']}: {c['status']}" for c in countries}
    labels |= {f"{c['country']}: not started" for c in countries}
    defective = {issue.split(":", 1)[0] for issue in result["issues"]
                 if issue not in labels and not issue.startswith("round is")}
    started = [j["started_at"] for j in state["jobs"] if j.get("started_at")]
    completed = all(c["status"] == "completed" for c in countries)
    finished = max(j["finished_at"] for j in state["jobs"]) if completed and started else None
    first = min(started, default=None)
    return {
        "started": first, "finished": finished, "completed": completed,
        "delay": _seconds(state["scheduled_at"], first), "duration": _seconds(first, finished),
        "jobs_ok": sum(c["status"] == "completed" for c in countries),
        "missing": sum(c["status"] not in TERMINAL - {"not_started_deadline"} for c in countries),
        "dup": len(countries) - len({c["country"] for c in countries}),
        "badchunk": len(defective & {c["country"] for c in countries}),
        "no_success": sum(c["stop_reason"] == "no_success" for c in countries),
        **{key: sum(c[key] for c in countries)
           for key in ("requests", "errors", "observations", "bytes_up", "bytes_down")},
    }, result["issues"], result["complete"]


def cell_row(path: Path, manifest: dict, cell: tuple[str, str, int],
             droplets: list[DropletRecord] | None) -> tuple[dict, list[str]]:
    """One TSV row (a dict over `COLUMNS`) and the issues of a (network, region, hour) cell."""
    network, region, hour = cell
    row = dict.fromkeys(COLUMNS) | {
        "net": network, "region": region, "round": hour, "scheduled": manifest["rounds"][hour],
        "completed": False, "countries": len(manifest["regions"][region]),
        "protocol": protocol(manifest["provider"]),
    } | _droplet_columns(droplets)
    file = round_directory(path, region, hour) / "round.json"
    state = read_json(file) if file.exists() else None
    kind, note = cell_status(state, droplets)
    issues = [note] if note else []
    if state is None or kind == "running":
        return row | {"cell": kind}, issues
    measured, problems, complete = _verified(path, region, hour, state)
    if kind == "pulled":
        kind = "complete" if complete else "partial"
    return row | measured | {"cell": kind}, issues + problems


def _text(value) -> str:
    return "" if value is None else str(value)


def render_tsv(rows: list[dict]) -> str:
    lines = ["\t".join(COLUMNS)]
    lines += ["\t".join(_text(row[key]) for key in COLUMNS) for row in rows]
    return "\n".join(lines) + "\n"


def _mark(row: dict) -> str:
    if row["cell"] not in ("complete", "partial"):
        return MARKS[row["cell"]]
    rate = round(100 * row["errors"] / row["requests"]) if row["requests"] else 0
    return f"{'✓' if row['cell'] == 'complete' else 'partial'} {rate}"


def render_tables(result: dict) -> str:
    """One table per network (rows = regions, columns h00-h23) after the status totals."""
    hours = [f"h{hour:02d}" for hour in range(HOURS)]
    lines = [f"# Campaign {result['campaign_id']}: per-hour completeness", "",
             "Cell = that hour's round of one network in one region. ✓ complete and verified, "
             "`partial` some countries interrupted, failed or unverified (each followed by the "
             "error rate %, errors / requests), ✗ ours = our side failed (create, assign, "
             "boot, start, or the round failed or ended without closing), ⚠ data = the round "
             "ran but its data is not in the campaign root, `running` not finished, · not "
             "launched.", "",
             "| Network | " + " | ".join(STATUSES) + " | cells |",
             "|---|" + "---|" * (len(STATUSES) + 1)]
    for name, counts in [*result["networks"].items(), ("total", result["totals"])]:
        lines.append(f"| {name} | " + " | ".join(str(counts[s]) for s in STATUSES)
                     + f" | {counts['cells']} |")
    for network in result["networks"]:
        lines += ["", f"## {network}", "", "| Region | " + " | ".join(hours) + " |",
                  "|---|" + "---|" * HOURS]
        regions = dict.fromkeys(r["region"] for r in result["rows"] if r["net"] == network)
        for region in regions:
            marks = [_mark(r) for r in result["rows"] if (r["net"], r["region"]) == (
                network, region)]
            lines.append(f"| {region} | " + " | ".join(marks) + " |")
    return "\n".join(lines) + "\n"


def _counts(rows: list[dict]) -> dict:
    return {status: sum(r["cell"] == status for r in rows) for status in STATUSES} | {
        "cells": len(rows)}


def build(root: Path) -> dict:
    """Check every cell of the campaign at `root` and write `completeness/`.

    Writes `completeness.json` (rows, issues, totals per network out of 24 × regions and
    overall), `per-hour-completeness.tsv` (`COLUMNS`, in order) and `per-hour-tables.md`.
    Reads only; a cell may be pending or running. Returns the JSON document.
    """
    spec = load_spec(root)
    droplets = ledger_droplets(root)
    rows, issues = [], []
    for network in spec.networks:
        path = manifest_path(root, network)
        manifest = load(path)
        for region in spec.regions:
            for hour in range(HOURS):
                cell = (network, region, hour)
                row, problems = cell_row(path, manifest, cell, droplets.get(cell))
                rows.append(row)
                issues += [f"{network} {region} h{hour:02d}: {issue}" for issue in problems]
    result = {
        "schema_version": 1, "campaign_id": spec.campaign_id, "cells": len(rows),
        "networks": {n: _counts([r for r in rows if r["net"] == n]) for n in spec.networks},
        "totals": _counts(rows), "rows": rows, "issues": issues,
    }
    folder = root / "completeness"
    atomic_json(folder / "completeness.json", result)
    atomic_bytes(folder / "per-hour-completeness.tsv", render_tsv(rows).encode())
    atomic_bytes(folder / "per-hour-tables.md", render_tables(result).encode())
    return result
