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

import html
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
from .orchestrator.control import read_setting

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
    "unique_ratio": "Stopped on the unique-IP rule: after the minimum sample, "
    "unique IPs fell below "
    "50% of the run’s recorded ratio basis (all completed requests for census v2).",
    "no_success": "Stopped because nothing succeeded in the first requests or seconds of the "
    "run.",
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


def rule_text(completed: int, unique_ips: int, min_sample: int,
              basis: str = "completed requests") -> str:
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
            f"unique share {pct(unique_ips, completed)}, {gap:.1f} points above the 50% "
            "stop line"
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
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
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
    return '<div class="cards">' + "".join(
        f'<div class="card"><b>{value}</b><span>{esc(label)}</span></div>'
        for label, value in items
    ) + "</div>"


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


# --- queries -----------------------------------------------------------------------------


def _percentiles(source: str, keys: list[str]) -> str:
    """p50/p90/p99 per key over a source of (keys..., steps, counts) histogram rows, exactly
    as stats.Histogram ranks them."""
    k = ",".join(keys)
    return (
        f"WITH s AS ({source}), h AS (SELECT {k},x.ms,sum(x.n) AS n FROM s CROSS JOIN LATERAL "
        f"histogram(s.steps,s.counts::bigint[]) x GROUP BY {k},x.ms), c AS (SELECT {k},ms,"
        f"sum(n) OVER (PARTITION BY {k} ORDER BY ms) AS seen,sum(n) OVER (PARTITION BY {k}) "
        f"AS total FROM h) SELECT {k},min(ms) FILTER (WHERE seen>=ceil(total*0.5)) AS p50,"
        "min(ms) FILTER (WHERE seen>=ceil(total*0.9)) AS p90,"
        f"min(ms) FILTER (WHERE seen>=ceil(total*0.99)) AS p99 FROM c GROUP BY {k}"
    )


def _keyed(rows: list[dict], keys: list[str]) -> dict[tuple, dict]:
    return {tuple(row[k] for k in keys): row for row in rows}


def batch_data(conn: psycopg.Connection, batch_id: int) -> dict[str, Any] | None:
    batch = conn.execute(
        "SELECT b.batch_id,b.kind,b.country_code,c.name AS country,r.slug AS region,b.target_id,"
        "t.label AS target_label,t.url AS target_url,b.scheduled_for,b.start_at,b.started_at,"
        "b.finished_at,b.status FROM test_batches b JOIN runner_regions r "
        "ON r.region_id=b.region_id JOIN countries c ON c.country_code=b.country_code "
        "JOIN targets t ON t.target_id=b.target_id WHERE b.batch_id=%s",
        (batch_id,),
    ).fetchone()
    if batch is None:
        return None
    params = {"batch": batch_id, "since": batch["scheduled_for"] - timedelta(days=1)}
    runs = conn.execute(
        "SELECT br.product_id,p.provider,p.name AS product,br.node_id,n.host,br.status,"
        "br.stop_reason,br.error,br.started_at,br.finished_at,br.requests,br.successes,"
        "br.unique_ips,br.challenges,br.blocks,br.timeouts,br.proxy_errors,br.bytes_up,"
        "br.bytes_down,br.run_key,br.last_progress_at,n.version AS node_version,"
        "tr.summary->'config_overrides'->>'census_rule_version' AS census_rule_version "
        "FROM batch_runs br "
        "JOIN products p USING(product_id) "
        "LEFT JOIN nodes n ON n.node_id=br.node_id "
        "LEFT JOIN test_runs tr ON tr.run_key=br.run_key WHERE br.batch_id=%(batch)s "
        "ORDER BY p.provider,p.name",
        params,
    ).fetchall()
    latency = _keyed(
        conn.execute(
            _percentiles(
                "SELECT br.product_id,b.histogram_steps AS steps,b.histogram_counts AS counts "
                "FROM batch_runs br JOIN run_buckets b ON b.run_key=br.run_key "
                "AND b.bucket_start>=%(since)s WHERE br.batch_id=%(batch)s",
                ["product_id"],
            ),
            params,
        ).fetchall(),
        ["product_id"],
    )
    new_ips = _keyed(
        conn.execute(
            "SELECT br.product_id,count(*)::bigint AS new_ips FROM batch_runs br "
            "JOIN test_runs tr ON tr.run_key=br.run_key "
            "CROSS JOIN LATERAL (SELECT DISTINCT o.ip_id FROM ip_observations o "
            " WHERE o.run_key=br.run_key AND o.bucket_start>=%(since)s) o "
            "JOIN product_ips pi ON pi.product_id=br.product_id AND pi.ip_id=o.ip_id "
            "WHERE br.batch_id=%(batch)s AND pi.first_seen>=tr.started_at GROUP BY 1",
            params,
        ).fetchall(),
        ["product_id"],
    )
    timelines: dict[int, list[dict]] = {}
    for row in conn.execute(
        "SELECT br.product_id,b.bucket_start,sum(b.completed)::bigint AS requests,"
        "sum(b.successes)::bigint AS successes FROM batch_runs br JOIN run_buckets b "
        "ON b.run_key=br.run_key AND b.bucket_start>=%(since)s WHERE br.batch_id=%(batch)s "
        "GROUP BY 1,2 ORDER BY 1,2",
        params,
    ):
        timelines.setdefault(row["product_id"], []).append(row)

    geo = (
        "FROM batch_runs br JOIN run_geo_buckets g ON g.run_key=br.run_key "
        "AND g.bucket_start>=%(since)s "
        "JOIN enrichment_locations l ON l.location_id=g.location_id "
        "JOIN enrichment_networks n ON n.network_id=g.network_id WHERE br.batch_id=%(batch)s"
    )
    city_keys = ["product_id", "region", "city"]
    cities = conn.execute(
        "SELECT br.product_id,l.region,l.city,sum(g.completed)::bigint AS requests,"
        f"sum(g.successes)::bigint AS successes {geo} GROUP BY 1,2,3 "
        "ORDER BY 4 DESC,2,3",
        params,
    ).fetchall()
    city_latency = _keyed(
        conn.execute(
            _percentiles(
                "SELECT br.product_id,l.region,l.city,g.histogram_steps AS steps,"
                f"g.histogram_counts AS counts {geo}",
                city_keys,
            ),
            params,
        ).fetchall(),
        city_keys,
    )
    asn_keys = ["product_id", "asn"]
    asns = conn.execute(
        "SELECT br.product_id,n.asn,max(coalesce(n.asn_organization,n.organization)) AS org,"
        "string_agg(DISTINCT n.usage_type,', ') AS usage_type,"
        "sum(g.completed)::bigint AS requests,sum(g.successes)::bigint AS successes "
        f"{geo} GROUP BY 1,2 ORDER BY 5 DESC,2",
        params,
    ).fetchall()
    asn_latency = _keyed(
        conn.execute(
            _percentiles(
                "SELECT br.product_id,n.asn,g.histogram_steps AS steps,"
                f"g.histogram_counts AS counts {geo}",
                asn_keys,
            ),
            params,
        ).fetchall(),
        asn_keys,
    )
    asn_ips = _keyed(
        conn.execute(
            "SELECT br.product_id,n.asn,count(DISTINCT o.ip_id)::bigint AS ips "
            "FROM batch_runs br JOIN ip_observations o ON o.run_key=br.run_key "
            "AND o.bucket_start>=%(since)s "
            "JOIN LATERAL (SELECT max(release_id) AS release_id FROM run_enrichments re "
            " WHERE re.run_key=br.run_key) re ON true "
            "JOIN ip_enrichment e ON e.ip_id=o.ip_id AND e.release_id=re.release_id "
            "JOIN enrichment_networks n ON n.network_id=e.network_id "
            "WHERE br.batch_id=%(batch)s GROUP BY 1,2",
            params,
        ).fetchall(),
        asn_keys,
    )
    for run in runs:
        pid = run["product_id"]
        run.update(latency.get((pid,), {"p50": None, "p90": None, "p99": None}))
        run["new_ips"] = new_ips[(pid,)]["new_ips"] if (pid,) in new_ips else (
            0 if run["run_key"] else None
        )
        run["timeline"] = timelines.get(pid, [])
        run["cities"] = [
            c | {"p50": city_latency.get(tuple(c[k] for k in city_keys), {}).get("p50")}
            for c in cities
            if c["product_id"] == pid
        ][:TOP]
        run["asns"] = [
            a
            | {
                "p50": asn_latency.get((pid, a["asn"]), {}).get("p50"),
                "ips": asn_ips.get((pid, a["asn"]), {}).get("ips"),
            }
            for a in asns
            if a["product_id"] == pid
        ][:TOP]
    batch["runs"] = runs
    batch["min_sample"] = int(read_setting(conn, "census.min_sample"))
    batch["events"] = conn.execute(
        "SELECT e.at,e.kind,p.provider,p.name AS product,e.signal,e.severity,e.data "
        "FROM test_events e LEFT JOIN products p USING(product_id) "
        "WHERE e.batch_id=%(batch)s AND e.at>=%(since)s ORDER BY e.at,e.event_id",
        params,
    ).fetchall()
    return batch


def index_data(
    conn: psycopg.Connection, now: datetime, limit: int = INDEX_LIMIT
) -> dict[str, Any]:
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
        row
        | {"next_at": next_slot(timedelta(minutes=row["pulse_interval_minutes"]), now)}
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
        [row | {"live": live_unique.get(key)} for row in unique
         for key in [(row["country_code"], row["provider"], row["product"])]]
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
    enabled = conn.execute(
        "SELECT count(*)::int AS n FROM countries WHERE enabled"
    ).fetchone()["n"]
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
            "Product", "Status", "Requests", "Successes", "Success rate", "Unique IPs",
            "Unique share", "New IPs", "Challenges", "Hard blocks", "Timeouts", "Proxy errors",
            "p50", "p90", "p99", "MB up", "MB down",
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
        modern_node = (len(version) >= 2 and all(v.isdigit() for v in version[:2])
                       and tuple(map(int, version[:2])) >= (0, 9))
        completed_basis = (str(r.get("census_rule_version")) == "2"
                           or (running and modern_node))
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
                *(
                    [("Last progress", esc(when(r["last_progress_at"])))]
                    if running
                    else []
                ),
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
            "Batch", "Test", "Country", "Target", "Scheduled", "Status", "Requests",
            "Unique IPs", "Success rate", "p50", "Duration",
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
