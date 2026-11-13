#!/usr/bin/env python3
"""Merge the minis' engine benches (scripts/browser_bench.py) into one comparison report.

    uv run python scripts/browser_bench_report.py DIR... --out report.md

Each DIR is one mini's bench --out folder (<engine>/<target>/ with summary.json, tries.jsonl
and samples.jsonl). The report has one table of outcomes, request time and bytes per target
and engine (all minis' tries together), one table of CPU and RAM per mini and engine (all its
targets' samples together) and a plain-words verdict on CPU and RAM. Minis are named by their
logical host name only: no host names, IPs or local paths.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from browser_bench import ENGINES, summarize

from atlas.browser import procstats
from atlas.browser.contracts import HOST_NAME_PATTERN

TITLES = {"camoufox": "Camoufox", "chrome": "Chrome"}
OUTCOME_COLUMNS = ("ok", "challenge", "block", "timeout", "proxy_error", "other")


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load(directories: list[Path]) -> list[dict[str, Any]]:
    """Every bench under the folders: its summary, try records and samples, and its mini."""
    benches = []
    for number, directory in enumerate(directories, 1):
        for path in sorted(directory.glob("*/*/summary.json")):
            summary = json.loads(path.read_text())
            host = summary.get("host", "")
            mini = host if re.fullmatch(HOST_NAME_PATTERN, host) else f"mini {number}"
            records = _lines(path.parent / "tries.jsonl")
            samples = _lines(path.parent / "samples.jsonl")
            benches.append(summary | {"mini": mini, "records": records, "lines": samples})
    return benches


def _share(count: int, total: int) -> str:
    return f"{count} ({count / total:.1%})" if total else "0"


def _size(value: float | None) -> str:
    if value is None:
        return "-"
    for unit in ("B", "KiB", "MiB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.2f} GiB"


def _number(value: float | None, unit: str = "") -> str:
    return "-" if value is None else f"{value:,.0f}{unit}"


def _groups(benches: list[dict], key: str) -> dict[tuple[str, str], list[dict]]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for bench in benches:
        groups[(bench[key], bench["engine"])].append(bench)
    return dict(sorted(groups.items()))


def machine(benches: list[dict]) -> dict[str, Any]:
    """The CPU and RAM of benches together: all their samples, and CPU seconds per try."""
    totals = procstats.summary(line for bench in benches for line in bench["lines"])
    seconds = [bench["cpu_seconds"] for bench in benches if bench.get("cpu_seconds") is not None]
    tries = sum(bench["tries"] for bench in benches)
    totals["cpu_per_try"] = sum(seconds) / tries if seconds and tries else None
    records = [record for bench in benches for record in bench["records"]]
    ok = summarize(records, [], {})["outcomes"]["ok"]
    totals["success"] = ok / len(records) if records else None
    return totals


def target_table(benches: list[dict]) -> list[str]:
    head = "| target | engine | tries | success | challenge | block | timeout | proxy error | other"
    lines = [head + " | avg ms | median ms | p95 ms | bytes per try |", "|---" * 13 + "|"]
    for (target, engine), group in _groups(benches, "target").items():
        records = [record for bench in group for record in bench["records"]]
        merged = summarize(records, [], {})
        shares = [_share(merged["outcomes"][name], len(records)) for name in OUTCOME_COLUMNS]
        times = [_number(merged["request_ms"][name]) for name in ("average", "median", "p95")]
        size = _size(merged["bytes"]["per_try"])
        row = [target, TITLES[engine], str(len(records)), *shares, *times, size]
        lines.append("| " + " | ".join(row) + " |")
    return lines


def machine_table(benches: list[dict]) -> list[str]:
    head = "| mini | engine | tries | CPU % avg | CPU % peak | RAM avg | RAM peak | CPU s per try |"
    lines = [head, "|---" * 8 + "|"]
    for (mini, engine), group in _groups(benches, "mini").items():
        totals = machine(group)
        cpu, rss = totals["cpu_percent"], totals["rss_bytes"]
        per_try = "-" if totals["cpu_per_try"] is None else f"{totals['cpu_per_try']:.2f}"
        row = [mini, TITLES[engine], str(sum(bench["tries"] for bench in group))]
        row += [_number(cpu["average"], "%"), _number(cpu["peak"], "%")]
        row += [_size(rss["average"]), _size(rss["peak"]), per_try]
        lines.append("| " + " | ".join(row) + " |")
    return lines


def _less(low: float, high: float) -> str:
    return f"{(high - low) / high:.0%} less" if high else "the same"


def verdict(totals: dict[str, dict[str, Any]]) -> list[str]:
    """Plain words: which engine costs the minis less CPU (per try) and RAM (on average)."""
    if set(totals) != set(ENGINES):
        return [f"Only {', '.join(TITLES[e] for e in sorted(totals))} was benched: no verdict."]
    needed = [totals[e]["cpu_per_try"] for e in ENGINES]
    needed += [totals[e]["rss_bytes"]["average"] for e in ENGINES]
    if None in needed:
        return ["CPU or RAM was not sampled for an engine: no verdict."]
    cpu = sorted(ENGINES, key=lambda e: totals[e]["cpu_per_try"])
    ram = sorted(ENGINES, key=lambda e: totals[e]["rss_bytes"]["average"])
    low, high = (totals[e] for e in cpu)
    lines = [
        f"- CPU: per try, {TITLES[cpu[0]]} used {low['cpu_per_try']:.2f} CPU seconds against "
        f"{TITLES[cpu[1]]}'s {high['cpu_per_try']:.2f} "
        f"({_less(low['cpu_per_try'], high['cpu_per_try'])}). On average the browsers kept "
        f"{_number(low['cpu_percent']['average'], '%')} of one core busy with {TITLES[cpu[0]]} "
        f"and {_number(high['cpu_percent']['average'], '%')} with {TITLES[cpu[1]]}.",
    ]
    low, high = (totals[e] for e in ram)
    lines.append(
        f"- RAM: {TITLES[ram[0]]} held {_size(low['rss_bytes']['average'])} on average (peak "
        f"{_size(low['rss_bytes']['peak'])}) against {TITLES[ram[1]]}'s "
        f"{_size(high['rss_bytes']['average'])} (peak {_size(high['rss_bytes']['peak'])}): "
        f"{_less(low['rss_bytes']['average'], high['rss_bytes']['average'])}."
    )
    lines.append("- CPU is compared per try: a slower engine shows a lower CPU % for less work.")
    success = ", ".join(f"{TITLES[e]} {totals[e]['success']:.1%}" for e in ENGINES)
    lines.append(f"- Success over all targets: {success} of their tries.")
    if cpu[0] == ram[0]:
        lines.append(f"\n**Verdict:** {TITLES[cpu[0]]} is better for the minis' CPU and RAM.")
    else:
        lines.append(
            f"\n**Verdict:** mixed: {TITLES[cpu[0]]} needs less CPU, {TITLES[ram[0]]} less RAM."
        )
    return lines


def report(benches: list[dict]) -> str:
    engines = {}
    for bench in benches:
        identity = bench.get("identity") or {}
        version = identity.get("release") or identity.get("version") or "unknown"
        engines.setdefault(bench["engine"], f"{TITLES[bench['engine']]} {version}")
    totals = {engine: machine([b for b in benches if b["engine"] == engine]) for engine in engines}
    minis = sorted({bench["mini"] for bench in benches})
    lines = [
        "# Browser engines compared: Camoufox and Chrome",
        "",
        f"- Minis: {', '.join(minis)}; engines: {', '.join(engines[e] for e in sorted(engines))}",
        f"- Networks: {', '.join(sorted({bench['network'] for bench in benches}))}",
        f"- Browsers at once: {', '.join(sorted({str(bench['browsers']) for bench in benches}))}",
        "",
        "## Verdict",
        "",
        *verdict(totals),
        "",
        "## Outcomes and request time per target (all minis)",
        "",
        *target_table(benches),
        "",
        "## CPU and RAM per mini (all targets; 100% = one core busy)",
        "",
        *machine_table(benches),
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dirs", type=Path, nargs="+", help="One mini's bench --out folder")
    parser.add_argument("--out", type=Path, required=True, help="The report (Markdown)")
    args = parser.parse_args(argv)
    benches = load(args.dirs)
    if not benches:
        print("browser_bench_report: no summary.json under the folders", file=sys.stderr)
        return 2
    args.out.write_text(report(benches))
    print(f"{len(benches)} benches merged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
