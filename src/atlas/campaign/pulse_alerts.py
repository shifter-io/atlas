"""PULSE-STATE-1: the pulse hour's manager alerts, one short line per real problem.

Each is a major row of the orchestrator's `alerts` table, one per kind and hour, which the server
alerts post to Slack as one line (scripts/server-alerts.sh check_alerts). `daily pulse-hour`
exits 3 only for an hour that opened one of them, so its unit's own failure needs no message
(the pulse unit counts exit 3 as a success: SuccessExitStatus=3). An hour whose results only wait
for the manager to store them is done and opens none.

- pulse_hour_failed: no droplet created (every create failed, `fleet.json` or the cap refused
  the hour), or the orchestrator opened no pulse (`pulse.on_demand` off);
- pulse_cells_lost: cells with no droplet of their network left to run their countries;
- pulse_runs_open: one per network and region with pulses still not run or not uploaded at the
  run limit (a node lost mid-run), in the wording the orchestrator accepted (RUNS_OPEN);
- pulse_teardown_failed and pulse_leftover: droplets not verified gone, or a finished hour's
  droplets still up (pulse_ondemand).
"""

from __future__ import annotations

from datetime import datetime

from ..orchestrator.events import raise_alert
from ..regions import REGION_CITIES

SHOWN = 10  # the nodes or cells one message names; the rest are counted
RUNS_OPEN = (
    "{title}: {runs} of {network} in {place} {verb} not measured this hour (a gap in its data, "
    "not counted as down). Once: nothing to do. Every hour: tell the operator."
)


async def alert(db, hour: datetime, kind: str, message: str, data: dict, severity="major"):
    """An alert, major unless told, one per kind and hour (Slack takes the major rows); a
    `subject` in `data` makes it one per kind, hour and subject."""
    subject = f":{data['subject']}" if "subject" in data else ""
    async with db.transaction() as conn:
        await raise_alert(
            conn,
            kind,
            message,
            severity=severity,
            dedupe_key=f"{kind}:{hour:%Y%m%dT%H}{subject}",
            data={"hour": hour.isoformat()} | data,
        )


def title(hour: datetime) -> str:
    return f"pulse hour {hour:%Y-%m-%d %H}:00 UTC"


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def names(items: list[str], sep: str = ", ") -> str:
    """At most SHOWN names, then how many more."""
    shown = sep.join(items[:SHOWN])
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
    for node, count in outcome["open_nodes"].items():  # none unless runs are still open
        await runs_open(db, hour, node, count)


async def runs_open(db, hour: datetime, node: str, count: int) -> None:
    """One line for a network and region whose pulses were not measured by the run limit."""
    network, region = node.split("/", 1)
    city = REGION_CITIES.get(region)
    place = f"{city} ({region})" if city else region
    runs = plural(count, "country pulse")
    verb = "was" if count == 1 else "were"
    message = RUNS_OPEN.format(
        title=title(hour), runs=runs, network=network, place=place, verb=verb
    )
    await alert(db, hour, "pulse_runs_open", message, {"subject": node, "runs": count})
