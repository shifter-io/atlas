"""The offline job (jobs `offline`, every 15 s): nodes whose heartbeat stopped go offline.

Node states (nodes.status):
- `online`: it sent a heartbeat (registration or `/v1/heartbeat`).
- `offline`: no heartbeat for `node.offline_seconds`; set here, with an alert and a tape event.
- `retired`: removed on purpose. The on-demand pulse hour retires its nodes before it removes
  their droplets (pulse_hour.retire_nodes), and this job retires the stale nodes of a pulse hour
  that is closed (every pulse batch of the hour ended) without an alert or event. A heartbeat
  keeps a retired node retired; a new token (issue_token, the next hour's arming) makes it
  `offline` until its first heartbeat.

With `pulse.on_demand`, a node whose latest run is a pulse lost while its hour is still open is
one of that hour's losses: the nodes lost in an hour share one major alert,
`pulse_nodes_lost:<YYYYMMDDTHH>` ("pulse hour <H>: N of M nodes lost before the end: <list>"),
updated in place as more go, so Slack posts it once. Every other node keeps its own
`node_offline:<id>` alert, which its next heartbeat acknowledges.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from .db import Database, setting
from .events import EventRow, emit, raise_alert
from .sqlrows import _now, _rows

HOUR = timedelta(hours=1)
STALE = (
    "n.status NOT IN ('offline','retired') "
    "AND (n.last_heartbeat IS NULL OR n.last_heartbeat<%s)"
)
# The batch of the node's latest run: its kind and hour (scheduled_for).
LATEST = (
    "LEFT JOIN LATERAL (SELECT b.scheduled_for AS latest,b.kind AS latest_kind "
    "FROM batch_runs br JOIN test_batches b USING(batch_id) WHERE br.node_id=n.node_id "
    "ORDER BY b.scheduled_for DESC LIMIT 1) l ON true"
)
HOUR_OPEN = (
    "SELECT 1 FROM test_batches WHERE kind='pulse' AND scheduled_for=%s "
    "AND status IN ('scheduled','running') LIMIT 1"
)
HOUR_NODES = (
    "SELECT DISTINCT br.node_id FROM batch_runs br JOIN test_batches b USING(batch_id) "
    "WHERE b.kind='pulse' AND b.scheduled_for=%s"
)


def _hour_of(node: dict, open_hours: set) -> datetime | None:
    """The open pulse hour a stale on-demand node belongs to; None for a closed hour."""
    if node["latest_kind"] == "pulse" and node["latest"] in open_hours:
        return node["latest"]
    return None


async def _classify(
    conn: psycopg.AsyncConnection, stale: list[dict]
) -> tuple[list[dict], dict[datetime, list[dict]], list[dict]]:
    """Split on-demand stale nodes into retired (hour closed), lost by hour and the rest."""
    retired, lost, other = [], defaultdict(list), []
    hours = {n["latest"] for n in stale if n["latest_kind"] == "pulse"}
    open_hours = set()
    for hour in hours:
        if await (await conn.execute(HOUR_OPEN, (hour,))).fetchone():
            open_hours.add(hour)
    for node in stale:
        hour = _hour_of(node, open_hours)
        if hour is not None:
            lost[hour].append(node)
        elif node["latest_kind"] == "pulse" and (
            node["last_heartbeat"] is None or node["last_heartbeat"] < node["latest"] + HOUR
        ):
            retired.append(node)
        else:
            other.append(node)
    return retired, lost, other


def _data(node: dict) -> dict[str, Any]:
    beat = node["last_heartbeat"]
    return {
        "region": node["region"],
        "provider": node["provider"],
        "product": f"{node['provider']}/{node['product']}",
        "node_id": node["node_id"],
        "host": node["host"],
        "last_heartbeat": beat.astimezone(UTC).isoformat() if beat else None,
    }


async def _event(conn: psycopg.AsyncConnection, node: dict, limit: timedelta, now) -> None:
    beat = node["last_heartbeat"]
    await emit(
        conn,
        EventRow(
            kind="node_offline",
            at=min(beat + limit, now) if beat else now,
            product_id=node["product_id"],
            region_id=node["region_id"],
            signal=-1,
            severity="major",
            data=_data(node),
        ),
    )


async def _lost_hour(conn: psycopg.AsyncConnection, hour: datetime) -> None:
    """One alert for the hour's lost nodes, every one lost so far (name and region only)."""
    nodes = [r["node_id"] for r in await _rows(conn, HOUR_NODES, (hour,))]
    gone = await _rows(
        conn,
        "SELECT n.provider,n.product,r.slug AS region FROM nodes n JOIN runner_regions r "
        "USING(region_id) WHERE n.node_id=ANY(%s) AND n.status='offline' ORDER BY 3,1,2",
        (nodes,),
    )
    names = [f"{g['provider']}/{g['product']} {g['region']}" for g in gone]
    message = (
        f"pulse hour {hour.astimezone(UTC):%Y-%m-%d %H}:00 UTC: {len(gone)} of {len(nodes)} "
        f"nodes lost before the end: {', '.join(names)}"
    )
    await raise_alert(
        conn,
        "pulse_nodes_lost",
        message,
        severity="major",
        dedupe_key=f"pulse_nodes_lost:{hour.astimezone(UTC):%Y%m%dT%H}",
        data={"hour": hour.astimezone(UTC).isoformat(), "lost": names, "nodes": len(nodes)},
    )


async def _node_alert(conn: psycopg.AsyncConnection, node: dict) -> None:
    data = _data(node)
    await raise_alert(
        conn,
        "node_offline",
        f"node {node['node_id']} ({data['product']} in {node['region']}) is offline",
        severity="major",
        dedupe_key=f"node_offline:{node['node_id']}",
        data=data,
    )


async def mark_offline(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """Stale nodes go offline (alert and tape event) or, of a closed pulse hour, retired.

    The event is at the moment the node passed the limit (its last heartbeat plus the limit);
    a node that never sent a heartbeat goes offline when this call sees it."""
    now = _now(now)
    await db.ensure_partition("test_events", now)
    async with db.transaction() as conn:
        limit = timedelta(seconds=float(await setting(conn, "node.offline_seconds")))
        stale = await _rows(
            conn,
            "SELECT n.node_id,n.region_id,r.slug AS region,n.provider,n.product,n.host,"
            "n.last_heartbeat,(SELECT product_id FROM products p WHERE p.provider=n.provider "
            "AND p.name=n.product) AS product_id,l.latest,l.latest_kind FROM nodes n "
            f"JOIN runner_regions r USING(region_id) {LATEST} WHERE {STALE} FOR UPDATE OF n",
            (now - limit,),
        )
        if await setting(conn, "pulse.on_demand"):
            retired, lost, other = await _classify(conn, stale)
        else:
            retired, lost, other = [], {}, stale
        offline = [n for group in lost.values() for n in group] + other
        await conn.execute(
            "UPDATE nodes SET status='retired' WHERE node_id=ANY(%s)",
            ([n["node_id"] for n in retired],),
        )
        await conn.execute(
            "UPDATE nodes SET status='offline' WHERE node_id=ANY(%s)",
            ([n["node_id"] for n in offline],),
        )
        for node in offline:
            await _event(conn, node, limit, now)
        for node in other:
            await _node_alert(conn, node)
        for hour in sorted(lost):
            await _lost_hour(conn, hour)
    return {
        "offline": sorted(n["node_id"] for n in offline),
        "retired": sorted(n["node_id"] for n in retired),
    }
