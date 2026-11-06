"""Self-contained HTML reports over census and pulse batches (proxy-index report html).

`write(credentials_file, out_dir, batch_ids)` writes `batch-<id>.html` for each batch and
regenerates `index.html`. Every page is one file with inline CSS (light and dark), inline SVG
bars and no scripts or external assets; the index refreshes itself every 30 seconds. The
orchestrator calls this from a thread whenever a batch closes and the `reports.dir` setting is
set (orchestrator/jobs.py). Everything is read in one read-only transaction.

A batch page shows, per product: status, stop reason in words, requests, successes, success
rate, unique IPs, unique share (unique IPs / successes), new IPs (addresses the product had
never been seen on before this run), challenges, hard blocks, timeouts, proxy errors, latency
p50/p90/p99 (from the run_buckets histograms), wire MB, a 5-minute timeline, and the top cities
and ASNs from run_geo_buckets; then the batch's tape events.

A batch still open is reported from the live batch_runs counters the nodes post every 30 s
(the histograms, IPs and geography only exist once a run is uploaded): the page says so, shows
the elapsed time and, for a census, how close each run is to the unique-ratio stop rule. The
orchestrator's `reports` job rewrites the index and every running batch's page every 30 s
(`write(..., live=True)`); the page of a batch that closes is rewritten from its final data.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row

from . import storage
from .report_data import batch_data as batch_data
from .report_data import index_data as index_data
from .report_index import render_index as render_index
from .report_parts import CSS as CSS
from .report_parts import INDEX as INDEX
from .report_parts import INDEX_LIMIT as INDEX_LIMIT
from .report_parts import KIND_LABELS as KIND_LABELS
from .report_parts import OPEN as OPEN
from .report_parts import OPEN_RUNS as OPEN_RUNS
from .report_parts import REFRESH_SECONDS as REFRESH_SECONDS
from .report_parts import RUN_STATUS as RUN_STATUS
from .report_parts import STOP_REASONS as STOP_REASONS
from .report_parts import STOP_SHARE as STOP_SHARE
from .report_parts import TOP as TOP
from .report_parts import badge as badge
from .report_parts import cards as cards
from .report_parts import duration as duration
from .report_parts import esc as esc
from .report_parts import mb as mb
from .report_parts import meter as meter
from .report_parts import ms as ms
from .report_parts import num as num
from .report_parts import page as page
from .report_parts import pct as pct
from .report_parts import rule_progress as rule_progress
from .report_parts import rule_text as rule_text
from .report_parts import span as span
from .report_parts import stop_reason as stop_reason
from .report_parts import table as table
from .report_parts import timeline as timeline
from .report_parts import when as when

# --- rendering ---------------------------------------------------------------------------


def _event_data(data: dict[str, Any]) -> str:
    parts = []
    for key, value in (data or {}).items():
        if isinstance(value, int | float) and not isinstance(value, bool):
            value = num(value)
        elif not isinstance(value, str):
            value = json.dumps(value)
        parts.append(f"{esc(key)}=<code>{esc(value)}</code>")
    return " ".join(parts)


def _batch_head(batch: dict[str, Any], title: str, now: datetime) -> str:
    runs = batch["runs"]
    total_requests = sum(r["requests"] for r in runs)
    total_successes = sum(r["successes"] for r in runs)
    live = batch["status"] in OPEN
    updated = max((r["last_progress_at"] for r in runs if r["last_progress_at"]), default=None)
    head = (
        f'<p><a href="{INDEX}">← All batches</a></p>'
        f"<h1>{esc(title)} {badge(batch['status'])}</h1>"
        f'<p class="muted">{esc(batch["country"])} ({esc(batch["country_code"].strip())}) from '
        f"{esc(batch['region'])} · target <code>{esc(batch['target_id'])}</code> "
        f"{esc(batch['target_url'])}</p>"
    )
    if live:
        head += (
            f'<p class="live"><b>{esc(batch["status"])} — live counters, updated '
            f"{esc(when(updated) if updated else 'never (no progress yet)')}</b>. Latency, new "
            "IPs, cities and ASNs appear once each run has uploaded its results.</p>"
        )
    return head + cards(
        [
            ("Scheduled", esc(when(batch["scheduled_for"]))),
            ("Start at", esc(when(batch["start_at"]))),
            ("Started", esc(when(batch["started_at"]))),
            ("Finished", esc(when(batch["finished_at"]))),
            (
                "Elapsed" if live else "Duration",
                esc(duration(span(batch["started_at"], batch["finished_at"], now))),
            ),
            ("Requests", num(total_requests)),
            ("Success rate", pct(total_successes, total_requests)),
        ]
    )


def _batch_summary(runs: list[dict[str, Any]]) -> str:
    return table(
        [
            "Product",
            "Status",
            "Requests",
            "Successes",
            "Success rate",
            "Unique IPs",
            "Unique share",
            "New IPs",
            "Challenges",
            "Hard blocks",
            "Timeouts",
            "Proxy errors",
            "p50",
            "p90",
            "p99",
            "MB up",
            "MB down",
        ],
        (
            [
                f"{esc(r['provider'])}/{esc(r['product'])}",
                badge(r["status"]),
                num(r["requests"]),
                num(r["successes"]),
                pct(r["successes"], r["requests"]),
                num(r["unique_ips"]),
                pct(r["unique_ips"], r["successes"]),
                num(r["new_ips"]),
                num(r["challenges"]),
                num(r["blocks"]),
                num(r["timeouts"]),
                num(r["proxy_errors"]),
                ms(r["p50"]),
                ms(r["p90"]),
                ms(r["p99"]),
                mb(r["bytes_up"]),
                mb(r["bytes_down"]),
            ]
            for r in runs
        ),
        numeric=set(range(2, 17)),
    )


def _run_rule(batch: dict[str, Any], r: dict[str, Any]) -> str:
    """A running census run's progress towards the unique-ratio stop rule ("" otherwise)."""
    running = r["status"] in OPEN_RUNS
    if not (running and batch["kind"] == "census"):
        return ""
    version = str(r.get("node_version") or "0.0.0").split(".")
    modern_node = (
        len(version) >= 2
        and all(v.isdigit() for v in version[:2])
        and tuple(map(int, version[:2])) >= (0, 9)
    )
    completed_basis = str(r.get("census_rule_version")) == "2" or modern_node
    denominator = r["requests"] if completed_basis else r["successes"]
    basis = "completed requests" if completed_basis else "successes"
    rule = rule_progress(denominator, r["unique_ips"], batch["min_sample"])
    detail = (
        f"<p>{esc(rule_text(denominator, r['unique_ips'], batch['min_sample'], basis))}</p>"
        + meter(rule["sample"], "share of the minimum sample reached")
        + '<p class="muted">Minimum sample</p>'
    )
    if rule["share"] is not None:
        # Full bar at a unique share of 100%; the rule fires below the 50% midpoint.
        detail += meter(rule["share"], "current unique share") + (
            '<p class="muted">Unique share (stops below 50%)</p>'
        )
    return detail


def _run_cards(r: dict[str, Any], now: datetime) -> str:
    running = r["status"] in OPEN_RUNS
    return cards(
        [
            (
                "Elapsed" if running else "Run time",
                esc(duration(span(r["started_at"], r["finished_at"], now))),
            ),
            *([("Last progress", esc(when(r["last_progress_at"])))] if running else []),
            ("Unique share", pct(r["unique_ips"], r["successes"])),
            ("Node", esc(f"#{r['node_id']} {r['host'] or ''}" if r["node_id"] else "–")),
            ("Unique IPs", num(r["unique_ips"])),
            ("New IPs", num(r["new_ips"])),
            (
                "Latency p50 / p90 / p99",
                f"{ms(r['p50'])} / {ms(r['p90'])} / {ms(r['p99'])}",
            ),
            ("Traffic", mb((r["bytes_up"] or 0) + (r["bytes_down"] or 0))),
        ]
    )


def _run_geo(r: dict[str, Any]) -> str:
    """The run's top cities and top ASNs, side by side."""
    cities = table(
        ["Region / state", "City", "Requests", "Success rate", "p50"],
        (
            [
                esc(c["region"] or "–"),
                esc(c["city"] or ("unknown" if c["region"] is None else "–")),
                num(c["requests"]),
                pct(c["successes"], c["requests"]),
                ms(c["p50"]),
            ]
            for c in r["cities"]
        ),
        numeric={2, 3, 4},
    )
    asns = table(
        ["ASN", "Organization", "Usage", "Requests", "IPs", "Success rate", "p50"],
        (
            [
                esc(f"AS{a['asn']}" if a["asn"] is not None else "–"),
                esc(a["org"] or ("unknown" if a["asn"] is None else "–")),
                esc(a["usage_type"] or "–"),
                num(a["requests"]),
                num(a["ips"]),
                pct(a["successes"], a["requests"]),
                ms(a["p50"]),
            ]
            for a in r["asns"]
        ),
        numeric={3, 4, 5, 6},
    )
    return (
        '<div class="cols"><div><h3>Top cities</h3>'
        + cities
        + "</div><div><h3>Top ASNs</h3>"
        + asns
        + "</div></div>"
    )


def _run_section(batch: dict[str, Any], r: dict[str, Any], now: datetime) -> str:
    detail = (
        f'<section class="product"><h2>{esc(r["provider"])}/{esc(r["product"])} '
        f"{badge(r['status'])}</h2>"
        f"<p>{esc(stop_reason(r['stop_reason'], r['status']))}"
        + (f' <span class="muted">({esc(r["error"])})</span>' if r["error"] else "")
        + "</p>"
    )
    detail += _run_rule(batch, r) + _run_cards(r, now)
    if r["timeline"]:
        detail += "<h3>Requests per 5 minutes</h3>" + timeline(r["timeline"])
    return detail + _run_geo(r) + "</section>"


def _batch_events(events: list[dict[str, Any]]) -> str:
    return table(
        ["At", "Event", "Product", "Signal", "Severity", "Data"],
        (
            [
                esc(when(e["at"])),
                esc(e["kind"]),
                esc(f"{e['provider']}/{e['product']}" if e["provider"] else "–"),
                esc(f"{e['signal']:+d}" if e["signal"] else "0"),
                esc(e["severity"]),
                _event_data(e["data"]),
            ]
            for e in events
        ),
    )


def render_batch(batch: dict[str, Any], now: datetime) -> str:
    runs = batch["runs"]
    title = f"Batch {batch['batch_id']} · {KIND_LABELS.get(batch['kind'], batch['kind'])}"
    live = batch["status"] in OPEN
    body = (
        _batch_head(batch, title, now)
        + "<h2>Products</h2>"
        + _batch_summary(runs)
        + "".join(_run_section(batch, r, now) for r in runs)
        + "<h2>Events</h2>"
        + _batch_events(batch["events"])
        + f'<p class="muted">Generated {esc(when(now))}.</p>'
    )
    return page(title, body, refresh=REFRESH_SECONDS if live else None)


# --- files -------------------------------------------------------------------------------


def _write_atomic(path: Path, text: str) -> None:
    """Replace the file in one rename, so a refreshing browser never reads half a page."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write(
    credentials_file: str | Path,
    out_dir: str | Path,
    batch_ids: Iterable[int] | None = None,
    *,
    live: bool = False,
    limit: int = INDEX_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write batch-<id>.html for `batch_ids` (None: every batch the index lists; with `live`,
    every running batch plus `batch_ids`) and regenerate index.html. Returns the files
    written."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    written = []
    with storage.connect(credentials_file) as conn:
        conn.row_factory = dict_row
        conn.execute("SET TRANSACTION READ ONLY")
        index = index_data(conn, now, limit)
        if live:
            running = conn.execute(
                "SELECT batch_id FROM test_batches WHERE status='running'"
            ).fetchall()
            ids = sorted(set(batch_ids or ()) | {row["batch_id"] for row in running})
        elif batch_ids is None:
            ids = [row["batch_id"] for row in index["batches"]]
        else:
            ids = sorted(set(batch_ids))
        for batch_id in ids:
            batch = batch_data(conn, batch_id)
            if batch is None:
                continue
            path = out / f"batch-{batch_id}.html"
            _write_atomic(path, render_batch(batch, now))
            written.append(path.name)
    existing = {
        int(name[6:-5])
        for name in os.listdir(out)
        if name.startswith("batch-") and name.endswith(".html") and name[6:-5].isdigit()
    }
    _write_atomic(out / INDEX, render_index(index, existing))
    return {"dir": str(out.resolve()), "batches": written, "index": INDEX}
