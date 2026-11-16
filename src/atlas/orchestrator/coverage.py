"""COVERAGE-ALERT-1 (#1216): every network runs every target, or Slack hears of it.

The job `coverage` (every 30 minutes) compares what each network should run, read from the manager
root's files, with its newest completed run of each in the database (LOOKBACK at most):

- browser: every [[target]] of the browser settings (`browser.settings_file`, while
  `browser.enabled` and the file's own `enabled`) for every network of a [[host]]
  ("<network>/<product>"). Its newest counted run is older than `coverage.browser_hours` (36,
  above the normal cycle: the queue visits a pair once a UTC day at an hour that shifts, so two
  counted runs are up to 28 h apart, and a run ends up to its 3 h deadline later; COVERAGE-ALERT-2):
  uploaded, completed, in a
  country that counted for the network at the run's start (its latest daily test verdict, as
  the calculation counts it with the methodology's country_verdicts: verdicts.COUNTS_AT);
- pulses: every network of `pulse/fleet.json` while `pulse.on_demand`; its newest stored pulse
  run is older than `coverage.pulse_hours` (3);
- daily: every network with a formula in `daily/formulas`; its newest completed daily test is
  older than `coverage.daily_hours` (24).

A network paused for a part (its browser entry "<network>/paused", its fleet entry in
`pulse/paused-networks.json`, its formula in `daily/formulas-paused`: netpause, docs/campaign.html)
is listed as paused in the job's result, never missing. A network with a gap gets one minor alert
coverage_missing per UTC day (dedupe key coverage_missing:<network>:<day>; the day's later checks
update the same row, a new day's row acknowledges the day before's): the dashboard and the hour
report show it, Slack does not (it posts major rows only).

Slack gets ONE major coverage_summary per run at most (COVERAGE-ALERT-2, #1262): every gap, the
worst first (a target that never ran in LOOKBACK, then the longest), and only when a (network,
part, target) pair is new: not in a summary of the last 24 hours, or grown past SEVERE since it
was reported there. So a gap that persists is posted once a day, and a new one at once. A new
summary acknowledges the one before. Once a network has no gap its open coverage alerts are
acknowledged (the alert clears), and once no network has one the summary clears too.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from ..browser.settings import BrowserSettings, load_settings
from ..daily import DAILY, MARKER
from ..errors import UserError
from ..netpause import PAUSED  # a paused network's browser entry: "<network>/paused"
from ..verdicts import counts_at
from ..verdicts import params as params_of
from .db import Database, setting
from .events import raise_alert

KIND = "coverage_missing"
SUMMARY = "coverage_summary"
REPEAT = timedelta(hours=24)  # a pair is posted again after this
SEVERE = timedelta(hours=72)  # a pair reported shorter is posted again once past this
SHOWN = 10  # the networks a summary names; the rest are counted
LOOKBACK = timedelta(days=8)  # a pair with no run since is "not in the last 8 days"
HOUR = timedelta(hours=1)
LIMITS = {
    "browser": "coverage.browser_hours",
    "pulses": "coverage.pulse_hours",
    "daily": "coverage.daily_hours",
}
WORDS = {
    "browser": "browser target {target} has no counted run",
    "pulses": "no pulse stored",
    "daily": "no daily test stored",
}
CONSEQUENCE = (
    "A browser target's value is carried over for up to 7 days; past that, or without pulses or "
    "daily tests, a network may become unranked until it runs again."
)

BROWSER_SQL = (
    "SELECT 'browser' AS part,p.provider AS network,b.target_id AS target,"
    "max(t.finished_at) AS last FROM batch_runs br JOIN test_batches b USING(batch_id) "
    "JOIN products p USING(product_id) JOIN test_runs t ON t.run_key=br.run_key "
    + counts_at("br.product_id", "b.country_code", "t.started_at")
    + "WHERE b.kind='browser' AND b.scheduled_for>=%(since)s AND t.status='completed' "
    "GROUP BY 2,3"
)
PULSE_SQL = (
    "SELECT 'pulses' AS part,p.provider AS network,NULL AS target,max(t.finished_at) AS last "
    "FROM batch_runs br JOIN test_batches b USING(batch_id) JOIN products p USING(product_id) "
    "JOIN test_runs t ON t.run_key=br.run_key WHERE b.kind='pulse' AND b.scheduled_for>=%(since)s "
    "GROUP BY 2"
)
DAILY_SQL = (
    "SELECT 'daily' AS part,p.provider AS network,NULL AS target,max(t.finished_at) AS last "
    "FROM test_runs t JOIN scope_stats s USING(scope_id) JOIN products p USING(product_id) "
    f"WHERE t.summary->>'{MARKER}'='{DAILY}' AND t.status='completed' AND t.started_at>=%(since)s "
    "GROUP BY 2"
)
OPEN_SQL = (
    "SELECT dedupe_key,data FROM alerts WHERE kind=%s AND acknowledged_at IS NULL "
    "ORDER BY data->>'network'"
)
ACK_SQL = "UPDATE alerts SET acknowledged_at=now() WHERE dedupe_key=ANY(%s)"
# The summaries posted in the REPEAT before a time (each carries its own run's time in data.at).
POSTED_SQL = "SELECT data FROM alerts WHERE kind=%s AND (data->>'at')::timestamptz>%s"


@dataclass(frozen=True)
class Plan:
    """Per part, what each network should run ({network: targets}, None for a part without
    targets) and the networks paused for it."""

    due: dict[str, dict[str, tuple[str | None, ...]]]
    paused: dict[str, set[str]]


@dataclass(frozen=True)
class Gap:
    network: str
    part: str
    target: str | None
    hours: int | None  # since its newest run; None: none in LOOKBACK

    @property
    def worst(self) -> tuple:
        """Its place in a summary: never run first, then the longest gap."""
        return (self.hours is not None, -(self.hours or 0))

    @property
    def level(self) -> str:
        """late, or hole: never run in LOOKBACK or past SEVERE."""
        severe = self.hours is None or self.hours >= SEVERE / HOUR
        return "hole" if severe else "late"

    def key(self) -> str:
        return f"{self.network}|{self.part}|{self.target or ''}"

    def words(self) -> str:
        text = WORDS[self.part].format(target=self.target)
        if self.hours is None:
            return f"{text} in the last {LOOKBACK.days} days"
        return f"{text} for {self.hours} hours"


def _json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _names(folder: Path) -> set[str]:
    return {path.stem for path in folder.glob("*.json")} if folder.is_dir() else set()


def plan(root: Path, browser: BrowserSettings | None, pulses: bool) -> Plan:
    """What should run, from the manager root's files and the browser settings (None: off)."""
    due: dict[str, dict] = {part: {} for part in LIMITS}
    paused: dict[str, set[str]] = {part: set() for part in LIMITS}
    due["daily"] = dict.fromkeys(_names(root / "daily" / "formulas"), (None,))
    paused["daily"] = _names(root / "daily" / "formulas-paused")
    if pulses:
        fleet = _json(root / "pulse" / "fleet.json").get("networks") or {}
        due["pulses"] = {entry.get("provider", name): (None,) for name, entry in fleet.items()}
        held = _json(root / "pulse" / "paused-networks.json")
        paused["pulses"] = {entry.get("provider", name) for name, entry in held.items()}
    if browser is not None:
        targets = tuple(target.id for target in browser.targets)
        for host in browser.hosts:
            for entry in host.networks:
                network, product = entry.split("/", 1)
                if product == PAUSED:
                    paused["browser"].add(network)
                else:
                    due["browser"][network] = targets
    return Plan(due, paused)


def gaps(plan: Plan, newest: dict, now: datetime, limits: dict[str, timedelta]) -> list[Gap]:
    """Every due (network, part, target) not paused whose newest run is older than its limit;
    newest maps (part, network, target) to that run's finish."""
    found = []
    for part, networks in plan.due.items():
        for network, targets in sorted(networks.items()):
            if network in plan.paused[part]:
                continue
            for target in targets:
                last = newest.get((part, network, target))
                if last is None or now - last > limits[part]:
                    hours = None if last is None else int((now - last) / HOUR)
                    found.append(Gap(network, part, target, hours))
    return found


def message(network: str, found: Iterable[Gap]) -> str:
    return f"{network}: {'; '.join(gap.words() for gap in found)}. {CONSEQUENCE}"


def short(data: dict) -> str:
    """An open alert's network and gaps in a few words, for the hour report."""
    parts = [
        f"{g['part']}{' ' + g['target'] if g['target'] else ''} "
        + (f"{g['hours']} h" if g["hours"] is not None else f"{LOOKBACK.days}+ d")
        for g in data.get("gaps", [])
    ]
    return f"{data.get('network')} ({', '.join(parts)})"


def _browser_settings(path: Any) -> BrowserSettings | None:
    """The browser settings when they plan; an unreadable file has its own alert (browser.py)."""
    try:
        settings = load_settings(path) if path else None
    except (OSError, UserError):
        return None
    return settings if settings is not None and settings.enabled else None


async def _inputs(conn: psycopg.AsyncConnection, root: Path, now: datetime, rule: int | None):
    browser = None
    if await setting(conn, "browser.enabled"):
        browser = _browser_settings(await setting(conn, "browser.settings_file"))
    the_plan = plan(root, browser, bool(await setting(conn, "pulse.on_demand")))
    limits = {part: timedelta(hours=await setting(conn, key)) for part, key in LIMITS.items()}
    newest = {}
    for sql in (BROWSER_SQL, PULSE_SQL, DAILY_SQL):
        params = {"since": now - LOOKBACK} | params_of(rule)
        for row in await (await conn.execute(sql, params)).fetchall():
            newest[(row["part"], row["network"], row["target"])] = row["last"]
    return the_plan, limits, newest


async def coverage(
    db: Database, root: Path, now: datetime | None = None, verdicts: int | None = None
) -> dict[str, Any]:
    """Raise or clear each network's coverage alert and post the run's summary when something is
    new; returns the gaps, the paused networks, the alerts cleared and whether it posted.
    verdicts: the methodology's country rule (policy.country_verdicts(): a browser run counts
    only in a country that counted for its network then; None: every run counts)."""
    now = now or datetime.now(UTC)
    async with db.connection() as conn:
        the_plan, limits, newest = await _inputs(conn, root, now, verdicts)
    by_network: dict[str, list[Gap]] = {}
    for gap in gaps(the_plan, newest, now, limits):
        by_network.setdefault(gap.network, []).append(gap)
    async with db.transaction() as conn:
        opened = await (await conn.execute(OPEN_SQL, (KIND,))).fetchall()
        for network, found in by_network.items():
            data = {"network": network, "gaps": [asdict(gap) for gap in found]}
            key = f"{KIND}:{network}:{now:%Y%m%d}"
            await raise_alert(
                conn, KIND, message(network, found), severity="minor", dedupe_key=key, data=data
            )
        today = {f"{KIND}:{network}:{now:%Y%m%d}" for network in by_network}
        cleared = [row["dedupe_key"] for row in opened if row["dedupe_key"] not in today]
        posted = await _summary(conn, by_network, now)
        if posted or not by_network:  # a new summary replaces the old; no gap clears it
            summaries = await (await conn.execute(OPEN_SQL, (SUMMARY,))).fetchall()
            cleared += [r["dedupe_key"] for r in summaries if r["dedupe_key"] != posted]
        await conn.execute(ACK_SQL, (cleared,))
    return {
        "missing": {net: [gap.words() for gap in found] for net, found in by_network.items()},
        "paused": {part: sorted(names) for part, names in the_plan.paused.items() if names},
        "cleared": len(cleared),
        "posted": posted is not None,
    }


async def _summary(conn: psycopg.AsyncConnection, by_network: dict, now: datetime) -> str | None:
    """Post the run's summary (a new major row) when a gap is new; returns its key, else None."""
    found = [gap for gaps_ in by_network.values() for gap in gaps_]
    reported: dict[str, str] = {}
    for row in await (await conn.execute(POSTED_SQL, (SUMMARY, now - REPEAT))).fetchall():
        for key, level in row["data"].get("pairs", {}).items():
            reported[key] = "hole" if "hole" in (level, reported.get(key)) else level
    new = [gap for gap in found if reported.get(gap.key()) not in (gap.level, "hole")]
    if not new:
        return None
    key = f"{SUMMARY}:{now:%Y%m%dT%H%M%S}"
    data = {"at": now.isoformat(), "pairs": {gap.key(): gap.level for gap in found}}
    data["new"] = sorted({gap.network for gap in new})
    text = summary_text(by_network, data["new"])
    await raise_alert(conn, SUMMARY, text, severity="major", dedupe_key=key, data=data)
    return key


def summary_text(by_network: dict, new: list[str]) -> str:
    """One Slack line per run: every network's gaps, the worst network first."""
    order = sorted(by_network, key=lambda n: (min(g.worst for g in by_network[n]), n))
    lines = []
    for network in order[:SHOWN]:
        found = sorted(by_network[network], key=lambda g: g.worst)
        lines.append(f"{network}: {'; '.join(g.words() for g in found)}")
    more = f" And {len(order) - SHOWN} more." if len(order) > SHOWN else ""
    head = f"Coverage: {len(order)} networks have targets that did not run (worst first). "
    tail = f" New since the last summary: {', '.join(new)}. {CONSEQUENCE}"
    return head + ". ".join(lines) + "." + more + tail
