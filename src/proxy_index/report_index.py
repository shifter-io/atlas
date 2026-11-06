"""The index page of the HTML reports (report_html.py writes it as index.html).

`render_index(data, existing)` turns `report_data.index_data` into one page: the scheduler and
traffic cards, batches by test and status, the next census and pulse slots, unique IPs per
country and product next to the nodes, and the latest batches, linked to the pages in
`existing`. Pure: values in, HTML out, no database.
"""

from __future__ import annotations

from typing import Any

from .report_parts import (
    KIND_LABELS,
    REFRESH_SECONDS,
    badge,
    cards,
    duration,
    esc,
    mb,
    ms,
    num,
    page,
    pct,
    span,
    table,
    when,
)


def _index_head(data: dict[str, Any]) -> str:
    sched = data["scheduler"]
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
    head = (
        "<h1>Census and pulse batches</h1>"
        f'<p class="muted">Generated {esc(when(data["now"]))} · refreshes every '
        f"{REFRESH_SECONDS} s · {num(data['enabled_countries'])} countries enabled</p>"
    )
    return head + cards(
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


def _index_counts(data: dict[str, Any]) -> str:
    kinds = sorted({row["kind"] for row in data["counts"]})
    statuses = ["scheduled", "running", "done", "failed", "skipped", "killed"]
    grid = {(row["kind"], row["status"]): row["n"] for row in data["counts"]}
    traffic = {row["kind"]: row for row in data["by_kind"]}
    return "<h2>Batches by test and status</h2>" + table(
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


def _index_slots(data: dict[str, Any]) -> str:
    """The next census and pulse slots, side by side."""
    now = data["now"]
    census = table(
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
    pulse = table(
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
    return (
        '<div class="cols"><div><h3>Next census slots</h3>'
        + census
        + "</div><div><h3>Next pulse slots</h3>"
        + pulse
        + "</div></div>"
    )


def _index_fleet(data: dict[str, Any]) -> str:
    """Unique IPs per country and product next to the nodes."""
    unique = table(
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
    nodes = table(
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
    return (
        '<div class="cols"><div><h3>Unique IPs per country and product</h3>'
        + unique
        + "</div><div><h3>Nodes</h3>"
        + nodes
        + "</div></div>"
    )


def _index_batches(data: dict[str, Any], existing: set[int]) -> str:
    now = data["now"]
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
    return f"<h2>Batches ({shown}, newest first)</h2>" + table(
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


def render_index(data: dict[str, Any], existing: set[int]) -> str:
    body = (
        _index_head(data)
        + _index_counts(data)
        + _index_slots(data)
        + _index_fleet(data)
        + _index_batches(data, existing)
    )
    return page("Census and pulse batches", body, refresh=REFRESH_SECONDS)
