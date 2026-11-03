"""Tape events (test_events) and operator alerts (alerts).

test_events is partitioned by month on `at`: callers create the partition with
`Database.ensure_partition("test_events", at)` before the transaction that inserts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

SEVERITIES = ("info", "minor", "major", "critical")


async def emit(
    conn: psycopg.AsyncConnection,
    kind: str,
    at: datetime,
    *,
    batch_id: int | None = None,
    product_id: int | None = None,
    country_code: str | None = None,
    target_id: str | None = None,
    region_id: int | None = None,
    signal: int = 0,
    severity: str = "info",
    data: dict[str, Any] | None = None,
) -> int:
    row = await (
        await conn.execute(
            "INSERT INTO test_events(at,kind,batch_id,product_id,country_code,target_id,"
            "region_id,signal,severity,data) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING event_id",
            (
                at,
                kind,
                batch_id,
                product_id,
                country_code,
                target_id,
                region_id,
                signal,
                severity,
                Jsonb(data or {}),
            ),
        )
    ).fetchone()
    return row["event_id"]


async def raise_alert(
    conn: psycopg.AsyncConnection,
    kind: str,
    message: str,
    *,
    severity: str = "minor",
    dedupe_key: str | None = None,
    data: dict[str, Any] | None = None,
) -> bool:
    """Open an alert, or bump the open one with the same dedupe key.

    An acknowledged alert that fires again is reopened. Returns True when the alert is new or
    reopened, which callers use to write a tape event once per incident rather than per tick.
    Run inside a transaction: the prior row is locked while it is read.
    """
    if severity not in SEVERITIES:
        raise ValueError("Unknown alert severity")
    key = dedupe_key or kind
    prior = await (
        await conn.execute(
            "SELECT acknowledged_at FROM alerts WHERE dedupe_key=%s FOR UPDATE", (key,)
        )
    ).fetchone()
    await conn.execute(
        "INSERT INTO alerts(kind,severity,message,data,dedupe_key) VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT(dedupe_key) DO UPDATE SET occurrences=alerts.occurrences+1,last_at=now(),"
        "message=excluded.message,severity=excluded.severity,data=excluded.data,"
        "acknowledged_at=NULL,first_at=CASE WHEN alerts.acknowledged_at IS NULL "
        "THEN alerts.first_at ELSE now() END",
        (kind, severity, message, Jsonb(data or {}), key),
    )
    return prior is None or prior["acknowledged_at"] is not None


# --- daily test, hour estimates and pulse tape kinds -------------------------------------------
# One helper per kind fixes its columns and its `data` payload (the contract the site API's tape
# port reads); the payload types check their fields when built. Categories (detect.category):
# daily_test_* and estimate_hour_released are "census", pulse_* is "pulse", latency_rise is
# "incident". A network is a product (product_id); a region is a runner region (region_id, with
# its name repeated in data.region).
DAILY_TEST_STARTED = "daily_test_started"
DAILY_TEST_FINISHED = "daily_test_finished"
ESTIMATE_HOUR_RELEASED = "estimate_hour_released"
PULSE_DOWN = "pulse_down"
PULSE_RECOVERED = "pulse_recovered"
LATENCY_RISE = "latency_rise"
DAILY_TEST_STATUSES = ("done", "failed")


def _check_countries(countries: tuple[str, ...]) -> None:
    if not countries or any(
        not isinstance(c, str) or len(c) != 2 or not c.isupper() for c in countries
    ):
        raise ValueError("countries must be a non-empty list of ISO alpha-2 codes")


def _counts(**values: float | None) -> dict[str, float]:
    """The given counters that are set; each must be a finite non-negative number."""
    out = {}
    for key, value in values.items():
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{key} must be a non-negative number")
        out[key] = value
    return out


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyTest:
    """One network's daily peak-hour test in one region, for the countries due there."""

    product_id: int
    region_id: int
    region: str
    countries: tuple[str, ...]
    batch_id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "countries", tuple(self.countries))
        _check_countries(self.countries)


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyTestResult:
    """How a daily test ended: status done or failed, and the measured counters when known."""

    status: str
    requests: int | None = None
    unique_ips: int | None = None
    duration_s: float | None = None

    def __post_init__(self) -> None:
        if self.status not in DAILY_TEST_STATUSES:
            raise ValueError("status must be done or failed")
        self.counters()

    def counters(self) -> dict[str, float]:
        return _counts(
            requests=self.requests, unique_ips=self.unique_ips, duration_s=self.duration_s
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class HourEstimate:
    """One estimated UTC hour of a network's day (measured IPs x hour factor), over its
    `countries` estimated countries."""

    product_id: int
    day: date
    hour: int
    formula_version: str
    unique_ips: int | None = None
    countries: int | None = None

    def __post_init__(self) -> None:
        hour = self.hour
        if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
            raise ValueError("hour must be 0..23")
        if not self.formula_version:
            raise ValueError("formula_version is required")
        _counts(unique_ips=self.unique_ips, countries=self.countries)


@dataclass(frozen=True, slots=True, kw_only=True)
class PulseWindow:
    """A network's pulse connection errors summed over all regions in one window that passed
    the downtime threshold: errors / requests >= threshold, at most `requests` errors, and a
    window of at least one minute and one region."""

    product_id: int
    window_start: datetime
    window_minutes: int
    errors: int
    requests: int
    regions: int
    threshold: float

    def __post_init__(self) -> None:
        if not 0 <= self.threshold <= 1:
            raise ValueError("threshold must be an error share between 0 and 1")
        self.counters()
        if self.window_minutes <= 0 or self.regions <= 0:
            raise ValueError("a window needs at least one minute and one region")
        if self.requests <= 0 or self.errors > self.requests:
            raise ValueError("a window needs requests and at most that many errors")
        if self.errors < self.threshold * self.requests:
            raise ValueError("the window's error share is below the threshold")

    def counters(self) -> dict[str, float]:
        return _counts(
            window_minutes=self.window_minutes,
            errors=self.errors,
            requests=self.requests,
            regions=self.regions,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class LatencyRise:
    """A pulse p50 (ms) above the daily test's p50 for that network and country; the
    baseline_source "census" flags a network with no daily-test run yet."""

    product_id: int
    country_code: str
    p50: float
    baseline: float
    region_id: int | None = None
    baseline_source: str = "daily"

    def __post_init__(self) -> None:
        _check_countries((self.country_code,))
        _counts(p50=self.p50, baseline=self.baseline)
        if not self.p50 > self.baseline >= 0:
            raise ValueError("a latency rise needs p50 above a non-negative baseline")
        if self.baseline_source not in ("daily", "census"):
            raise ValueError("a latency baseline comes from the daily test or a census")


def _daily_columns(test: DailyTest) -> dict[str, Any]:
    return {"batch_id": test.batch_id, "product_id": test.product_id, "region_id": test.region_id}


async def emit_daily_test_started(
    conn: psycopg.AsyncConnection, at: datetime, test: DailyTest
) -> int:
    """data: {"region": "fra1", "countries": ["DE", "FR"]}. Signal 0."""
    return await emit(
        conn,
        DAILY_TEST_STARTED,
        at,
        **_daily_columns(test),
        data={"region": test.region, "countries": list(test.countries)},
    )


async def emit_daily_test_finished(
    conn: psycopg.AsyncConnection, at: datetime, test: DailyTest, result: DailyTestResult
) -> int:
    """The daily test of one network and region ended and its report was ingested.

    data: {"region", "countries", "status": "done"|"failed", and the measured "requests",
    "unique_ips", "duration_s" when known}. Signal +1 when done, -1 (severity minor) when failed.
    """
    failed = result.status == "failed"
    return await emit(
        conn,
        DAILY_TEST_FINISHED,
        at,
        **_daily_columns(test),
        signal=-1 if failed else 1,
        severity="minor" if failed else "info",
        data={"region": test.region, "countries": list(test.countries), "status": result.status}
        | result.counters(),
    )


async def emit_estimate_hour_released(
    conn: psycopg.AsyncConnection, at: datetime, estimate: HourEstimate
) -> int:
    """One event per network hour, never one per country: data: {"day": "2026-11-02",
    "hour": 0..23, "formula_version", and when known the estimated "unique_ips" over all
    countries and "countries", how many countries the hour covers}. Signal 0.
    """
    return await emit(
        conn,
        ESTIMATE_HOUR_RELEASED,
        at,
        product_id=estimate.product_id,
        data={
            "day": estimate.day.isoformat(),
            "hour": estimate.hour,
            "formula_version": estimate.formula_version,
        }
        | _counts(unique_ips=estimate.unique_ips, countries=estimate.countries),
    )


async def emit_pulse_down(
    conn: psycopg.AsyncConnection,
    at: datetime,
    window: PulseWindow,
    *,
    outage_ids: tuple[int, ...] = (),
) -> int:
    """The window passed the downtime threshold; the outage opens.

    data: {"window_start" (ISO UTC), "window_minutes", "errors", "requests", "regions",
    "threshold" (error share 0..1), "outage_ids" (the outages rows it opened)}. Signal -1,
    severity major.
    """
    return await emit(
        conn,
        PULSE_DOWN,
        at,
        product_id=window.product_id,
        signal=-1,
        severity="major",
        data={
            "window_start": window.window_start.astimezone(UTC).isoformat(),
            "threshold": window.threshold,
            "outage_ids": list(outage_ids),
        }
        | window.counters(),
    )


async def emit_pulse_recovered(
    conn: psycopg.AsyncConnection,
    at: datetime,
    *,
    product_id: int,
    down_since: datetime,
    outage_ids: tuple[int, ...] = (),
) -> int:
    """The network's pulse is below the downtime threshold again; the outage closes.

    data: {"down_since" (ISO UTC), "duration_s", "outage_ids" (the outages rows it closed)}.
    Signal +1.
    """
    duration = (at - down_since).total_seconds()
    if duration < 0:
        raise ValueError("down_since is after the recovery")
    return await emit(
        conn,
        PULSE_RECOVERED,
        at,
        product_id=product_id,
        signal=1,
        data={
            "down_since": down_since.astimezone(UTC).isoformat(),
            "duration_s": duration,
            "outage_ids": list(outage_ids),
        },
    )


async def emit_latency_rise(conn: psycopg.AsyncConnection, at: datetime, rise: LatencyRise) -> int:
    """A lower pulse p50 never raises one (100 concurrency is not comparable with the daily
    test's load).

    data: {"p50", "baseline"} in milliseconds and "baseline_source" ("daily", or "census"
    while the network has no daily-test run in that country). Signal -1, severity minor.
    """
    return await emit(
        conn,
        LATENCY_RISE,
        at,
        product_id=rise.product_id,
        country_code=rise.country_code,
        region_id=rise.region_id,
        signal=-1,
        severity="minor",
        data={
            "p50": rise.p50,
            "baseline": rise.baseline,
            "baseline_source": rise.baseline_source,
        },
    )
