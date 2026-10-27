#!/usr/bin/env python3
"""Render a standalone private report from a collected onboarding probe summary."""

import argparse
import html
import json
from pathlib import Path

from proxy_index.onboarding.files import atomic_bytes
from proxy_index.regions import load_regions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--baseline", type=Path, help="Previous private probe directory")
    parser.add_argument("--telemetry", type=Path, help="Saved local monitor JSONL samples")
    args = parser.parse_args()
    state = json.loads((args.directory / "summary.json").read_text())
    measured = state["measurement"]
    region = next(r for r in load_regions().regions if r.slug == state["region"])
    elapsed = measured["duration_seconds"]
    remaining = max(0, 3600 - state["total_duration_seconds"])
    others = len(region.countries) - 1
    average = remaining / others if others else 0
    esc = html.escape
    extra = ""
    samples = []
    if args.telemetry:
        samples = [json.loads(line)["node"] for line in args.telemetry.read_text().splitlines()]
        live = [
            n
            for n in samples
            if n.get("state", {}).get("status") == "running"
            and sum(
                p.get("role") == "http_worker" and "cpu_pct" in p for p in n.get("processes", [])
            )
            == 4
        ]
        core_rows = []
        for core in ("cpu0", "cpu1", "cpu2", "cpu3"):
            values = [n["core_cpu_pct"][core] for n in live if core in n.get("core_cpu_pct", {})]
            if values:
                core_rows.append(
                    f"<tr><td>{core}</td><td>{sum(values) / len(values):.1f}%</td>"
                    f"<td>{min(values):.1f}%</td><td>{max(values):.1f}%</td></tr>"
                )
        pids = sorted({p["pid"] for n in live for p in n.get("processes", [])})
        worker_rows = []
        for pid in pids:
            values = [p for n in live for p in n["processes"] if p["pid"] == pid]
            cpu = [p["cpu_pct"] for p in values if "cpu_pct" in p]
            if cpu:
                worker_rows.append(
                    f"<tr><td>{esc(values[0]['role'])}</td><td>{pid}</td>"
                    f"<td>{sum(cpu) / len(cpu):.1f}%</td>"
                    f"<td>{max(p['rss_mb'] for p in values):.1f} MiB</td></tr>"
                )
        extra += f"""<section><h2>CPU evidence from production</h2>
<p>{len(live)} running samples with CPU readings for all four HTTP workers.
CPU percentages use the interval between SSH samples (approximately 13 seconds).
For one process, 100% means one occupied core. Core totals include other host activity.</p>
<table><thead><tr><th>Core</th><th>Mean usage</th><th>Minimum</th><th>Maximum</th></tr></thead>
<tbody>{"".join(core_rows)}</tbody></table>
<table><thead><tr><th>Role</th><th>PID</th><th>Mean CPU</th><th>Peak sampled RSS</th></tr></thead>
<tbody>{"".join(worker_rows)}</tbody></table>
<p><a href="cpu-samples.jsonl">Raw CPU and progress samples</a></p></section>"""
        atomic_bytes(args.directory / "cpu-samples.jsonl", args.telemetry.read_bytes())
    if args.baseline:
        before = json.loads((args.baseline / "summary.json").read_text())["measurement"]
        rows = []
        for label, key in (
            ("Tester duration (seconds)", "duration_seconds"),
            ("Completed requests", "completed"),
            ("Unique IPs", "unique_ips"),
            ("Errors", "total_errors"),
            ("Requests/second", "requests_per_second"),
        ):
            old_value = (
                f"{before[key]:,}" if isinstance(before[key], int) else f"{before[key]:,.2f}"
            )
            value = (
                f"{measured[key]:,}" if isinstance(measured[key], int) else f"{measured[key]:,.2f}"
            )
            rows.append(f"<tr><td>{label}</td><td>{old_value}</td><td>{value}</td></tr>")
        extra += f"""<section><h2>Comparison with the first run</h2><table><thead><tr>
<th>Measurement</th><th>Previous execution</th><th>Four workers</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
<p>Both runs used US targeting, 2,500 concurrency, a 10-second timeout, and the same census rule.
These are separate live samples, with different request totals and observed pools.
The comparison is not a fixed-work CPU benchmark.</p></section>"""
    metrics = [
        ("Tester duration", f"{elapsed:.3f} s ({elapsed / 60:.2f} min)"),
        ("Including finalization", f"{state['total_duration_seconds']:.3f} s"),
        ("Total completed requests", f"{measured['completed']:,}"),
        ("Total unique IPs", f"{measured['unique_ips']:,}"),
        ("Successful responses", f"{measured['successes']:,}"),
        ("Failed requests", f"{measured['total_errors']:,}"),
        ("Throughput", f"{measured['requests_per_second']:,.1f} requests/s"),
        ("Peak coordinator memory", f"{state['peak_memory_mb']:.1f} MiB"),
    ]
    if samples:
        memory = max(n.get("rss_mb") or 0 for n in samples)
        metrics.append(("Peak sampled combined RSS", f"{memory:.1f} MiB"))
    cards = "".join(
        f'<div class="metric"><small>{esc(k)}</small><b>{esc(v)}</b></div>' for k, v in metrics
    )
    errors = "".join(f"<li>{esc(k)}: {v:,}</li>" for k, v in measured["errors"].items())
    codes = ", ".join(f"{esc(k)}: {v:,}" for k, v in measured["status_codes"].items())
    page = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(state["country"].upper())} census calibration</title><style>
body{{font:16px/1.6 system-ui;color:#1b2b40;background:#f4f7fa;margin:0}}
main{{max-width:1100px;padding:36px 24px;margin:auto}}h1{{font-size:36px;line-height:1.2}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}}
.metric,section{{padding:20px;background:white;border:1px solid #d7e2eb;border-radius:10px}}
.metric small{{display:block;color:#52657a}}.metric b{{display:block;font-size:24px;margin-top:8px}}
section{{margin-top:24px}}code{{overflow-wrap:anywhere}}
table{{width:100%;border-collapse:collapse;margin:16px 0;font-size:14px}}
th,td{{text-align:left;padding:10px;border-bottom:1px solid #d7e2eb}}th{{background:#edf2f7}}
</style><main><h1>{esc(state["country"].upper())} census calibration · {esc(state["region"])}</h1>
<p>{esc(state["provider"])} / {esc(state["product"])}
 · concurrency {state["settings"]["concurrency"]:,}
· timeout {state["settings"]["timeout_seconds"]} seconds
· HTTP workers {measured["config"].get("execution_processes", 1)}</p><div class="grid">{cards}</div>
{extra}
<section><h2>Run evidence</h2><p>Started: {esc(state["started_at"])}<br>
Finished: {esc(state["finished_at"])}<br>Status: {esc(state["status"])}<br>
Stop reason: <code>{esc(measured["stop_reason"])}</code></p>
<p>Census v2 stops after at least 1,000 completions when unique IPs / completed requests falls below
50%. Failures count in the denominator. Already-started requests finish before normal closure.</p>
<h3>Errors</h3><ul>{errors or "<li>No failed requests.</li>"}</ul>
<p>HTTP status counts: {codes}</p></section>
<section><h2>What this means for the hourly round</h2>
<p>{esc(state["region"])} has {len(region.countries)} assigned countries. This probe measured one.
Using total time including finalization, it leaves {remaining / 60:.2f} minutes of a 60-minute round
for the other {others} countries: an average budget of {average:.2f} seconds each.</p>
<p>This is a remaining-time budget, not a prediction. Country pool sizes, duplicate rates and errors
can differ substantially. Confirming the full regional round requires measuring all its countries.
No full regional or 24-hour campaign was launched as part of this calibration.</p></section>
<section><h2>Private artifacts</h2><p><a href="summary.json">Exact JSON summary</a> ·
<a href="state.json">Final status checkpoint</a></p><p>All successful responses,
including duplicates,
are retained in the adjacent compressed observation chunks.
These files remain local and Git-ignored.</p>
</section></main></html>"""
    atomic_bytes(args.directory / "report.html", page.encode())
    print(args.directory / "report.html")


if __name__ == "__main__":
    main()
