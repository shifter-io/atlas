"""The report pages' constants, formatters, rule text and page components.

Everything here is pure: values in, text or HTML out, no database. report_html.py imports
these names back, so `report_html.<name>` keeps working for its callers and tests.
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

INDEX = "index.html"
INDEX_LIMIT = 500
TOP = 15
REFRESH_SECONDS = 30
OPEN = ("scheduled", "running")
OPEN_RUNS = ("queued", "running", "uploading")
# Census v2 counts all completions; old saved runs keep their original basis.
STOP_SHARE = 0.5
KIND_LABELS = {
    "census": "Test #1 · Census",
    "pulse": "Test #2 · Pulse",
    "load": "Test #2 · Load (latency under load)",
}
STOP_REASONS = {
    "unique_ratio": "Stopped on the unique-IP rule: after the minimum sample, unique IPs fell "
    "below 50% of the run’s recorded ratio basis (all completed requests for census v2).",
    "no_success": "Stopped because nothing succeeded in the first requests or seconds of the run.",
    "stalled": "Stopped as stalled: it had successes, then none for the stall window (recorded "
    "as an outage).",
    "rate_limited": "Stopped because the provider kept rate limiting the run with no success for "
    "the rate-limit window (RATE_LIMIT_STOP_SECONDS, 120 s).",
    "requests": "Completed its fixed number of requests.",
    "operator": "Stopped by an operator (kill or skip).",
    "error": "Ended with an error: the node reported a failure or the run went silent.",
    "no_node": "Did not run: the product has no node in this region.",
}
RUN_STATUS = {
    "queued": "Waiting for its start time.",
    "running": "Running now.",
    "uploading": "Uploading its results.",
    "skipped": "Skipped.",
    "killed": "Killed by an operator.",
}


# --- formatting --------------------------------------------------------------------------


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def num(value: Any) -> str:
    if value is None:
        return "–"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.2f}"
    return f"{int(value):,}"


def pct(part: Any, whole: Any) -> str:
    return f"{part / whole * 100:,.1f}%" if whole else "–"


def mb(value: Any) -> str:
    return "–" if value is None else f"{value / 1_000_000:,.2f} MB"


def ms(value: Any) -> str:
    return "–" if value is None else f"{int(value):,} ms"


def when(value: datetime | None) -> str:
    return "–" if value is None else value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    seconds = int(max(seconds, 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours:,}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def span(start: datetime | None, end: datetime | None, now: datetime) -> float | None:
    if start is None:
        return None
    return ((end or now) - start).total_seconds()


def rule_progress(completed: int, unique_ips: int, min_sample: int) -> dict[str, Any]:
    """How close a running census is to the unique-ratio stop rule."""
    share = unique_ips / completed if completed else None
    sample = min(completed / min_sample, 1.0) if min_sample else 1.0
    return {
        "sample": sample,
        "share": share,
        "met": completed >= min_sample and share is not None and share < STOP_SHARE,
    }


def rule_text(
    completed: int, unique_ips: int, min_sample: int, basis: str = "completed requests"
) -> str:
    rule = rule_progress(completed, unique_ips, min_sample)
    if rule["met"]:
        return (
            f"Stop rule met: {num(completed)} {basis} ≥ {num(min_sample)} and unique share "
            f"{pct(unique_ips, completed)} < 50%; the node stops at its next check."
        )
    sample = (
        f"minimum sample reached ({num(completed)} of {num(min_sample)} {basis})"
        if completed >= min_sample
        else f"{num(completed)} of {num(min_sample)} {basis} for the minimum sample "
        f"({rule['sample'] * 100:.1f}%)"
    )
    if rule["share"] is None:
        share = f"no {basis} yet, so no unique share"
    else:
        gap = (rule["share"] - STOP_SHARE) * 100
        share = (
            f"unique share {pct(unique_ips, completed)}, {gap:.1f} points above the 50% stop line"
            if gap >= 0
            else f"unique share {pct(unique_ips, completed)}, already below the 50% stop line"
        )
    return f"Stop rule not met yet: {sample}; {share}."


def meter(fraction: float, label: str) -> str:
    fraction = min(max(fraction, 0.0), 1.0)
    return (
        f'<div class="meter" role="img" aria-label="{esc(label)}" title="{esc(label)}">'
        f'<span style="width:{fraction * 100:.1f}%"></span></div>'
    )


def stop_reason(reason: str | None, status: str) -> str:
    if reason:
        return STOP_REASONS.get(reason, f"Stopped: {reason}.")
    if status == "done":
        return "Finished."
    if status == "failed":
        return "Failed before recording a stop reason."
    return RUN_STATUS.get(status, "")


CSS = """
:root{color-scheme:light dark;--bg:#fcfcfb;--panel:#f3f2ef;--ink:#0b0b0b;--muted:#52514e;
--line:#dddcd6;--accent:#2a78d6;--good:#008300;--bad:#c43b3a;--warn:#a86f00}
@media (prefers-color-scheme:dark){:root{--bg:#1a1a19;--panel:#242422;--ink:#f4f4f2;
--muted:#c3c2b7;--line:#3a3a37;--accent:#3987e5;--good:#4cae4c;--bad:#e66767;--warn:#d9a43a}}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--ink);
font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1280px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}
h3{font-size:15px;margin:18px 0 6px}
a{color:var(--accent)}
.muted{color:var(--muted)}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 14px;
min-width:150px}
.card b{display:block;font-size:18px;font-variant-numeric:tabular-nums}
.card span{color:var(--muted);font-size:12px}
table{border-collapse:collapse;width:100%;margin:6px 0 14px;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;white-space:nowrap}
td.n,th.n{text-align:right;white-space:nowrap}
.wrap{overflow-x:auto}
.status{display:inline-block;padding:1px 7px;border-radius:9px;font-size:12px;
border:1px solid var(--line)}
.s-done{color:var(--good);border-color:var(--good)}
.s-failed,.s-killed{color:var(--bad);border-color:var(--bad)}
.s-skipped{color:var(--warn);border-color:var(--warn)}
.s-running,.s-scheduled,.s-queued,.s-uploading{color:var(--accent);border-color:var(--accent)}
.product{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:4px 16px
 12px;margin:14px 0}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:18px}
.live{border-left:4px solid var(--accent);background:var(--panel);padding:6px 12px;
border-radius:4px}
.meter{height:8px;max-width:420px;background:var(--line);border-radius:4px;overflow:hidden;
margin:4px 0 2px}.meter span{display:block;height:100%;background:var(--accent)}
svg .bar{fill:var(--accent)}svg .axis{stroke:var(--line)}svg text{fill:var(--muted);font-size:10px}
code{font-size:12px}
"""


def page(title: str, body: str, *, refresh: int | None = None) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta name="robots" content="noindex">{meta}<title>{esc(title)}</title>'
        f"<style>{CSS}</style></head><body><main>{body}</main></body></html>\n"
    )


def badge(status: str | None) -> str:
    return f'<span class="status s-{esc(status)}">{esc(status)}</span>'


def table(headers: list[str], rows: Iterable[list[str]], numeric: set[int] | None = None) -> str:
    numeric = numeric or set()
    head = "".join(
        f'<th class="n">{esc(h)}</th>' if i in numeric else f"<th>{esc(h)}</th>"
        for i, h in enumerate(headers)
    )
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="n">{cell}</td>' if i in numeric else f"<td>{cell}</td>"
            for i, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    if not body:
        body = f'<tr><td colspan="{len(headers)}" class="muted">Nothing recorded.</td></tr>'
    return (
        f'<div class="wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def cards(items: list[tuple[str, str]]) -> str:
    return (
        '<div class="cards">'
        + "".join(
            f'<div class="card"><b>{value}</b><span>{esc(label)}</span></div>'
            for label, value in items
        )
        + "</div>"
    )


def timeline(buckets: list[dict[str, Any]]) -> str:
    """Requests per 5-minute bucket as an inline SVG bar chart; hover a bar for its numbers."""
    if not buckets:
        return ""
    width, height, pad = 640, 120, 18
    top = max(b["requests"] for b in buckets) or 1
    slot = (width - 2) / len(buckets)
    bar = max(min(slot - 2, 40), 1)
    marks = []
    for i, b in enumerate(buckets):
        h = (height - pad - 4) * b["requests"] / top
        x = 1 + i * slot + (slot - bar) / 2
        tip = (
            f"{b['bucket_start'].astimezone(UTC):%H:%M} UTC: {b['requests']:,} requests, "
            f"{b['successes']:,} successes ({pct(b['successes'], b['requests'])})"
        )
        marks.append(
            f'<rect class="bar" x="{x:.1f}" y="{height - pad - h:.1f}" width="{bar:.1f}" '
            f'height="{h:.1f}" rx="2"><title>{esc(tip)}</title></rect>'
        )
    first = buckets[0]["bucket_start"].astimezone(UTC)
    last = buckets[-1]["bucket_start"].astimezone(UTC)
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px" '
        'role="img" aria-label="Requests per 5-minute bucket">'
        f'<line class="axis" x1="0" x2="{width}" y1="{height - pad}" y2="{height - pad}"/>'
        + "".join(marks)
        + f'<text x="0" y="{height - 4}">{first:%H:%M}</text>'
        f'<text x="{width}" y="{height - 4}" text-anchor="end">{last:%H:%M} UTC · peak '
        f"{top:,} requests</text></svg>"
    )
