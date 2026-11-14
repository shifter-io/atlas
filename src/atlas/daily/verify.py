"""VERIFY-HOUR-1: a read-only health report of one UTC hour (`atlas daily verify-hour`).

For the hour it compares what was due with what was stored, per network and in total:

- daily tests: the countries the planner put in the hour (planner.plan_hour) against the daily
  runs (MARKER) that started in it: done (completed), failed (another status) or missing;
- pulses: every country of the hour's on-demand pulse cells (pulses.pulse_hour) once per
  pulse-enabled HTTP target, against the runs of the hour's pulse batches (stored: run_key set;
  failed, killed or skipped: failed), with their requests; none are due with pulse.on_demand off;
- browser runs: the runs of the browser batches closed in the 24 hours to the hour's end, per
  network and target, against those uploaded (run_key set); a skipped batch has no network (-);
- processing: the ingest lag (the newest stored run's finish, at most LAG before now), the
  observations and distinct IPs stored in the hour per network (a network due with none fails),
  whether the calculation covering the hour ran (the current build's as_of at or after the
  hour's end) and the last calculate job, and the hour's tape rows (each due daily cell needs
  its daily_test_finished, at the test's own time);
- enrichment: the DB-IP release the hour's runs were enriched with (at most DBIP_AGE old) and
  the runs with none;
- alerts: the open alerts above info, and the jobs that failed since the hour began.

A check is ok (True), failed (False) or None: off, or not due yet. Missing results and a
calculation not yet run are not due until SETTLE after the hour's end. The connection is
read-only, so the report can never write.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from psycopg.rows import dict_row

from ..orchestrator.control import read_setting
from ..storage import connect
from . import DAILY, MARKER
from .planner import HourPlan, load_schedules, plan_hour
from .pulses import TARGETS_SQL, pulse_hour
from .runner import start_hour

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
SETTLE = timedelta(minutes=30)  # after the hour's end, a missing result is a failure
LAG = timedelta(minutes=90)  # the newest stored run finished at most this long ago
DBIP_AGE = timedelta(days=45)  # DB-IP releases monthly
SHOWN = 5  # the keys named per failed or missing list
FAILED = ("failed", "killed", "skipped")  # a pulse run that ended without a stored result

DAILY_SQL = (
    "SELECT p.provider AS network,upper(ps.requested_country) AS country,t.status "
    "FROM test_runs t JOIN scope_stats s USING(scope_id) JOIN pool_stats ps USING(pool_id) "
    "JOIN products p ON p.product_id=s.product_id "
    "WHERE t.summary->>%s=%s AND t.started_at>=%s AND t.started_at<%s"
)
PULSE_SQL = (
    "SELECT p.provider AS network,b.country_code AS country,b.target_id AS target,br.status,"
    "br.run_key IS NOT NULL AS stored,br.requests FROM test_batches b "
    "JOIN batch_runs br USING(batch_id) JOIN products p USING(product_id) "
    "WHERE b.kind='pulse' AND b.scheduled_for=%s"
)
BROWSER_SQL = (
    "SELECT coalesce(p.provider,'-') AS network,b.target_id AS target,count(*) AS due,"
    "count(br.run_key) AS uploaded FROM test_batches b LEFT JOIN batch_runs br USING(batch_id) "
    "LEFT JOIN products p USING(product_id) WHERE b.kind='browser' AND b.start_at>=%s "
    "AND b.start_at<%s AND b.status NOT IN ('scheduled','running') GROUP BY 1,2 ORDER BY 1,2"
)
OBSERVED_SQL = (
    "SELECT p.provider AS network,sum(io.count) AS observations,count(DISTINCT io.ip_id) AS ips "
    "FROM ip_observations io JOIN test_runs t USING(run_key) JOIN scope_stats s USING(scope_id) "
    "JOIN products p USING(product_id) WHERE io.bucket_start>=%s AND io.bucket_start<%s "
    "GROUP BY 1 ORDER BY 1"
)
EVENTS_SQL = (
    "SELECT coalesce(p.provider,'-') AS network,count(*) AS events,"
    "count(*) FILTER (WHERE e.kind='daily_test_finished') AS finished FROM test_events e "
    "LEFT JOIN products p USING(product_id) WHERE e.at>=%s AND e.at<%s GROUP BY 1 ORDER BY 1"
)
CALC_SQL = (
    "SELECT c.build_id,b.as_of,b.created_at FROM current_calculation c "
    "JOIN calculation_builds b USING(build_id)"
)
JOB_SQL = (
    "SELECT status,started_at FROM job_runs WHERE job='calculate' ORDER BY started_at DESC LIMIT 1"
)
LAG_SQL = "SELECT max(finished_at) AS newest FROM test_runs"
DBIP_SQL = (
    "SELECT count(*) AS runs,count(*) FILTER (WHERE r.built_at IS NULL) AS bare,"
    "max(r.built_at) AS built_at FROM test_runs t LEFT JOIN LATERAL (SELECT "
    "max(d.metadata->>'built_at') AS built_at FROM run_enrichments e JOIN dbip_releases d "
    "USING(release_id) WHERE e.run_key=t.run_key) r ON true "
    "WHERE t.started_at>=%s AND t.started_at<%s"
)
ALERTS_SQL = (
    "SELECT kind,severity,occurrences FROM alerts WHERE acknowledged_at IS NULL "
    "AND severity<>'info' ORDER BY last_at DESC"
)
FAILED_JOBS_SQL = (
    "SELECT job,count(*) AS failures FROM job_runs WHERE status='failed' AND started_at>=%s "
    "GROUP BY job ORDER BY job"
)


@dataclass(frozen=True, slots=True)
class Check:
    section: str
    subject: str  # a network, "network target", or "total" for the section's line
    ok: bool | None  # None: off or not due yet, shown but never a failure
    detail: str


@dataclass(frozen=True)
class Report:
    hour: datetime
    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok is not False for check in self.checks)

    def first_failure(self) -> Check | None:
        """The first failed network check, else the first failed section."""
        failed = [c for c in self.checks if c.ok is False]
        return next((c for c in failed if c.subject != "total"), failed[0] if failed else None)

    def to_dict(self) -> dict:
        first = self.first_failure()
        return {
            "hour": self.hour.isoformat(),
            "ok": self.ok,
            "first_failure": asdict(first) if first else None,
            "checks": [asdict(check) for check in self.checks],
        }


@dataclass(frozen=True)
class Hour:
    """The hour under report, now, and whether a missing result is a failure yet."""

    start: datetime
    now: datetime

    @property
    def end(self) -> datetime:
        return self.start + HOUR

    @property
    def settled(self) -> bool:
        return self.now >= self.end + SETTLE


def hour_of(value: str, now: datetime) -> datetime:
    """last-hour (the hour that just ended), now-hour or an ISO hour with its offset."""
    if value == "last-hour":
        return start_hour("now-hour", now)[0] - HOUR
    return start_hour(value, now)[0]


def _named(word: str, keys: list[str]) -> str:
    if not keys:
        return ""
    more = ", ..." if len(keys) > SHOWN else ""
    return f", {len(keys)} {word} ({', '.join(keys[:SHOWN])}{more})"


def tally(section: str, subject: str, due: list[str], state: dict, settled: bool) -> Check:
    """One subject's due keys against their state (done, failed, or absent: missing)."""
    failed = [k for k in due if state.get(k) == "failed"]
    missing = [k for k in due if k not in state]
    done = len(due) - len(failed) - len(missing)
    detail = f"{done} of {len(due)} stored" + _named("failed", failed) + _named("missing", missing)
    ok = False if failed or (missing and settled) else None if missing else True
    return Check(section, subject, ok, detail)


def total(section: str, checks: list[Check], detail: str, ok: bool | None = True) -> list[Check]:
    """The subjects' checks, then the section's line: failed if one failed, else None if one is
    (or `ok` is) None."""
    states = [c.ok for c in checks] + [ok]
    state = False if False in states else None if None in states else True
    return [*checks, Check(section, "total", state, detail)]


def _daily(cur, plan: HourPlan, hour: Hour) -> list[Check]:
    state: dict[tuple, str] = {}
    for row in cur.execute(DAILY_SQL, (MARKER, DAILY, hour.start, hour.end)):
        key = (row["network"], row["country"])
        if state.get(key) != "done":  # a rerun's completed run counts
            state[key] = "done" if row["status"] == "completed" else "failed"
    checks = []
    for network in plan.networks():
        due = [c for cell in plan.cells if cell.network == network for c in cell.countries]
        mine = {c: s for (n, c), s in state.items() if n == network}
        checks.append(tally("daily tests", network, due, mine, hour.settled))
    countries = sum(len(cell.countries) for cell in plan.cells)
    detail = f"{len(plan.cells)} cells, {countries} countries due"
    detail += f"; no template entry: {', '.join(plan.skipped)}" if plan.skipped else ""
    return total("daily tests", checks, detail)


def _pulses(cur, plan: HourPlan, hour: Hour, on: bool) -> list[Check]:
    if not on:
        return total("pulses", [], "off (pulse.on_demand is off)", None)
    targets = [row["target_id"] for row in cur.execute(TARGETS_SQL)]
    state: dict[str, dict] = {}
    requests: dict[str, int] = {}
    for row in cur.execute(PULSE_SQL, (hour.start,)):
        found = "done" if row["stored"] else "failed" if row["status"] in FAILED else None
        if found:
            state.setdefault(row["network"], {})[f"{row['country']} {row['target']}"] = found
        requests[row["network"]] = requests.get(row["network"], 0) + row["requests"]
    checks = []
    for network in plan.networks():
        countries = [c for cell in plan.cells if cell.network == network for c in cell.countries]
        due = [f"{country} {target}" for country in countries for target in targets]
        check = tally("pulses", network, due, state.get(network, {}), hour.settled)
        extra = f", {requests.get(network, 0):,} requests"
        checks.append(Check(check.section, network, check.ok, check.detail + extra))
    detail = f"{len(plan.cells)} cells x {len(targets)} targets, "
    return total("pulses", checks, detail + f"{sum(requests.values()):,} requests")


def _browser(cur, hour: Hour) -> list[Check]:
    rows = cur.execute(BROWSER_SQL, (hour.end - DAY, hour.end)).fetchall()
    if not rows and not read_setting(cur.connection, "browser.enabled"):
        return total("browser 24h", [], "off (browser.enabled is off)", None)
    checks = [
        Check(
            "browser 24h",
            f"{row['network']} {row['target']}",
            row["uploaded"] == row["due"],
            f"{row['uploaded']} of {row['due']} uploaded",
        )
        for row in rows
    ]
    due = sum(row["due"] for row in rows)
    return total("browser 24h", checks, f"{due} runs due in the 24 hours to the hour's end")


def _observed(cur, networks: tuple[str, ...], hour: Hour) -> list[Check]:
    rows = {r["network"]: r for r in cur.execute(OBSERVED_SQL, (hour.start, hour.end))}
    checks = []
    for network in sorted(set(networks) | set(rows)):
        row = rows.get(network) or {"observations": 0, "ips": 0}
        stored = row["observations"] > 0
        ok = True if stored or network not in networks else False if hour.settled else None
        detail = f"{row['observations']:,} observations, {row['ips']:,} IPs"
        checks.append(Check("observations", network, ok, detail))
    count = sum(r["observations"] for r in rows.values())
    return total("observations", checks, f"{count:,} in the hour, {len(rows)} networks")


def _events(cur, plan: HourPlan, hour: Hour) -> list[Check]:
    rows = {r["network"]: r for r in cur.execute(EVENTS_SQL, (hour.start, hour.end))}
    checks = []
    for network in plan.networks():
        cells = sum(1 for cell in plan.cells if cell.network == network)
        finished = rows[network]["finished"] if network in rows else 0
        ok = True if finished >= cells else False if hour.settled else None
        detail = f"{finished} of {cells} daily_test_finished"
        checks.append(Check("events", network, ok, detail))
    count = sum(r["events"] for r in rows.values())
    return total("events", checks, f"{count:,} tape rows in the hour")


def _age(now: datetime, then: datetime) -> str:
    minutes = int((now - then).total_seconds() // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"


def _processing(cur, hour: Hour) -> list[Check]:
    newest = cur.execute(LAG_SQL).fetchone()["newest"]
    lag_ok = newest is not None and hour.now - newest <= LAG
    lag = f"newest stored result {_age(hour.now, newest)} ago" if newest else "nothing stored"
    build, job = cur.execute(CALC_SQL).fetchone(), cur.execute(JOB_SQL).fetchone()
    ran = build is not None and build["as_of"] >= hour.end
    calc_ok = True if ran else False if hour.settled else None
    calc = "no published calculation"
    if build:
        calc = f"snapshot {build['build_id']} as of {build['as_of']:%Y-%m-%d %H:%M}, "
        calc += f"built {_age(hour.now, build['created_at'])} ago"
    calc += f"; last calculate job {job['status']}" if job else "; no calculate job yet"
    return [
        Check("ingest lag", "total", lag_ok, lag),
        Check("calculation", "total", calc_ok, calc),
    ]


def _enrichment(cur, hour: Hour) -> list[Check]:
    row = cur.execute(DBIP_SQL, (hour.start, hour.end)).fetchone()
    if not row["runs"]:
        return [Check("DB-IP", "total", None, "no runs stored in the hour")]
    detail = f"{row['bare']} of {row['runs']} runs not enriched"
    if row["built_at"] is None:
        return [Check("DB-IP", "total", False, detail)]
    built = datetime.fromisoformat(row["built_at"])
    fresh = hour.now - built <= DBIP_AGE
    detail = f"release built {built:%Y-%m-%d}, {(hour.now - built).days} days old; " + detail
    return [Check("DB-IP", "total", fresh and not row["bare"], detail)]


def _alerts(cur, hour: Hour) -> list[Check]:
    alerts = cur.execute(ALERTS_SQL).fetchall()
    named = [f"{a['kind']} ({a['severity']}, {a['occurrences']}x)" for a in alerts]
    jobs = [f"{r['job']} ({r['failures']}x)" for r in cur.execute(FAILED_JOBS_SQL, (hour.start,))]
    return [
        Check("alerts", "total", not alerts, f"{len(alerts)} open" + listed(named)),
        Check("failed jobs", "total", not jobs, f"{len(jobs)} since the hour began" + listed(jobs)),
    ]


def listed(items: list[str]) -> str:
    """': a, b, c, ...' (the first SHOWN), or nothing."""
    if not items:
        return ""
    return ": " + ", ".join(items[:SHOWN]) + (", ..." if len(items) > SHOWN else "")


def report(database_file: Path, formulas: Path, hour: Hour, networks=None) -> Report:
    """The hour's checks, read in one read-only transaction."""
    schedules = load_schedules(formulas)
    daily = plan_hour(schedules, hour.start.hour, networks=networks)
    pulses = pulse_hour(schedules, hour.start.hour, networks=networks)
    with connect(database_file) as conn:
        conn.read_only = True
        cur = conn.cursor(row_factory=dict_row)
        on = bool(read_setting(conn, "pulse.on_demand"))
        due = daily.networks() + (pulses.networks() if on else ())
        checks = [
            *_daily(cur, daily, hour),
            *_pulses(cur, pulses, hour, on),
            *_browser(cur, hour),
            *_processing(cur, hour),
            *_observed(cur, due, hour),
            *_events(cur, daily, hour),
            *_enrichment(cur, hour),
            *_alerts(cur, hour),
        ]
    return Report(hour.start, tuple(checks))


MARK = {True: "ok", False: "FAIL", None: "-"}  # None: off or not due yet
LINES = 20  # the network lines shown under the sections


def _line(check: Check) -> str:
    subject = "" if check.subject == "total" else f", {check.subject}"
    return f"{check.section}{subject}: {check.detail}"


def render(report: Report) -> str:
    """One screen: the verdict and first problem, each section's line, then the network
    checks not ok (failed, or not due yet)."""
    sections = [c for c in report.checks if c.subject == "total"]
    failed = sum(c.ok is False for c in sections)
    verdict = "OK" if report.ok else f"FAIL in {failed} of {len(sections)} sections"
    lines = [f"verify-hour {report.hour:%Y-%m-%d %H}:00 UTC: {verdict}"]
    first = report.first_failure()
    lines += [f"first problem: {_line(first)}"] if first else []
    lines += [""] + [f"{c.section:<13} {MARK[c.ok]:<4} {c.detail}" for c in sections]
    shown = [c for c in report.checks if c.subject != "total" and c.ok is not True]
    if shown:
        lines += ["", "not ok:"] + [f"  {MARK[c.ok]:<4} {_line(c)}" for c in shown[:LINES]]
        lines += [f"  ... and {len(shown) - LINES} more (--json)"] if len(shown) > LINES else []
    return "\n".join(lines)
