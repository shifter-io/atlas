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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from . import storage
from .report_data import _keyed as _keyed
from .report_data import _percentiles as _percentiles
from .report_data import batch_data as batch_data
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

# --- queries -----------------------------------------------------------------------------


def index_data(conn: psycopg.Connection, now: datetime, limit: int = INDEX_LIMIT) -> dict[str, Any]:
    from .orchestrator.batches import census_every, next_slot
    from .orchestrator.control import state

    counts = conn.execute(
        "SELECT kind,status,count(*)::bigint AS n FROM test_batches GROUP BY 1,2 ORDER BY 1,2"
    ).fetchall()
    by_kind = conn.execute(
        "SELECT b.kind,coalesce(sum(br.requests),0)::bigint AS requests,"
        "coalesce(sum(br.successes),0)::bigint AS successes,"
        "coalesce(sum(br.bytes_up),0)::bigint AS bytes_up,"
        "coalesce(sum(br.bytes_down),0)::bigint AS bytes_down "
        "FROM test_batches b JOIN batch_runs br USING(batch_id) GROUP BY 1 ORDER BY 1"
    ).fetchall()
    unique = conn.execute(
        "SELECT b.country_code,p.provider,p.name AS product,"
        "count(DISTINCT o.ip_id)::bigint AS ips FROM test_batches b "
        "JOIN batch_runs br USING(batch_id) JOIN products p USING(product_id) "
        "JOIN ip_observations o ON o.run_key=br.run_key GROUP BY 1,2,3 ORDER BY 1,2,3"
    ).fetchall()
    batches = conn.execute(
        "SELECT b.batch_id,b.kind,b.country_code,r.slug AS region,b.target_id,b.scheduled_for,"
        "b.started_at,b.finished_at,b.status,count(br.product_id)::int AS runs,"
        "coalesce(sum(br.requests),0)::bigint AS requests,"
        "coalesce(sum(br.successes),0)::bigint AS successes,"
        "coalesce(sum(br.unique_ips),0)::bigint AS unique_ips "
        "FROM test_batches b JOIN runner_regions r ON r.region_id=b.region_id "
        "LEFT JOIN batch_runs br USING(batch_id) GROUP BY b.batch_id,r.slug "
        "ORDER BY b.scheduled_for DESC,b.batch_id DESC LIMIT %s",
        (limit,),
    ).fetchall()
    if batches:
        p50 = _keyed(
            conn.execute(
                _percentiles(
                    "SELECT br.batch_id,b.histogram_steps AS steps,b.histogram_counts AS counts "
                    "FROM batch_runs br JOIN run_buckets b ON b.run_key=br.run_key "
                    "AND b.bucket_start>=%(since)s WHERE br.batch_id=ANY(%(ids)s)",
                    ["batch_id"],
                ),
                {
                    "ids": [b["batch_id"] for b in batches],
                    "since": min(b["scheduled_for"] for b in batches) - timedelta(days=1),
                },
            ).fetchall(),
            ["batch_id"],
        )
        for row in batches:
            row["p50"] = p50.get((row["batch_id"],), {}).get("p50")
    scheduler = state(conn)
    every = census_every(scheduler["census_every_minutes"])
    if every is not None:
        census_next = conn.execute(
            "SELECT c.country_code,r.slug AS region,%s::timestamptz AS next_at FROM countries c "
            "JOIN runner_regions r ON r.region_id=c.region_id AND r.enabled WHERE c.enabled "
            "ORDER BY c.country_code LIMIT 20",
            (next_slot(every, now),),
        ).fetchall()
    else:
        census_next = conn.execute(
            "SELECT c.country_code,r.slug AS region,CASE WHEN s.today>=%(now)s THEN s.today "
            "ELSE s.today+interval '1 day' END AS next_at FROM countries c "
            "JOIN runner_regions r ON r.region_id=c.region_id AND r.enabled "
            "CROSS JOIN LATERAL (SELECT ((%(now)s AT TIME ZONE 'UTC')::date+c.census_time) "
            " AT TIME ZONE 'UTC' AS today) s WHERE c.enabled ORDER BY 3,1 LIMIT 20",
            {"now": now},
        ).fetchall()
    pulse_next = [
        row | {"next_at": next_slot(timedelta(minutes=row["pulse_interval_minutes"]), now)}
        for row in conn.execute(
            "SELECT target_id,pulse_interval_minutes,pulse_requests FROM targets "
            "WHERE pulse_enabled ORDER BY sort_order,target_id"
        ).fetchall()
    ]
    live = conn.execute(
        "SELECT count(DISTINCT b.batch_id)::int AS batches,"
        "coalesce(sum(br.requests),0)::bigint AS requests,"
        "coalesce(sum(br.successes),0)::bigint AS successes,"
        "coalesce(sum(br.unique_ips),0)::bigint AS unique_ips,"
        "max(br.last_progress_at) AS updated "
        "FROM test_batches b JOIN batch_runs br USING(batch_id) WHERE b.status='running'"
    ).fetchone()
    live_unique = {
        (row["country_code"], row["provider"], row["product"]): row["unique_ips"]
        for row in conn.execute(
            "SELECT b.country_code,p.provider,p.name AS product,"
            "sum(br.unique_ips)::bigint AS unique_ips FROM test_batches b "
            "JOIN batch_runs br USING(batch_id) JOIN products p USING(product_id) "
            "WHERE b.status='running' GROUP BY 1,2,3"
        )
    }
    stored = {(row["country_code"], row["provider"], row["product"]) for row in unique}
    unique = sorted(
        [
            row | {"live": live_unique.get(key)}
            for row in unique
            for key in [(row["country_code"], row["provider"], row["product"])]
        ]
        + [
            {"country_code": c, "provider": p, "product": n, "ips": None, "live": ips}
            for (c, p, n), ips in live_unique.items()
            if (c, p, n) not in stored
        ],
        key=lambda row: (row["country_code"], row["provider"], row["product"]),
    )
    nodes = conn.execute(
        "SELECT n.node_id,r.slug AS region,n.provider,n.product,n.status,n.last_heartbeat "
        "FROM nodes n JOIN runner_regions r ON r.region_id=n.region_id ORDER BY 3,4,2"
    ).fetchall()
    enabled = conn.execute("SELECT count(*)::int AS n FROM countries WHERE enabled").fetchone()["n"]
    return {
        "now": now,
        "counts": counts,
        "by_kind": by_kind,
        "unique": unique,
        "batches": batches,
        "scheduler": scheduler,
        "census_next": census_next,
        "pulse_next": pulse_next,
        "nodes": nodes,
        "enabled_countries": enabled,
        "live": live,
        "limit": limit,
    }


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


def render_batch(batch: dict[str, Any], now: datetime) -> str:
    runs = batch["runs"]
    title = f"Batch {batch['batch_id']} · {KIND_LABELS.get(batch['kind'], batch['kind'])}"
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
    head += cards(
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
    summary = table(
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
    sections = []
    for r in runs:
        detail = (
            f'<section class="product"><h2>{esc(r["provider"])}/{esc(r["product"])} '
            f"{badge(r['status'])}</h2>"
            f"<p>{esc(stop_reason(r['stop_reason'], r['status']))}"
            + (f' <span class="muted">({esc(r["error"])})</span>' if r["error"] else "")
            + "</p>"
        )
        running = r["status"] in OPEN_RUNS
        version = str(r.get("node_version") or "0.0.0").split(".")
        modern_node = (
            len(version) >= 2
            and all(v.isdigit() for v in version[:2])
            and tuple(map(int, version[:2])) >= (0, 9)
        )
        completed_basis = str(r.get("census_rule_version")) == "2" or (running and modern_node)
        denominator = r["requests"] if completed_basis else r["successes"]
        basis = "completed requests" if completed_basis else "successes"
        if running and batch["kind"] == "census":
            rule = rule_progress(denominator, r["unique_ips"], batch["min_sample"])
            detail += (
                f"<p>{esc(rule_text(denominator, r['unique_ips'], batch['min_sample'], basis))}</p>"
                + meter(rule["sample"], "share of the minimum sample reached")
                + '<p class="muted">Minimum sample</p>'
            )
            if rule["share"] is not None:
                # Full bar at a unique share of 100%; the rule fires below the 50% midpoint.
                detail += meter(rule["share"], "current unique share") + (
                    '<p class="muted">Unique share (stops below 50%)</p>'
                )
        detail += cards(
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
        if r["timeline"]:
            detail += "<h3>Requests per 5 minutes</h3>" + timeline(r["timeline"])
        detail += '<div class="cols"><div><h3>Top cities</h3>' + table(
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
        detail += "</div><div><h3>Top ASNs</h3>" + table(
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
        detail += "</div></div></section>"
        sections.append(detail)
    events = table(
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
            for e in batch["events"]
        ),
    )
    body = (
        head
        + "<h2>Products</h2>"
        + summary
        + "".join(sections)
        + "<h2>Events</h2>"
        + events
        + f'<p class="muted">Generated {esc(when(now))}.</p>'
    )
    return page(title, body, refresh=REFRESH_SECONDS if live else None)


def render_index(data: dict[str, Any], existing: set[int]) -> str:
    now = data["now"]
    sched = data["scheduler"]
    kinds = sorted({row["kind"] for row in data["counts"]})
    statuses = ["scheduled", "running", "done", "failed", "skipped", "killed"]
    grid = {(row["kind"], row["status"]): row["n"] for row in data["counts"]}
    traffic = {row["kind"]: row for row in data["by_kind"]}
    total_requests = sum(row["requests"] for row in data["by_kind"])
    total_successes = sum(row["successes"] for row in data["by_kind"])
    switch = {True: "on", False: "off"}
    census_mode = (
        f"every {num(sched['census_every_minutes'])} min"
        if sched["census_every_minutes"]
        else "daily census_time"
    )
    body = (
        "<h1>Census and pulse batches</h1>"
        f'<p class="muted">Generated {esc(when(now))} · refreshes every '
        f"{REFRESH_SECONDS} s · {num(data['enabled_countries'])} countries enabled</p>"
    )
    body += cards(
        [
            ("Census scheduler", f"{switch[sched['census']]} · {esc(census_mode)}"),
            ("Pulse scheduler", switch[sched["pulse"]]),
            ("Batches", num(sum(grid.values()))),
            ("Requests (incl. live)", num(total_requests)),
            ("Success rate", pct(total_successes, total_requests)),
            (
                "Running now · live counters",
                f"{num(data['live']['batches'])} "
                f"{'batch' if data['live']['batches'] == 1 else 'batches'} · "
                f"{num(data['live']['requests'])} requests",
            ),
            ("Live counters updated", esc(when(data["live"]["updated"]))),
            *(
                (
                    f"Traffic · {KIND_LABELS.get(kind, kind)}",
                    mb(traffic[kind]["bytes_up"] + traffic[kind]["bytes_down"]),
                )
                for kind in sorted(traffic)
            ),
        ]
    )
    body += "<h2>Batches by test and status</h2>" + table(
        ["Test", *statuses, "Requests", "MB up", "MB down"],
        (
            [
                esc(KIND_LABELS.get(kind, kind)),
                *(num(grid.get((kind, s), 0)) for s in statuses),
                num(traffic.get(kind, {}).get("requests", 0)),
                mb(traffic.get(kind, {}).get("bytes_up", 0)),
                mb(traffic.get(kind, {}).get("bytes_down", 0)),
            ]
            for kind in kinds
        ),
        numeric=set(range(1, len(statuses) + 4)),
    )
    body += '<div class="cols"><div><h3>Next census slots</h3>' + table(
        ["Country", "Region", "Next slot", "In"],
        (
            [
                esc(row["country_code"].strip()),
                esc(row["region"]),
                esc(when(row["next_at"])),
                esc(duration((row["next_at"] - now).total_seconds())),
            ]
            for row in data["census_next"]
        ),
    )
    body += "</div><div><h3>Next pulse slots</h3>" + table(
        ["Target", "Every", "Requests", "Next slot", "In"],
        (
            [
                f"<code>{esc(row['target_id'])}</code>",
                esc(f"{row['pulse_interval_minutes']} min"),
                num(row["pulse_requests"]),
                esc(when(row["next_at"])),
                esc(duration((row["next_at"] - now).total_seconds())),
            ]
            for row in data["pulse_next"]
        ),
        numeric={2},
    )
    body += "</div></div>"
    body += '<div class="cols"><div><h3>Unique IPs per country and product</h3>' + table(
        ["Country", "Product", "Unique IPs (uploaded runs)", "Running now (live)"],
        (
            [
                esc(row["country_code"].strip()),
                esc(f"{row['provider']}/{row['product']}"),
                num(row["ips"]),
                num(row["live"]),
            ]
            for row in data["unique"]
        ),
        numeric={2, 3},
    )
    body += "</div><div><h3>Nodes</h3>" + table(
        ["Node", "Product", "Region", "Status", "Last heartbeat"],
        (
            [
                esc(f"#{row['node_id']}"),
                esc(f"{row['provider']}/{row['product']}"),
                esc(row["region"]),
                badge(row["status"]),
                esc(when(row["last_heartbeat"])),
            ]
            for row in data["nodes"]
        ),
    )
    body += "</div></div>"
    rows = []
    for b in data["batches"]:
        link = (
            f'<a href="batch-{b["batch_id"]}.html">{b["batch_id"]}</a>'
            if b["batch_id"] in existing
            else str(b["batch_id"])
        )
        rows.append(
            [
                link,
                esc(KIND_LABELS.get(b["kind"], b["kind"])),
                esc(b["country_code"].strip()),
                f"<code>{esc(b['target_id'])}</code>",
                esc(when(b["scheduled_for"])),
                badge(b["status"]),
                num(b["requests"]),
                num(b["unique_ips"]),
                pct(b["successes"], b["requests"]),
                ms(b.get("p50")),
                esc(duration(span(b["started_at"], b["finished_at"], now))),
            ]
        )
    shown = f"latest {num(data['limit'])}" if len(data["batches"]) >= data["limit"] else "all"
    body += f"<h2>Batches ({shown}, newest first)</h2>" + table(
        [
            "Batch",
            "Test",
            "Country",
            "Target",
            "Scheduled",
            "Status",
            "Requests",
            "Unique IPs",
            "Success rate",
            "p50",
            "Duration",
        ],
        rows,
        numeric={6, 7, 8, 9, 10},
    )
    return page("Census and pulse batches", body, refresh=REFRESH_SECONDS)


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
