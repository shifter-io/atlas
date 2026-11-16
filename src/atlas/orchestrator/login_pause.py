"""PROXY-AUTH-PAUSE-1 (owner rule, 16 Nov 2026): a network refusing our proxy login is paused.

The job login_pause (every minute) pauses each network that has a login trigger and is not
paused yet, with netpause.pause (its pulses, daily tests and browser entry), and posts one
message per pause: the major alert network_paused, a new row each time, which the server alerts
post to Slack (scripts/server-alerts.sh check_alerts). That open alert is the network's pause:
its data hold the reason, the time and what was moved; while it is open later triggers post
nothing. The triggers (proxy_login.MOSTLY of the requests refused at login, HTTP 407):

- an open proxy_login_refused alert: the pulse evaluator's (at least 2 regions refused,
  pulse_login.py) or the daily import's (a network's daily test of an hour, daily_refused);
- a browser run of the last BROWSER_WINDOW, after the network's last resume, mostly refused.

Pausing acknowledges the network's proxy_login_refused alerts: the pause stands for them. A
network whose files have nothing to pause (none of the three parts on this manager) is left as
it is. `atlas daily resume-network` (resume) runs a login test first, puts the parts back,
acknowledges the pause and posts network_resumed. The hour report lists the paused networks on
its "expected" line (paused_line), never as a failure.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import netpause, proxy_login
from .db import Database, setting
from .events import raise_alert
from .pulse_login import KIND as TRIGGER

KIND = "network_paused"
RESUMED = "network_resumed"
REASON = "login refused"
BROWSER_WINDOW = timedelta(hours=1)
MESSAGE = (
    "{network}: the account credentials no longer work (HTTP 407 on {refused} of {requests} "
    "requests since {since:%H:%M} UTC). Its tests are paused. Resume after a one-request login "
    "test passes."
)
_PAUSED = f"SELECT data FROM alerts WHERE kind='{KIND}' AND acknowledged_at IS NULL"
# The open triggers per product: (product_id, refused, requests, since) and whether it is paused.
TRIGGERS_SQL = (
    "SELECT (data->>'product_id')::int AS product_id,(data->>'refused')::bigint AS refused,"
    "(data->>'requests')::bigint AS requests,(data->>'since')::timestamptz AS since "
    f"FROM alerts WHERE kind='{TRIGGER}' AND acknowledged_at IS NULL UNION ALL "
    f"SELECT br.product_id,{proxy_login.REFUSED_SQL},br.requests,br.started_at FROM batch_runs br "
    "JOIN test_batches b USING(batch_id) JOIN test_runs r USING(run_key) WHERE b.kind='browser' "
    f"AND br.finished_at>%(after)s AND NOT {proxy_login.MEASURED_SQL} AND br.finished_at>"
    "coalesce((SELECT max(first_at) FROM alerts WHERE kind=%(resumed)s "
    "AND data->>'product_id'=br.product_id::text),'-infinity')"
)
NETWORK_SQL = "SELECT provider,name FROM products WHERE product_id=%s"
CLEAR_SQL = (
    "UPDATE alerts SET acknowledged_at=now() WHERE kind=ANY(%s) AND acknowledged_at IS NULL "
    "AND data->>'product_id'=%s"
)
RESUMED_SQL = (
    "INSERT INTO alerts(kind,severity,message,data,dedupe_key,acknowledged_at) "
    "VALUES (%s,'major',%s,%s,%s,now())"
)


def _merged(rows: list[dict]) -> dict[int, dict]:
    found: dict[int, dict] = {}
    for row in rows:
        seen = found.setdefault(row["product_id"], {"refused": 0, "requests": 0, "since": None})
        seen["refused"] += row["refused"] or 0
        seen["requests"] += row["requests"] or 0
        if row["since"] and (seen["since"] is None or row["since"] < seen["since"]):
            seen["since"] = row["since"]
    return found


async def login_pause(db: Database, root: Path, now: datetime | None = None) -> dict[str, Any]:
    """Pause every network with a trigger that is not paused yet; returns what it did."""
    now = now or datetime.now(UTC)
    if netpause.quiet(now):
        return {"skipped": "the hours read their files at minutes 54 to 58"}
    params = {"after": now - BROWSER_WINDOW, "resumed": RESUMED}
    async with db.connection() as conn:
        triggers = _merged(await (await conn.execute(TRIGGERS_SQL, params)).fetchall())
        paused = {
            row["data"]["product_id"] for row in await (await conn.execute(_PAUSED)).fetchall()
        }
        browser = await setting(conn, "browser.settings_file")
    places = netpause.Places(root, Path(browser) if browser else None)
    done: dict[str, list] = {"paused": [], "nothing_to_pause": []}
    for product, trigger in triggers.items():
        async with db.transaction() as conn:
            network = await _pause(conn, places, product, trigger | {"now": now}, product in paused)
        if network is None:
            done["nothing_to_pause"].append(product)
        elif product not in paused:
            done["paused"].append(network)
    return done


async def _pause(conn, places: netpause.Places, product: int, trigger: dict, paused: bool):
    """Pause one network (unless it is already): its name, or None when nothing is paused."""
    row = await (await conn.execute(NETWORK_SQL, (product,))).fetchone()
    provider, name = row["provider"], row["name"]
    if not paused:
        browser, now = f"{provider}/{name}", trigger["now"]
        held, problems = await asyncio.to_thread(netpause.pause, places, provider, browser, now)
        if not held:
            return None
        since = trigger["since"] or now
        message = MESSAGE.format(network=provider, **trigger | {"since": since})
        data = {"product_id": product, "network": provider, "browser": browser}
        data |= {"reason": REASON, "since": since.isoformat(), "paused_at": now.isoformat()}
        data |= {"refused": trigger["refused"], "requests": trigger["requests"]}
        data |= {"paused": held, "problems": problems}
        key = f"{KIND}:{product}:{now:%Y%m%dT%H%M%S}"
        await raise_alert(conn, KIND, message, severity="major", dedupe_key=key, data=data)
    await conn.execute(CLEAR_SQL, ([TRIGGER], str(product)))
    return provider


async def daily_refused(db: Database, network: dict, refused: int, requests: int, since) -> None:
    """The daily import's trigger: a network's daily test of the hour `since` was mostly refused
    at login (`network`: its manifest's provider and product). Minor: the pause posts."""
    async with db.transaction() as conn:
        row = await (
            await conn.execute(
                "SELECT product_id FROM products WHERE provider=%s AND name=%s",
                (network["provider"], network["product"]),
            )
        ).fetchone()
        if row is None:
            return
        product = row["product_id"]
        message = (
            f"{network['provider']}: {refused} of {requests} daily test requests of the hour "
            f"{since:%Y-%m-%d %H}:00 UTC were refused at login (HTTP 407)."
        )
        data = {"product_id": product, "network": network["provider"], "source": "daily"}
        data |= {"refused": refused, "requests": requests, "since": since.isoformat()}
        key = f"{TRIGGER}:{product}:daily:{since:%Y%m%dT%H}"
        await raise_alert(conn, TRIGGER, message, severity="minor", dedupe_key=key, data=data)


def resume(conn: psycopg.Connection, places: netpause.Places, network: str, test=None, now=None):
    """`atlas daily resume-network`: the login test, then (when it passes) the parts back, the
    pause acknowledged and network_resumed posted (an acknowledged row: Slack posts it once)."""
    now = now or datetime.now(UTC)
    if netpause.quiet(now):
        raise netpause.PauseError("not at minutes 54 to 58: the hours read the files then")
    summary = (test or netpause.login_test)(places, network)
    login = {key: summary.get(key) for key in ("completed", "successes")}
    login["refused"] = (summary.get("errors") or {}).get(proxy_login.ERROR, 0)
    if not netpause.passed(summary):
        return {"network": network, "resumed": False, "login": login}
    found = netpause.entry(places, network)
    provider, product = found.get("provider", network), found["product"]
    restored, problems = netpause.resume(places, network, f"{provider}/{product}", now)
    row = conn.execute(
        "SELECT product_id FROM products WHERE provider=%s AND name=%s", (provider, product)
    ).fetchone()
    if row is not None:
        conn.execute(CLEAR_SQL, ([KIND, TRIGGER], str(row[0])))
        message = (
            f"{network}: the login test passed ({login['successes']} of {login['completed']} "
            "requests answered, none refused at login). Its tests are resumed."
        )
        data = Jsonb({"product_id": row[0], "network": network})
        key = f"{RESUMED}:{network}:{now:%Y%m%dT%H%M%S}"
        conn.execute(RESUMED_SQL, (RESUMED, message, data, key))
    return {"network": network, "resumed": True, "login": login, "restored": restored} | (
        {"problems": problems} if problems else {}
    )


def paused_line(cur) -> str:
    """The hour report's paused networks (dict-row cursor), for its "expected" line."""
    rows = [row["data"] for row in cur.execute(_PAUSED + " ORDER BY first_at").fetchall()]
    named = [f"{d['network']} ({d['reason']} since {_minute(d['since'])} UTC)" for d in rows]
    return f"; {len(named)} paused: {', '.join(named)}" if named else ""


def _minute(moment: str) -> str:
    return datetime.fromisoformat(moment).astimezone(UTC).strftime("%Y-%m-%d %H:%M")
