"""PROXY-AUTH-ALERT-1: the pulse evaluator's alert for a network that refuses our proxy login.

A window in which at least proxy_login.MOSTLY of a network's pulse requests were refused at login,
and of each of at least proxy_login.MIN_REGIONS regions' (refused()), is not measured for the
network (pulse.is_down gives None: no pulse_down, no recovery, its open outages untouched). It
raises one major alert proxy_login_refused per network and hour instead: a new row each hour,
which the server alerts post to Slack (scripts/server-alerts.sh check_alerts), with the window's
refused share, its refused regions and the incident's first window (`since`, kept while one of
the network's alerts stays open). The network's first measured window that is not refused
acknowledges its open ones.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from .. import proxy_login
from .events import raise_alert

KIND = "proxy_login_refused"
# The network's name, the refused regions' names and the first window of its open alerts.
FACTS_SQL = (
    "SELECT p.provider||'/'||p.name AS network,(SELECT coalesce(array_agg(r.slug ORDER BY r.slug),"
    "'{}') FROM runner_regions r WHERE r.region_id=ANY(%(regions)s)) AS regions,"
    "(SELECT min(a.data->>'since') FROM alerts a WHERE a.kind=%(kind)s "
    "AND a.acknowledged_at IS NULL AND a.data->>'product_id'=%(key)s) AS since "
    "FROM products p WHERE p.product_id=%(product)s"
)
CLEAR_SQL = (
    "UPDATE alerts SET acknowledged_at=now() WHERE kind=%s AND acknowledged_at IS NULL "
    "AND data->>'product_id'=%s"
)
MESSAGE = (
    "{network}: {share:.0%} of its pulse requests in {count} regions ({regions}) were refused at "
    "login (HTTP 401 or 407) since {since:%Y-%m-%d %H:%M} UTC. Our login or account, not the "
    "network's downtime: no outage is published while it lasts. Check its credentials and account."
)


def refused(stats: dict[str, Any]) -> bool:
    """Whether a network's window (pulse.window_stats) was refused at login."""
    return len(stats["login"]) >= proxy_login.MIN_REGIONS and proxy_login.refused(
        stats["refused"], stats["requests"]
    )


async def judge(
    conn: psycopg.AsyncConnection, start: datetime, product: int, stats: dict[str, Any]
) -> None:
    """Raise the network's alert for a refused window; acknowledge its open ones after a
    measured window that is not refused. A window without requests changes nothing."""
    if not stats["requests"]:
        return
    if not refused(stats):
        await conn.execute(CLEAR_SQL, (KIND, str(product)))
        return
    params = {"regions": stats["login"], "kind": KIND, "key": str(product), "product": product}
    facts = await (await conn.execute(FACTS_SQL, params)).fetchone()
    earlier = facts["since"]
    since = min(datetime.fromisoformat(earlier), start) if earlier else start
    share = stats["refused"] / stats["requests"]
    regions = list(facts["regions"])
    message = MESSAGE.format(
        network=facts["network"],
        share=share,
        count=len(regions),
        regions=", ".join(regions),
        since=since,
    )
    await raise_alert(
        conn,
        KIND,
        message,
        severity="major",
        dedupe_key=f"{KIND}:{product}:{start:%Y%m%dT%H}",
        data={
            "product_id": product,
            "network": facts["network"],
            "share": round(share, 4),
            "refused": stats["refused"],
            "requests": stats["requests"],
            "regions": regions,
            "since": since.isoformat(),
            "window_start": start.isoformat(),
        },
    )
