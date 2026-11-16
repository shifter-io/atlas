"""PROXY-CHEAP-BUDGET-1: a budgeted network pulses after its daily runs of the hour.

A network with a connection budget (daily.budget) never runs its daily tests and its pulses at
the same time. The hour's pulse job (pulse_ondemand, started at HH:57) creates and arms every
cell as before, so the network's droplets are ready, but opens only the other networks' pulses
at once. Once those are stored (about HH:15 to HH:19) it waits, polling every POLL_SECONDS,
until the network's daily runs of the hour are done (`daily_running` is empty) or GATE_LATEST
past the hour, whichever comes first. Then it opens the network's pulses in their own slot (the
minute it opened, never HH:00, so the batches are the network's own), starting at most
LAST_START past the hour, and waits for them until STOP less REMOVE_LEAD past the hour; every
droplet is removed by about HH:55, before the next hour's job creates the same names at HH:57.

PULSE-STEP-1: the slot starts one country every daily.budget.PULSE_STEP_SECONDS of the held
networks (proxy-cheap: 30 s, a run about every 15 s per droplet, each about 29 s at its share),
none after FINISH_BY less a step, so the last run ends before the wait does (HH:52). fra1, the
largest region (59 countries, fewer with the low-availability skip), takes at most about 30
minutes: from a gate at HH:20, every country starts by HH:50.

The daily runs of the hour are the one-slot campaigns `<campaigns>/<YYYYMMDDtHH>0000z-*` the
daily runner writes beside the template (`campaigns` is the template link's folder; the job's
`--formulas` is `<daily>/formulas` and the template `<daily>/template`). A network's runs are
done when the hour wrote `daily.json`, its ledger state is done or failed, or every cell of the
network is past pending and running. An hour without such a campaign has no daily run to wait
for. At GATE_LATEST with a run still going the pulses start anyway; the job's outcome lists the
cells (`daily_still_running`) and raises one major alert, `pulse_budget_overlap`.

Cost: the network's droplets (8 x s-1vcpu-2gb-amd, pulse_fleet.DEFAULT_SIZE) live from HH:57
until their pulses are stored, about HH:30 when the daily runs end at HH:15, at most about
HH:55: up to 40 minutes longer than the other networks' (about 5.3 droplet-hours an hour, $2.6 a
day, at most).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..daily import budget
from ..orchestrator.pulse_hour import Armed, open_hour

GATE_LATEST = timedelta(minutes=40)
LAST_START = timedelta(minutes=50)
STOP = timedelta(minutes=55)
FINISH_BY = timedelta(minutes=52)  # STOP less pulse_ondemand.REMOVE_LEAD: the late wait's end
POLL_SECONDS = 15.0
WAITING = ("pending", "running")
FINAL = ("done", "failed")


def campaigns_dir(formulas: Path) -> Path:
    """Where the daily runner writes its hour campaigns: beside the template it runs from."""
    return (formulas.parent / "template").resolve().parent


def _waiting(root: Path, networks: set[str]) -> list[str]:
    """One hour campaign's cells of `networks` not done yet."""
    try:
        spec = json.loads((root / "campaign-spec.json").read_text())
    except (OSError, ValueError):
        return []  # not a campaign the runner finished writing
    due = sorted(networks & set(spec.get("networks") or ()))
    if spec.get("hours") != 1 or not due or (root / "daily.json").exists():
        return []  # the template (24 slots), another network's hour or a finished hour
    try:
        record = json.loads((root / "ledger" / "00" / "hour.json").read_text())
    except (OSError, ValueError):
        return [f"{network}/launching" for network in due]
    if record.get("state") in FINAL:
        return []
    cells = record.get("cells") or {}
    keys = [key for key in cells if key.split("/")[0] in due]
    if record.get("state") in ("planned", "launching") and not keys:
        return [f"{network}/launching" for network in due]
    return sorted(key for key in keys if cells[key] in WAITING)


def daily_running(campaigns: Path, hour: datetime, networks: set[str]) -> list[str]:
    """The hour's daily cells of `networks` still running ("network/region"); [] once done."""
    found: list[str] = []
    for root in sorted(campaigns.glob(f"{hour:%Y%m%dt%H}0000z-*")):
        found += _waiting(root, networks)
    return found


async def wait_for_daily(clock, campaigns: Path, hour: datetime, networks: set[str]) -> dict:
    """Wait until the networks' daily runs of the hour are done or GATE_LATEST past it."""
    latest = (hour + GATE_LATEST).timestamp()
    while True:
        running = daily_running(campaigns, hour, networks)
        now = clock.now()
        if not running or now >= latest:
            at = datetime.fromtimestamp(now, UTC).isoformat()
            if not running:
                return {"gate": "daily_done", "at": at}
            return {"gate": "latest", "at": at, "daily_still_running": running}
        await clock.sleep(min(POLL_SECONDS, latest - now))


def slot(hour: datetime, now: datetime) -> datetime:
    """The late batches' scheduled_for: the minute now, at least one minute past the hour."""
    minutes = max(1, min(59, int((now - hour).total_seconds() // 60)))
    return hour + timedelta(minutes=minutes)


def last_start(step: timedelta | None) -> timedelta:
    """The latest start past the hour: LAST_START, earlier when a step's run would end after
    FINISH_BY."""
    return min(LAST_START, FINISH_BY - step) if step else LAST_START


async def open_late(db, clock, hour: datetime, cells: list[Armed], networks=()) -> dict:
    """Open the held cells' pulses in their own slot, one country every PULSE_STEP_SECONDS of
    their `networks` (daily.budget), starting by last_start past the hour."""
    now = datetime.fromtimestamp(clock.now(), UTC)
    found, step = slot(hour, now), budget.pulse_step(networks)
    latest = last_start(step)
    opened = await open_hour(db, hour, cells, now, slot=found, last_start=latest, step=step)
    paced = {"step_seconds": step.total_seconds()} if step else {}
    return opened | {"slot": found.isoformat()} | paced


def keys(config) -> set[tuple[str, str]]:
    """(provider, product) of the fleet's networks the hour holds back (FleetConfig)."""
    return {
        (config.networks[name].provider, config.networks[name].product)
        for name in config.max_connections
    }
