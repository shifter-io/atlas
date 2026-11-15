"""PULSE-STATE-1: the pulse hour's manager alerts, one short line per real problem.

Each is a major row of the orchestrator's `alerts` table, one per kind and hour, which the server
alerts post to Slack as one line (scripts/server-alerts.sh check_alerts). `daily pulse-hour`
exits 3 only for an hour that opened one of them, so its unit's own failure needs no message
(the pulse unit counts exit 3 as a success: SuccessExitStatus=3). An hour whose results only wait
for the manager to store them is done and opens none.

- pulse_hour_failed: no droplet created (every create failed, `fleet.json` or the cap refused
  the hour), or the orchestrator opened no pulse (`pulse.on_demand` off);
- pulse_cells_lost: cells with no droplet of their network left to run their countries;
- pulse_runs_open: pulses still not run or not uploaded at the run limit (a node lost mid-run);
- pulse_teardown_failed and pulse_leftover: droplets not verified gone, or a finished hour's
  droplets still up (pulse_ondemand).
"""

from __future__ import annotations

from datetime import datetime

from ..orchestrator.events import raise_alert

SHOWN = 10  # the nodes or cells one message names; the rest are counted


async def alert(db, hour: datetime, kind: str, message: str, data: dict) -> None:
    """A major alert, one per kind and hour (Slack takes the alerts table's major rows)."""
    async with db.transaction() as conn:
        await raise_alert(
            conn,
            kind,
            message,
            severity="major",
            dedupe_key=f"{kind}:{hour:%Y%m%dT%H}",
            data={"hour": hour.isoformat()} | data,
        )


def title(hour: datetime) -> str:
    return f"pulse hour {hour:%Y-%m-%d %H}:00 UTC"


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def names(items: list[str]) -> str:
    """At most SHOWN names, then how many more."""
    shown = ", ".join(items[:SHOWN])
    return shown + (f" and {len(items) - SHOWN} more" if len(items) > SHOWN else "")


async def hour_failed(db, hour: datetime, cause: str, data: dict) -> None:
    await alert(db, hour, "pulse_hour_failed", f"{title(hour)}: {cause}", data)


async def hour_alerts(db, hour: datetime, cells: int, outcome: dict) -> None:
    """The alerts of an hour that ran: none created, none opened, cells lost, runs still open."""
    if cells and not outcome["created"]:
        causes = ", ".join(f"{k} ({n})" for k, n in outcome["create_failed"].items())
        cause = f"0 of {cells} droplets created: {causes or 'not_created'}"
        await hour_failed(db, hour, cause, {"create_failed": outcome["create_failed"]})
        return
    if "skipped" in outcome["opened"]:
        await hour_failed(db, hour, f"no pulse opened: {outcome['opened']['skipped']}", {})
    if outcome["lost"]:
        lost = outcome["lost"]
        message = f"{title(hour)}: {plural(len(lost), 'cell')} lost: {names(lost)}"
        await alert(db, hour, "pulse_cells_lost", message, {"cells": lost})
    if outcome["still_open"]:
        nodes = outcome["open_nodes"]
        message = (
            f"{title(hour)}: {plural(outcome['still_open'], 'pulse')} not run or not uploaded "
            f"by the limit, on {names(nodes)}"
        )
        await alert(db, hour, "pulse_runs_open", message, {"nodes": nodes})
