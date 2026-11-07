#!/usr/bin/env python3
"""REPLAY-REPORT-1: the replay's report, from its state and its event log (REPLAY.md, "Report").

Usage: replay_report.py <out dir>   (writes report.md and report.json there)

It holds counts, sizes, times, table names and statement shapes only: no IP, host name,
credential or path (the slices' directory and the retry reasons are left out).
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import measure

GB = 2**30
STATEMENTS = 20  # statement shapes of the whole run in the Markdown, slowest first
# The table a statement writes rows to: an INSERT or a COPY into it.
WRITES = re.compile(r'^(?:INSERT INTO|COPY) (?:"?(\w+)"?\.)?"?(\w+)"?', re.I)
SUMMED = ("tests", "rows_total", "slice_bytes", "growth_bytes", "ingest_seconds")
SUMMED += ("calculate_seconds", "retries", "delay_seconds")
MODELLED = (
    "Byteful rows: a daily test's bytes are modelled, not measured.",
    "HTTP pulses: no real pulse data exists; each run is 100 requests with 0 to 3 timeouts and "
    "geo rows from the slice's enrichment of the country.",
    "Browser data: BROWSER-MODEL-1's modelled browser hour.",
    "Bucket counters and latency: 2 to 4 five-minute buckets per daily test and the network's "
    "modelled latency bins.",
)
IP_TABLES = ("ip_addresses", "ip_enrichment", "product_ips", "pool_ips")
LOWER_BOUND = (
    "The 7 days are copies of one day, so from day 2 every IP recurs: ip_addresses, "
    "ip_enrichment, product_ips and pool_ips do not grow after day 1, whereas in real data "
    "30-60% of a day's IPs recur (#150). Their rows and sizes are a lower bound."
)


def gb(value) -> float | None:
    return None if value is None else round(value / GB, 3)


def seconds_between(work: dict) -> float | None:
    if "started" not in work or "finished" not in work:
        return None
    since = datetime.fromisoformat(work["finished"]) - datetime.fromisoformat(work["started"])
    return round(since.total_seconds(), 3)


def summaries(record: dict):
    """The hour's Tracer summaries: (part, summary) of the ingest and the calculation."""
    for part in ("ingest", "calculate"):
        found = record[part].get("statements")
        if isinstance(found, dict):
            yield part, found


def written(record: dict, tables) -> dict[str, int]:
    """Rows the hour's plain INSERT and COPY statements added, per table of the baseline (replay
    rows only: the ingest writes nothing else; staging and temporary tables are left out). An
    upsert's rowcount includes updated rows and an INSERT inside WITH reports its SELECT's rows:
    neither is counted here (the day's counts are exact)."""
    found: dict[str, int] = {}
    for part, summary in summaries(record):
        for row in summary["shapes"] if part == "ingest" else ():
            match = WRITES.match(row["statement"])
            name = match and f"{match.group(1) or 'public'}.{match.group(2)}"
            if name in tables and row["rows"] and not row.get("upsert"):
                found[name] = found.get(name, 0) + row["rows"]
    return dict(sorted(found.items()))


def slowest(record: dict) -> dict | None:
    found = [s | {"part": part} for part, summary in summaries(record) for s in summary["slowest"]]
    return max(found, key=lambda s: s["seconds"], default=None)


def slice_bytes(state: dict, hour: int) -> int | None:
    names = (f"slice-{hour:02d}.json", f"slice-{hour:02d}.jsonl.gz")
    paths = [Path(state["slices"], name) for name in names]
    return sum(path.stat().st_size for path in paths) if all(p.exists() for p in paths) else None


def hour_row(state: dict, record: dict, events: list[dict], before) -> dict:
    """One simulated hour's numbers; `before` is the database's bytes before the hour."""
    start = datetime.fromisoformat(record["start"])
    daily = record["ingest"]["steps"].get("daily", {})
    rows = written(record, state["baseline"]["counts"])
    work, attempts = record["calculate"], record["calculate"]["attempts"]
    size = record.get("sizes", {}).get("database_bytes")
    retries = [event for event in events if event["hour"] == record["start"]]
    found = {"day": record["day"] + 1, "hour": start.hour, "start": record["start"]}
    found |= {"tests": (daily.get("parts") or [{}])[-1].get("tests", 0)}
    found |= {"rows": rows, "rows_total": sum(rows.values())}
    found |= {"slice_bytes": slice_bytes(state, start.hour), "database_bytes": size}
    found["growth_bytes"] = None if None in (size, before) else size - before
    paused = sum(e["pause"] for e in retries if e["step"] != "calculate")
    wall = seconds_between(record["ingest"])  # without its retries' pauses (delay_seconds)
    found["ingest_seconds"] = None if wall is None else round(max(wall - paused, 0), 3)
    found["per_test"] = measure.percentiles(daily.get("test_seconds", []))
    done = attempts[-1] if work["done"] and attempts else {}
    found |= {"calculate_seconds": done.get("seconds"), "peak_rss_kb": done.get("peak_rss_kb")}
    found |= {"calculated": work["done"], "slowest": slowest(record)}
    found["all_databases_bytes"] = record.get("sizes", {}).get("all_databases_bytes")
    return found | {"retries": len(retries), "delay_seconds": sum(e["pause"] for e in retries)}


def replay_growth(state: dict, day: int) -> dict[str, int] | None:
    """The replay's rows a counted day added per table (replay_baseline.day_counts), exact."""
    counted = state["days_counted"]
    if str(day - 1) not in counted:
        return None
    now, before = counted[str(day - 1)]["replay"], counted.get(str(day - 2), {}).get("replay", {})
    found = {name: count - before.get(name, 0) for name, count in sorted(now.items())}
    return {name: count for name, count in found.items() if count}


def day_row(state: dict, day: int, hours: list[dict]) -> dict:
    found = {"day": day, "hours": len(hours), "replay_rows": replay_growth(state, day)}
    found |= {key: round(sum(h[key] or 0 for h in hours), 3) for key in SUMMED}
    rows: dict[str, int] = {}
    for hour in hours:
        for name, count in hour["rows"].items():
            rows[name] = rows.get(name, 0) + count
    timings = [h["calculate_seconds"] for h in hours if h["calculate_seconds"] is not None]
    found |= {"rows": dict(sorted(rows.items())), "calculate": measure.percentiles(timings)}
    found["peak_rss_kb"] = max((h["peak_rss_kb"] or 0 for h in hours), default=0)
    sized = [h for h in hours if h["database_bytes"] is not None] or [{}]
    found["database_gb"] = gb(sized[-1].get("database_bytes"))
    return found | {"server_gb": gb(sized[-1].get("all_databases_bytes"))}


def quantile(histogram: dict, share: int) -> float | None:
    """The nearest-rank share (%) of a shape's bucketed times (measure.bucket), in seconds."""
    left = -(-sum(histogram.values()) * share // 100)
    for key in sorted(histogram, key=int):
        left -= histogram[key]
        if left <= 0:
            return round(1e-6 * measure.STEP ** int(key), 4)
    return None


def combine(found: list[dict]) -> list[dict]:
    """Tracer summaries added up by statement shape: calls, seconds, p95, maximum and rows."""
    shapes: dict[str, dict] = {}
    for summary in found:
        for row in summary["shapes"]:
            entry = shapes.setdefault(row["statement"], measure.blank(row["statement"]))
            for key in ("calls", "seconds", "rows"):
                entry[key] += row[key]
            entry["max_seconds"] = max(entry["max_seconds"], row["max_seconds"])
            for key, count in row["histogram"].items():
                entry["histogram"][key] = entry["histogram"].get(key, 0) + count
    rows = []
    for entry in sorted(shapes.values(), key=lambda e: (-e["seconds"], e["statement"])):
        p95 = quantile(entry.pop("histogram"), 95)
        entry |= {key: round(entry[key], 4) for key in ("seconds", "max_seconds")}
        p95 = None if p95 is None else min(p95, entry["max_seconds"])
        rows.append(entry | {"p95_seconds": p95})
    return rows


def final(state: dict, days: list[dict]) -> dict | None:
    """The last counted day: the sizes, each table's bytes and production's own growth."""
    counted = state["days_counted"]
    if not counted:
        return None
    day = max(counted, key=int)
    tables = counted[day].get("table_bytes", {})
    other = {name: n for name, n in sorted(counted[day]["other"].items()) if n}
    sizes = next(d for d in days if d["day"] == int(day) + 1)
    found = {"day": int(day) + 1, "of": state["days"]}
    found |= {"database_gb": sizes["database_gb"], "server_gb": sizes["server_gb"]}
    tables = {name: gb(size) for name, size in sorted(tables.items(), key=lambda t: -t[1])}
    return found | {"tables_gb": tables, "production_growth": other}


def build(out: Path) -> dict:
    state = json.loads((out / "state.json").read_text())
    path = out / "events.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    hours, before = [], state["baseline"].get("sizes", {}).get("database_bytes")
    for record in state["hours"]:
        if record["ingest"]["done"]:
            hours.append(hour_row(state, record, events, before))
            before = hours[-1]["database_bytes"] or before
    numbers = sorted({h["day"] for h in hours})
    days = [day_row(state, n, [h for h in hours if h["day"] == n]) for n in numbers]
    done = [h for h in hours if h["calculated"]]
    found = {"replay_id": state["replay_id"], "database": state["database"]}
    found |= {"first_day": state["first_day"], "days": state["days"]}
    found |= {"written_at": datetime.now(UTC).isoformat(timespec="seconds"), "hours": hours}
    found |= {"days_done": days, "final": final(state, days)}
    found["hardest"] = max(done, key=lambda h: h["rows_total"], default=None)
    found["lightest"] = min(done, key=lambda h: h["rows_total"], default=None)
    traced = [summary for record in state["hours"] for _, summary in summaries(record)]
    found["statements"] = combine(traced)
    return found | {"modelled": MODELLED, "lower_bound": {"tables": IP_TABLES, "why": LOWER_BOUND}}


def private(path, flags):
    return os.open(path, flags, 0o600)


def write_private(path: Path, text: str) -> None:
    """Replace a file with text, mode 600 from its creation."""
    with open(path.with_name(path.name + ".tmp"), "w", opener=private) as handle:
        handle.write(text)
    os.replace(handle.name, path)


def mb(value) -> str:
    return "" if value is None else f"{value / 2**20:.1f}"


def cell(value) -> str:
    return "" if value is None else str(value).replace("|", "\\|")


def table(header, rows) -> list[str]:
    return ["", *measure.table(header, [[cell(value) for value in row] for row in rows])]


def statement(found: dict | None) -> str:
    if not found:
        return ""
    return f"{found['seconds']:.2f} s {found['part']}: `{found['statement'][:80]}`"


def hour_cells(h: dict) -> list:
    per_test = "{p50}/{p95}/{max}".format(**h["per_test"]) if h["per_test"]["count"] else ""
    rss = mb((h["peak_rss_kb"] or 0) * 1024) if h["peak_rss_kb"] else ""
    return [h["day"], h["hour"], h["tests"], h["rows_total"], mb(h["slice_bytes"])] + [
        mb(h["growth_bytes"]), h["ingest_seconds"], per_test, h["calculate_seconds"], rss,
        h["retries"], h["delay_seconds"], statement(h["slowest"]),
    ]  # fmt: skip


def day_cells(d: dict) -> list:
    calculate = "{p95}/{max}".format(**d["calculate"]) if d["calculate"]["count"] else ""
    exact = "" if d["replay_rows"] is None else sum(d["replay_rows"].values())
    return [d["day"], d["hours"], d["tests"], exact, d["rows_total"], mb(d["slice_bytes"])] + [
        mb(d["growth_bytes"]), d["ingest_seconds"], d["calculate_seconds"], calculate,
        mb(d["peak_rss_kb"] * 1024), d["retries"], d["delay_seconds"], d["database_gb"],
        d["server_gb"],
    ]  # fmt: skip


HOURS = ("Day", "Hour", "Tests", "Rows inserted", "Slice MB", "Growth MB", "Ingest s")
HOURS += ("Per test p50/p95/max s", "Calculate s", "RSS MB", "Retries", "Delay s", "Slowest")
DAYS = ("Day", "Hours", "Tests", "Replay rows", "Rows inserted", "Slice MB", "Growth MB")
DAYS += ("Ingest s", "Calculate s")
DAYS += ("Calculate p95/max s", "RSS MB", "Retries", "Delay s", "Database GB", "Server GB")


def extremes(report: dict) -> list[str]:
    lines = ["", "## The hardest and the lightest hour (rows inserted)", ""]
    for name in ("hardest", "lightest"):
        h = report[name]
        lines.append(
            f"- {name.capitalize()}: day {h['day']} {h['hour']:02d}h, "
            f"{h['rows_total']} rows inserted, {h['tests']} tests, ingest "
            f"{h['ingest_seconds']} s, calculation {h['calculate_seconds']} s, "
            f"RSS {mb((h['peak_rss_kb'] or 0) * 1024)} MB."
            if h
            else f"- {name.capitalize()}: no hour calculated yet."
        )
    return lines


def last_day(found: dict | None) -> list[str]:
    if not found:
        return ["", "No simulated day has ended yet."]
    lines = ["", f"## Day {found['day']} of {found['of']}: sizes", ""]
    lines.append(f"Database {found['database_gb']} GB; the server {found['server_gb']} GB.")
    lines += table(("Table (with its partitions)", "GB"), found["tables_gb"].items())
    lines += ["", "## Production's own growth (rows not reachable from replay rows)"]
    growth = found["production_growth"].items()
    return lines + (table(("Table", "Rows"), growth) if growth else ["", "None."])


ROWS = (
    "Replay rows: each table's growth in rows reachable from the replay's runs and nodes, counted "
    "at the day's end (exact). Rows inserted: the rows the ingest's plain INSERT and COPY "
    "statements added, from their rowcount; upserts (ON CONFLICT DO UPDATE: product_ips, "
    "provider_overlap) and inserts inside WITH (pool_ips) are not counted per hour."
)


def days(report: dict) -> list[dict]:
    return [d for d in report["days_done"] if d["replay_rows"] is not None]


def markdown(report: dict) -> str:
    done = sum(h["calculated"] for h in report["hours"])
    lines = ["# Replay report (REPLAY-REPORT-1)", ""]
    lines.append(
        f"Replay `{report['replay_id']}` in `{report['database']}`: {report['days']} days from "
        f"{report['first_day']}, {done} hours calculated, written {report['written_at']}. "
        "Statements are timed by the tool from the client side."
    )
    lines += extremes(report)
    lines += ["", "## Per day", "", ROWS, *table(DAYS, map(day_cells, report["days_done"]))]
    found = sorted({name for d in report["days_done"] for name in d["replay_rows"] or {}})
    rows = [[d["day"], *((d["replay_rows"] or {}).get(t, "") for t in found)] for d in days(report)]
    lines += ["", "## Replay rows per day (exact)", *table(("Day", *found), rows)]
    lines += last_day(report["final"])
    lines += ["", "## Per hour", *table(HOURS, map(hour_cells, report["hours"]))]
    tables = sorted({name for h in report["hours"] for name in h["rows"]})
    rows = [[h["day"], h["hour"], *(h["rows"].get(t, 0) for t in tables)] for h in report["hours"]]
    lines += ["", "## Rows inserted per hour", "", ROWS, *table(("Day", "Hour", *tables), rows)]
    shapes = report["statements"][:STATEMENTS]
    lines += ["", f"## The {STATEMENTS} slowest statement shapes of the run"]
    keys = ("seconds", "calls", "p95_seconds", "max_seconds", "rows")
    found = [[*(s[key] for key in keys), f"`{s['statement']}`"] for s in shapes]
    lines += table(("Seconds", "Calls", "p95 s", "Max s", "Rows", "Statement"), found)
    lines += ["", "## Modelled, not real", "", *(f"- {text}" for text in report["modelled"])]
    lines += ["", "## IP tables: a lower bound", "", report["lower_bound"]["why"]]
    return "\n".join(lines) + "\n"


def write(out) -> Path:
    """report.json and report.md in the replay's out directory; returns the Markdown's path."""
    out = Path(out)
    report = build(out)
    write_private(out / "report.json", json.dumps(report, indent=2) + "\n")
    write_private(out / "report.md", markdown(report))
    return out / "report.md"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: replay_report.py <out dir>")
    print(write(sys.argv[1]))
