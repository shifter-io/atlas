"""Pulse planning and the network-wide pulse evaluator.

Planning (pulse_slots.py): a region's countries are spread over a slot (interval / N apart), one
at a time.

Evaluation (job pulse_evaluate): reads only the pulse runs' 5-minute run_buckets, not their IP
rows. Each complete window sums a network's connection errors (timeouts + proxy errors) over
every region. A window is down when the summed error share is at least pulse.down_error_share
and at least pulse.down_min_regions regions are each over that share; the network's outage
then opens as one outages row per enabled country it was pulsed in by its last pulse interval
(source 'pulse', batch_id NULL, which is how a network outage is told apart from a per-batch
one) and a pulse_down event lists them. After pulse.recover_windows healthy windows the rows
close and pulse_recovered lists them. A window without pulse data for the network ends its open
outage there, not measured (pulse_unmeasured.py, OUTAGE-NOT-MEASURED-1). The two events are
at the outage's start (the down window's start) and end (the first healthy window's start), not
at the evaluation. Each evaluation also closes the per-country pulse outages (detect.py) the
data says are over (pulse_stale.py, PULSE-OUTAGE-CLOSE-1). A window whose requests were mostly
refused at login (proxy_login.py) is not judged for the network, neither down nor healthy (an
open outage stays as it is), and raises proxy_login_refused instead (pulse_login.py,
PROXY-AUTH-ALERT-1). The runs that got only the network's own "no exit" answers
are left out of a window (pulse_no_exit.strip, PULSE-NOEXIT-OUTAGE-1) unless the network guard
holds. Latency:
latency_rise fires when a network's last pulse.latency_windows done pulse runs in a country
(runs, one per slot, not 5-minute windows) all have a p50 at least pulse.latency_ratio x
latency_baseline() (the daily test's p50), which a pulse never moves; it is at the finish of
the streak's newest run.

A window is evaluated once, pulse.settle_seconds after it ends: buckets uploaded later never
count toward its downtime, and a run that turns done later is never a latency candidate, so
settle_seconds must cover the observed upload lag. Every query is bounded by time (a window's
batches began at most batch.start_grace_seconds before it, see PULSES), so run_buckets
partitions prune and test_batches is read through its (kind, target_id, scheduled_for) index.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from .. import proxy_login, pulse_no_exit, verdicts
from ..daily import DAILY, MARKER
from ..detect import LOCK as DETECT_LOCK
from ..detect import RUN_P50_SQL, country_rule
from . import pulse_login, pulse_stale, pulse_unmeasured
from .db import Database, put_setting, setting
from .events import (
    LatencyRise,
    PulseWindow,
    emit_latency_rise,
    emit_pulse_down,
    emit_pulse_recovered,
)
from .pulse_slots import GRACE, MIN_GAP, open_slot, spread  # noqa: F401 (callers import them here)

WINDOW = timedelta(minutes=5)
MAX_WINDOWS = 12  # windows evaluated per tick; a backlog continues on the next ticks

# Pulse batches that may hold a run started in [%(lo)s, %(hi)s). A batch starts within one
# interval (plus the lead) of its slot, so the targets join turns the range into an index range
# on test_batches(kind, target_id, scheduled_for); 2 intervals keep a lowered interval safe. A
# run starts at its batch's start or, moved from a stalled node (PULSE-STATE-2), later at its
# own (batch_runs.start_at): RUNS picks the runs by that, so the batch's other runs keep their
# window and the moved one counts in the window it ran in.
PULSES = (
    "test_batches b JOIN targets t ON t.target_id=b.target_id AND b.kind='pulse' "
    "AND b.scheduled_for>=%(lo)s-2*make_interval(mins=>t.pulse_interval_minutes) "
    "AND b.scheduled_for<%(hi)s AND b.start_at<%(hi)s "
)
RUNS = (
    "JOIN batch_runs br ON br.batch_id=b.batch_id "
    "AND coalesce(br.start_at,b.start_at)>=%(lo)s AND coalesce(br.start_at,b.start_at)<%(hi)s "
)
# Only the pulse runs whose network's country counted when they started (COUNTRY-MIN-IPS-1,
# with the methodology's country_verdicts: detect.country_rule()).
COUNTED = (
    "LEFT JOIN test_runs r ON r.run_key=br.run_key "
    f"{verdicts.counts_at('br.product_id', 'trim(b.country_code)', verdicts.RUN_AT)}"
)
# Per network, region and whether the runs answered mostly "no exit": their countries, requests,
# connection errors, requests refused at login and answers refused for payment (HTTP 402,
# PAUSE-402-1) of the pulse runs in the window.
WINDOW_STATS_SQL = (
    f"SELECT br.product_id,b.region_id,{pulse_no_exit.EMPTY_SQL} AS empty,"
    "count(DISTINCT b.country_code)::int AS countries,sum(rb.completed)::bigint AS requests,"
    "sum(rb.timeouts+rb.proxy_errors)::bigint AS errors,"
    f"sum(coalesce((rb.errors->>'{proxy_login.ERROR}')::bigint,0))::bigint AS refused,"
    f"sum(coalesce((rb.status_codes->>'{proxy_login.PAYMENT_STATUS}')::bigint,0))::bigint AS "
    "payment FROM " + PULSES + RUNS + COUNTED + "JOIN run_buckets rb ON rb.run_key=br.run_key "
    "AND rb.bucket_start=%(start)s GROUP BY 1,2,3"
)
# The networks and countries of the done pulse runs whose first bucket is the window.
PAIRS_SQL = (
    "SELECT DISTINCT br.product_id,b.country_code FROM "
    + PULSES
    + RUNS
    + "AND br.status='done' "
    + COUNTED
    + "WHERE (SELECT min(rb.bucket_start) FROM run_buckets rb WHERE rb.run_key=br.run_key "
    "AND rb.bucket_start>=%(lo)s-interval '5 minutes' AND rb.bucket_start<=%(start)s)=%(start)s "
    "ORDER BY 1,2"
)
# RUN_P50_SQL over the buckets in [%s, %s), so only those run_buckets partitions are read.
P50_SINCE_SQL = RUN_P50_SQL.replace(
    "b.run_key=ANY(%s)", "b.run_key=ANY(%s) AND b.bucket_start>=%s AND b.bucket_start<%s"
)
# window_stats' counts per network (and `login`, its regions mostly refused at login).
STATS = ("requests", "errors", "regions", "over", "refused", "payment")
NEVER = datetime(9999, 1, 1, tzinfo=UTC)
# A network's latest daily-test run in a country that finished after %s: test_runs_scope
# serves each scope's (scope_id, finished_at) range, so only DAILY_RECENT of runs are read.
# DAILY_ANY_SQL, unbounded, runs only when that finds none (a new network, a lapsed test).
DAILY_RECENT = timedelta(days=2)  # the daily test's 24-hour cadence plus a day of slack
DAILY_BASELINE_SQL = (
    "SELECT r.run_key,r.started_at AS start_at FROM test_runs r JOIN scope_stats s "
    "ON s.scope_id=r.scope_id JOIN pool_stats p ON p.pool_id=s.pool_id WHERE s.product_id=%s "
    "AND upper(p.requested_country)=%s AND r.summary->>%s=%s AND r.finished_at>%s "
    "ORDER BY r.finished_at DESC LIMIT 1"
)
DAILY_ANY_SQL = DAILY_BASELINE_SQL.replace(" AND r.finished_at>%s", "")
CENSUS_BASELINE_SQL = (
    "SELECT br.run_key,b.start_at FROM batch_runs br JOIN test_batches b "
    "ON b.batch_id=br.batch_id WHERE b.kind='census' AND br.product_id=%s "
    "AND b.country_code=%s AND br.status='done' AND br.run_key IS NOT NULL "
    "ORDER BY b.start_at DESC LIMIT 1"
)


async def _settings(conn: psycopg.AsyncConnection) -> dict[str, Any]:
    keys = (
        "down_error_share",
        "down_min_regions",
        "recover_windows",
        "latency_ratio",
        "latency_windows",
        "settle_seconds",
        "evaluated_until",
    )
    cfg = {key: await setting(conn, f"pulse.{key}") for key in keys}
    grace = timedelta(seconds=float(await setting(conn, "batch.start_grace_seconds")))
    # A run starts at most start_grace after its batch (the reaper fails it then) and a pulse
    # run lasts well under a window: a window's buckets come from batches that began this far
    # before it at the most.
    cfg["lookback"] = grace + 2 * WINDOW
    cfg["baselines"] = {}  # (product, country) -> latency_baseline(), for this evaluate call
    cfg["verdicts"] = country_rule()
    return cfg


def _range(start: datetime, cfg: dict[str, Any]) -> dict[str, Any]:
    window = {"start": start, "lo": start - cfg["lookback"], "hi": start + WINDOW}
    return window | verdicts.params(cfg.get("verdicts"))


async def window_stats(
    conn: psycopg.AsyncConnection, start: datetime, share: float, cfg: dict[str, Any]
) -> dict[int, dict[str, Any]]:
    """Per network: requests, connection errors, requests refused at login and answers refused
    for payment over all regions in the window, the regions with data, the regions whose own
    error share reached `share` and the ids of those mostly refused at login (`login`), without
    the runs that answered mostly "no exit" (pulse_no_exit.strip)."""
    rows = await (await conn.execute(WINDOW_STATS_SQL, _range(start, cfg))).fetchall()
    rows = pulse_no_exit.strip(rows)
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        stats = out.setdefault(row["product_id"], dict.fromkeys(STATS, 0) | {"login": []})
        requests, errors = int(row["requests"] or 0), int(row["errors"] or 0)
        refused = int(row["refused"] or 0)
        stats["requests"] += requests
        stats["errors"] += errors
        stats["refused"] += refused
        stats["payment"] += int(row["payment"] or 0)
        if requests:
            stats["regions"] += 1
            stats["over"] += int(errors >= share * requests)
            if proxy_login.refused(refused, requests):
                stats["login"].append(row["region_id"])
    return out


async def _stats(
    conn: psycopg.AsyncConnection, start: datetime, cfg: dict[str, Any]
) -> dict[int, dict[str, Any]]:
    """window_stats, computed once per window and evaluation (cfg["cache"])."""
    cache = cfg.setdefault("cache", {})
    if start not in cache:
        cache[start] = await window_stats(conn, start, float(cfg["down_error_share"]), cfg)
    return cache[start]


def is_down(stats: dict[str, Any] | None, share: float, min_regions: int) -> bool | None:
    """True down, False healthy, None when the window has no pulse data for the network or its
    requests were mostly refused at login (pulse_login.refused: not measured)."""
    if not stats or not stats["requests"] or pulse_login.refused(stats):
        return None
    return stats["errors"] >= share * stats["requests"] and stats["over"] >= min_regions


async def _open_network(conn: psycopg.AsyncConnection, product_id: int) -> list[dict]:
    return await (
        await conn.execute(
            "SELECT outage_id,started_at FROM outages WHERE product_id=%s AND source='pulse' "
            "AND batch_id IS NULL AND ended_at IS NULL",
            (product_id,),
        )
    ).fetchall()


async def _open_outage(
    conn: psycopg.AsyncConnection,
    start: datetime,
    product: int,
    window: PulseWindow,
    rule: int | None,
) -> bool:
    """The network's outage: a row per enabled country that counts for it at `start` (every
    enabled country without the rule), was pulsed by its last pulse interval
    (pulse_unmeasured.PULSED_SQL) and has none open; whether it opened any."""
    counts = verdicts.counts_at("%(product)s", "c.country_code", "%(start)s")
    rows = await (
        await conn.execute(
            "INSERT INTO outages(product_id,country_code,region_id,source,started_at) "
            "SELECT %(product)s,c.country_code,c.region_id,'pulse',%(start)s FROM countries c "
            f"JOIN runner_regions r ON r.region_id=c.region_id AND r.enabled {counts}"
            "WHERE c.enabled AND NOT EXISTS (SELECT 1 FROM outages o "
            "WHERE o.product_id=%(product)s AND o.country_code=c.country_code "
            f"AND o.ended_at IS NULL) {pulse_unmeasured.PULSED_SQL}"
            "ORDER BY c.country_code RETURNING outage_id",
            {"product": product, "start": start, "end": start + WINDOW} | verdicts.params(rule),
        )
    ).fetchall()
    if rows:
        ids = tuple(row["outage_id"] for row in rows)
        await emit_pulse_down(conn, start, window, outage_ids=ids)
    return bool(rows)


async def _recovered(
    conn: psycopg.AsyncConnection, start: datetime, product: int, cfg: dict[str, Any]
) -> bool:
    """True when this window and the recover_windows - 1 before it are all healthy."""
    for k in range(int(cfg["recover_windows"])):
        stats = (await _stats(conn, start - WINDOW * k, cfg)).get(product)
        if is_down(stats, cfg["down_error_share"], int(cfg["down_min_regions"])) is not False:
            return False
    return True


async def _close_outage(
    conn: psycopg.AsyncConnection, product: int, at: datetime, open_rows: list
) -> None:
    ids = tuple(row["outage_id"] for row in open_rows)
    await conn.execute(
        "UPDATE outages SET ended_at=greatest(started_at,%s) WHERE outage_id=ANY(%s)",
        (at, list(ids)),
    )
    down_since = min(row["started_at"] for row in open_rows)
    await emit_pulse_recovered(
        conn, max(at, down_since), product_id=product, down_since=down_since, outage_ids=ids
    )


async def evaluate_window(
    conn: psycopg.AsyncConnection, start: datetime, cfg: dict[str, Any]
) -> dict[str, int]:
    """Open and close network outages for one complete window. Call inside a transaction."""
    share, min_regions = float(cfg["down_error_share"]), int(cfg["down_min_regions"])
    opened = closed = 0
    found = await _stats(conn, start, cfg)
    for product, stats in sorted(found.items()):
        await pulse_login.judge(conn, start, product, stats)
        state = is_down(stats, share, min_regions)
        open_rows = await _open_network(conn, product)
        if state and not open_rows:
            window = PulseWindow(
                product_id=product,
                window_start=start,
                window_minutes=int(WINDOW.total_seconds() // 60),
                errors=stats["errors"],
                requests=stats["requests"],
                regions=stats["over"],
                threshold=share,
            )
            opened += await _open_outage(conn, start, product, window, cfg.get("verdicts"))
        elif state is False and open_rows and await _recovered(conn, start, product, cfg):
            first = start - WINDOW * (int(cfg["recover_windows"]) - 1)
            await _close_outage(conn, product, first, open_rows)
            closed += 1
    closed += await pulse_unmeasured.close_unmeasured(conn, start, found)
    return {"opened": opened, "closed": closed}


async def _p50s(
    conn: psycopg.AsyncConnection, run_keys: list[str], since: datetime, until: datetime = NEVER
) -> dict[str, int]:
    if not run_keys:
        return {}
    keys = sorted(set(run_keys))
    rows = await (await conn.execute(P50_SINCE_SQL, (keys, since, until))).fetchall()
    return {row["run_key"]: row["p50"] for row in rows}


async def latency_baseline(
    conn: psycopg.AsyncConnection, product: int, country: str, now: datetime | None = None
) -> tuple[int | None, str] | None:
    """(p50, source) of a network in a country: the p50 of its latest daily-test run there
    (source "daily", found by the atlas.daily contract), or of its latest done census
    run only while it has no daily-test run yet (source "census", flagged in latency_rise).
    None without either. The one place the baseline source is chosen; pulses never move it.
    A latest daily-test run without a p50 (every request failed) gives (None, "daily"): the
    network and country get no latency_rise until the next daily test, deliberately, rather
    than falling back to an older daily run or the census. The first lookup is bounded to runs
    finished after now - DAILY_RECENT; the unbounded one runs only when it finds none, so the
    result is the same latest run either way.
    """
    daily = (product, country, MARKER, DAILY)
    for source, sql, params in (
        ("daily", DAILY_BASELINE_SQL, (*daily, (now or datetime.now(UTC)) - DAILY_RECENT)),
        ("daily", DAILY_ANY_SQL, daily),
        ("census", CENSUS_BASELINE_SQL, (product, country)),
    ):
        row = await (await conn.execute(sql, params)).fetchone()
        if row is not None:
            key = row["run_key"]
            return (await _p50s(conn, [key], row["start_at"] - WINDOW)).get(key), source
    return None


async def _recent_pulses(
    conn: psycopg.AsyncConnection, product: int, country: str, before: datetime, slots: int
) -> list[dict]:
    """The done pulse runs of the last `slots` slots that started before `before`, newest
    first: a streak is made of runs in consecutive slots, and older runs are never read."""
    return await (
        await conn.execute(
            "SELECT br.run_key,b.region_id,coalesce(br.start_at,b.start_at) AS start_at,"
            "r.finished_at FROM test_batches b "
            "JOIN targets t ON t.target_id=b.target_id JOIN batch_runs br USING(batch_id) "
            "JOIN test_runs r ON r.run_key=br.run_key "
            "WHERE b.kind='pulse' AND b.country_code=%(country)s AND br.product_id=%(product)s "
            "AND br.status='done' AND br.run_key IS NOT NULL "
            "AND coalesce(br.start_at,b.start_at)<%(before)s "
            "AND b.scheduled_for>=%(before)s"
            "-make_interval(mins=>t.pulse_interval_minutes*%(slots)s::int) "
            "ORDER BY coalesce(br.start_at,b.start_at) DESC,b.batch_id DESC LIMIT %(slots)s",
            {"product": product, "country": country, "before": before, "slots": slots},
        )
    ).fetchall()


def _above(p50: int | None, baseline: int, ratio: float) -> bool:
    return p50 is not None and p50 > baseline and p50 >= ratio * baseline


async def evaluate_latency(
    conn: psycopg.AsyncConnection, start: datetime, now: datetime, cfg: dict[str, Any]
) -> int:
    """latency_rise for each network and country whose newest done pulse run began in this
    window and completes a streak of latency_windows runs above ratio x baseline."""
    ratio, streak = float(cfg["latency_ratio"]), int(cfg["latency_windows"])
    pairs = await (await conn.execute(PAIRS_SQL, _range(start, cfg))).fetchall()
    fired = 0
    for pair in pairs:
        product, country = pair["product_id"], pair["country_code"].strip()
        runs = await _recent_pulses(conn, product, country, start + WINDOW, streak + 1)
        if len(runs) < streak:
            continue  # no streak yet: the baseline is never read
        baselines = cfg["baselines"]
        if (product, country) not in baselines:
            baselines[product, country] = await latency_baseline(conn, product, country, now)
        found = baselines[product, country]
        if found is None or found[0] is None:
            continue
        baseline, source = found
        since = runs[-1]["start_at"] - WINDOW
        p50 = await _p50s(conn, [r["run_key"] for r in runs], since, start + 2 * WINDOW)
        above = [_above(p50.get(r["run_key"]), baseline, ratio) for r in runs]
        # Fire once per streak: the run before the streak must not be above too.
        if not all(above[:streak]) or (len(above) > streak and above[-1]):
            continue
        rise = LatencyRise(
            product_id=product,
            country_code=country,
            p50=float(p50[runs[0]["run_key"]]),
            baseline=float(baseline),
            region_id=runs[0]["region_id"],
            baseline_source=source,
        )
        await emit_latency_rise(conn, min(runs[0]["finished_at"], now), rise)
        fired += 1
    return fired


def _floor(at: datetime) -> datetime:
    seconds = WINDOW.total_seconds()
    return datetime.fromtimestamp(at.timestamp() // seconds * seconds, UTC)


async def evaluate(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """Evaluate the complete, settled windows after the watermark, the oldest first and at most
    MAX_WINDOWS per call; a longer backlog (the evaluator was down) continues on the next calls,
    so no window is skipped. The first call starts at the newest settled window. Each call also
    closes the per-country pulse outages the data says are over (pulse_stale, counted in
    `closed`)."""
    now = now or datetime.now(UTC)
    # Events are at their window's start: a day back covers the first windows after a start
    # (a backlog's older windows are in months that were current when they were written); a
    # stale outage's 'recovered' is at most pulse_stale.RECENT back.
    for moment in (now - pulse_stale.RECENT, now - timedelta(days=1), now):
        await db.ensure_partition("test_events", moment)
    totals = {"windows": 0, "opened": 0, "closed": 0, "latency_rise": 0}
    async with db.transaction() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (DETECT_LOCK,))
        cfg = await _settings(conn)
        last = _floor(now - timedelta(seconds=float(cfg["settle_seconds"]))) - WINDOW
        done = cfg["evaluated_until"]
        start = datetime.fromisoformat(done) + WINDOW if done else last
        while start <= last and totals["windows"] < MAX_WINDOWS:
            for key, value in (await evaluate_window(conn, start, cfg)).items():
                totals[key] += value
            totals["latency_rise"] += await evaluate_latency(conn, start, now, cfg)
            totals["windows"] += 1
            await put_setting(conn, "pulse.evaluated_until", start.isoformat())
            start += WINDOW
        totals["closed"] += await pulse_stale.close_stale(conn, now)
        if cfg["verdicts"]:
            totals["closed"] += await pulse_stale.close_ignored(conn, now, cfg["verdicts"])
    return totals
