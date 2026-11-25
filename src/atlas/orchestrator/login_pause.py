"""PROXY-AUTH-PAUSE-1 (owner rule, 16 Nov 2026): a network refusing our proxy login is paused.

The job login_pause (every minute) pauses each network that has a login trigger and is not
paused yet, with netpause.pause (its pulses, daily tests and browser entry), and posts one
message per pause: the major alert network_paused, a new row each time, which the server alerts
post to Slack (scripts/server-alerts.sh check_alerts). That open alert is the network's pause:
its data hold the reason, the time and what was moved; while it is open later triggers post
nothing. The triggers (proxy_login.MOSTLY of the requests refused at login, HTTP 401 or 407, by a
gateway's 403 refusing our account the IP-check host, WEBSHARE-ECHO-2, or for payment, HTTP 402):

- an open proxy_login_refused alert: the pulse evaluator's (at least 2 regions refused,
  pulse_login.py) or the daily import's (a network's daily test of an hour, daily_refused);
- a browser run of the last BROWSER_WINDOW, after the network's last resume, mostly refused.

Pausing acknowledges the network's proxy_login_refused alerts: the pause stands for them. A
network whose files have nothing to pause (none of the three parts on this manager) is left as
it is. `atlas daily resume-network` (resume) runs a login test first, puts the parts back,
acknowledges the pause and posts network_resumed. The hour report lists the paused networks on
its "expected" line (paused_line), never as a failure.

PROXY-AUTH-PAUSE-2 (#1413): a paused network is not measured, so no verdict would end its open
outages and each would count to the end of every window. Each tick therefore ends every open
outage of a network with an open pause (all sources: per-country and network-wide pulse, census)
at the pause's `since`, the first refused request its triggers saw, never before the outage's own
start: one opened by a partly refused pulse ends at its start and adds no downtime. The end_reason
is 'not_measured' (pulse_unmeasured.END_REASON), with no tape event; the pause's data lists the
ends in `closed_outages`. Pulses of the pause's hour still running are covered too. Undo:
`atlas daily reopen-outages <network>` (reopen) reopens them and marks the pause `outages_kept`,
after which its ticks end none.

PAUSE-402-1: a network whose refused requests were mostly refused for payment (HTTP 402: our
account's balance or plan, proxy_login.payment) is paused the same way, with the reason
PAYMENT_REASON and PAYMENT_MESSAGE ("payment required"), so the page says what to fix.
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
from .pulse_unmeasured import END_REASON

KIND = "network_paused"
RESUMED = "network_resumed"
REASON = "login refused"
PAYMENT_REASON = "payment required"
BROWSER_WINDOW = timedelta(hours=1)
MESSAGE = (
    "{network}: the account credentials no longer work, or the account is refused the IP-check "
    "host (HTTP 401, 403 or 407 on {refused} of {requests} requests since {since:%H:%M} UTC). "
    "Its tests are paused and its open outages end, not measured, at {since:%H:%M} UTC. Resume "
    "after a one-request login test passes."
)
PAYMENT_MESSAGE = (
    "{network}: payment required. The gateway refuses our account for payment (HTTP 402 on "
    "{payment} of {requests} requests since {since:%H:%M} UTC). Its tests are paused and its "
    "open outages end, not measured, at {since:%H:%M} UTC. Top up or renew the account, then "
    "resume after a one-request login test passes."
)
_PAUSED = f"SELECT data FROM alerts WHERE kind='{KIND}' AND acknowledged_at IS NULL"
# The network's last resume: a trigger counts only for refusals after it (the runs of the hour of
# a pause go on, and their imports or windows can still raise one after the resume).
_RESUMED = (
    "coalesce((SELECT max(z.first_at) FROM alerts z WHERE z.kind=%(resumed)s "
    "AND z.data->>'product_id'={}),'-infinity')"
)
# The open triggers per product: (product_id, refused, requests, since, payment).
TRIGGERS_SQL = (
    "SELECT (a.data->>'product_id')::int AS product_id,(a.data->>'refused')::bigint AS refused,"
    "(a.data->>'requests')::bigint AS requests,(a.data->>'since')::timestamptz AS since,"
    "coalesce((a.data->>'payment')::bigint,0) AS payment "
    f"FROM alerts a WHERE a.kind='{TRIGGER}' AND a.acknowledged_at IS NULL "
    "AND coalesce(a.data->>'window_start',a.data->>'since')::timestamptz>"
    + _RESUMED.format("a.data->>'product_id'")
    + f" UNION ALL SELECT br.product_id,{proxy_login.REFUSED_SQL},br.requests,br.started_at,"
    f"{proxy_login.PAYMENT_SQL} "
    "FROM batch_runs br JOIN test_batches b USING(batch_id) JOIN test_runs r USING(run_key) "
    f"WHERE b.kind='browser' AND br.finished_at>%(after)s AND NOT {proxy_login.MEASURED_SQL} "
    "AND br.finished_at>" + _RESUMED.format("br.product_id::text")
)
COUNTS = ("refused", "requests", "payment")
NETWORK_SQL = "SELECT provider,name FROM products WHERE product_id=%s"
CLEAR_SQL = (
    "UPDATE alerts SET acknowledged_at=now() WHERE kind=ANY(%s) AND acknowledged_at IS NULL "
    "AND data->>'product_id'=%s"
)
# Every open outage of a network under an open pause that keeps its outages' ends.
END_SQL = (
    "UPDATE outages o SET ended_at=greatest(o.started_at,(a.data->>'since')::timestamptz),"
    "end_reason=%(reason)s FROM alerts a WHERE a.kind=%(kind)s AND a.acknowledged_at IS NULL "
    "AND NOT a.data ? 'outages_kept' AND (a.data->>'product_id')::int=o.product_id "
    "AND o.ended_at IS NULL RETURNING a.alert_id,o.outage_id"
)
LIST_SQL = (
    "UPDATE alerts SET data=jsonb_set(data,'{closed_outages}',"
    "coalesce(data->'closed_outages','[]')||%s) WHERE alert_id=%s"
)
LATEST_SQL = (
    f"SELECT alert_id,data FROM alerts WHERE kind='{KIND}' AND data->>'network'=%s "
    "ORDER BY first_at DESC,alert_id DESC LIMIT 1"
)
# The pause's ended outages, unless another closer ended them or one is open in their country.
REOPEN_SQL = (
    "UPDATE outages o SET ended_at=NULL,end_reason=NULL WHERE o.outage_id=ANY(%s) "
    "AND o.end_reason=%s AND NOT EXISTS (SELECT 1 FROM outages x WHERE "
    "x.product_id=o.product_id AND x.country_code=o.country_code AND x.ended_at IS NULL) "
    "RETURNING o.outage_id"
)
KEEP_SQL = "UPDATE alerts SET data=data||'{\"outages_kept\":true}' WHERE alert_id=%s"
RESUMED_SQL = (
    "INSERT INTO alerts(kind,severity,message,data,dedupe_key,acknowledged_at) "
    "VALUES (%s,'major',%s,%s,%s,now())"
)


def _merged(rows: list[dict]) -> dict[int, dict]:
    found: dict[int, dict] = {}
    for row in rows:
        seen = found.setdefault(row["product_id"], dict.fromkeys(COUNTS, 0) | {"since": None})
        for key in COUNTS:
            seen[key] += row[key] or 0
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
    done: dict[str, Any] = {"paused": [], "nothing_to_pause": []}
    for product, trigger in triggers.items():
        async with db.transaction() as conn:
            network = await _pause(conn, places, product, trigger | {"now": now}, product in paused)
        if network is None:
            done["nothing_to_pause"].append(product)
        elif product not in paused:
            done["paused"].append(network)
    async with db.transaction() as conn:
        done["outages_ended"] = await end_outages(conn)
    return done


async def end_outages(conn) -> int:
    """End the open outages of every paused network (PROXY-AUTH-PAUSE-2, module docstring) and
    list them on its pause; returns how many. Idempotent."""
    rows = await (await conn.execute(END_SQL, {"reason": END_REASON, "kind": KIND})).fetchall()
    ended: dict[int, list[int]] = {}
    for row in rows:
        ended.setdefault(row["alert_id"], []).append(row["outage_id"])
    for alert, outages in ended.items():
        await conn.execute(LIST_SQL, (Jsonb(sorted(outages)), alert))
    return len(rows)


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
        paid = proxy_login.payment(trigger["payment"], trigger["refused"])
        text, reason = (PAYMENT_MESSAGE, PAYMENT_REASON) if paid else (MESSAGE, REASON)
        message = text.format(network=provider, **trigger | {"since": since})
        data = {"product_id": product, "network": provider, "browser": browser}
        data |= {"reason": reason, "since": since.isoformat(), "paused_at": now.isoformat()}
        data |= {key: trigger[key] for key in COUNTS}
        data |= {"paused": held, "problems": problems}
        key = f"{KIND}:{product}:{now:%Y%m%dT%H%M%S}"
        await raise_alert(conn, KIND, message, severity="major", dedupe_key=key, data=data)
    await conn.execute(CLEAR_SQL, ([TRIGGER], str(product)))
    return provider


async def daily_refused(db: Database, network: dict, counts: dict, since) -> None:
    """The daily import's trigger: a network's daily test of the hour `since` was mostly refused
    at login or for payment (`network`: its manifest's provider and product; `counts`: its
    refused, requests and payment, COUNTS). Minor: the pause posts."""
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
        paid = proxy_login.payment(counts["payment"], counts["refused"])
        how = "for payment (HTTP 402)" if paid else "at login or for the IP-check host"
        message = (
            f"{network['provider']}: {counts['refused']} of {counts['requests']} daily test "
            f"requests of the hour {since:%Y-%m-%d %H}:00 UTC were refused {how}."
        )
        data = {"product_id": product, "network": network["provider"], "source": "daily"}
        data |= {key: counts[key] for key in COUNTS} | {"since": since.isoformat()}
        key = f"{TRIGGER}:{product}:daily:{since:%Y%m%dT%H}"
        await raise_alert(conn, TRIGGER, message, severity="minor", dedupe_key=key, data=data)


def resume(conn: psycopg.Connection, places: netpause.Places, network: str, test=None, now=None):
    """`atlas daily resume-network`: the login test, then (when it passes) the parts back, the
    pause acknowledged and network_resumed posted (an acknowledged row: Slack posts it once);
    nothing is posted for a network that was not paused (no open pause, nothing moved back)."""
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
    restored, problems, moved = netpause.resume(places, network, f"{provider}/{product}", now)
    row = conn.execute(
        "SELECT product_id FROM products WHERE provider=%s AND name=%s", (provider, product)
    ).fetchone()
    pause = "SELECT count(*) FROM alerts WHERE kind=%s AND acknowledged_at IS NULL "
    pause += "AND data->>'product_id'=%s"
    was = row is not None and conn.execute(pause, (KIND, str(row[0]))).fetchone()[0]
    if row is not None and (was or moved):
        conn.execute(CLEAR_SQL, ([KIND, TRIGGER], str(row[0])))
        message = (
            f"{network}: the login test passed ({login['successes']} of {login['completed']} "
            "requests answered, none refused at login). Its tests are resumed."
        )
        data = Jsonb({"product_id": row[0], "network": network})
        key = f"{RESUMED}:{network}:{now:%Y%m%dT%H%M%S}"
        conn.execute(RESUMED_SQL, (RESUMED, message, data, key))
    done = {"network": network, "resumed": True, "posted": bool(was or moved), "login": login}
    return done | {"restored": restored} | ({"problems": problems} if problems else {})


def reopen(conn: psycopg.Connection, network: str) -> dict[str, Any]:
    """`atlas daily reopen-outages`: undo PROXY-AUTH-PAUSE-2 for the network's latest pause
    (`network`: the name its alert gives): reopen the outages it ended (those another closer
    ended since, or whose country has an open outage, stay as they are) and mark the pause
    `outages_kept`, so its ticks end none. A network never paused reopens nothing."""
    row = conn.execute(LATEST_SQL, (network,)).fetchone()
    if row is None:
        return {"network": network, "paused": False, "reopened": []}
    alert, data = row
    listed = data.get("closed_outages") or []
    reopened = [r[0] for r in conn.execute(REOPEN_SQL, (listed, END_REASON)).fetchall()]
    conn.execute(KEEP_SQL, (alert,))
    done = {"network": network, "paused": True, "reopened": sorted(reopened)}
    return done | {"kept": sorted(set(listed) - set(reopened))}


def paused_line(cur) -> str:
    """The hour report's paused networks (dict-row cursor), for its "expected" line."""
    rows = [row["data"] for row in cur.execute(_PAUSED + " ORDER BY first_at").fetchall()]
    named = [f"{d['network']} ({d['reason']} since {_minute(d['since'])} UTC)" for d in rows]
    return f"; {len(named)} paused: {', '.join(named)}" if named else ""


def _minute(moment: str) -> str:
    return datetime.fromisoformat(moment).astimezone(UTC).strftime("%Y-%m-%d %H:%M")
