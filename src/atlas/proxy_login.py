"""PROXY-AUTH-ALERT-1: a network that refuses our proxy login, the rule every reader shares.

A request the gateway refused at login is the error proxy_auth (an HTTP 407 from the gateway, or
a SOCKS login refused without a status: transport.py, targets.py). When most of a run's answers
are that, the network did not measure anything: our login or account failed, which is ours to
fix, not the network's downtime (15 Nov 2026: ProxyEmpire refused our unchanged login and 75
network-wide and 108 per-country outages were published within 3 minutes). So:

- the pulse evaluator (orchestrator/pulse.py) treats a window in which at least MOSTLY of a
  network's pulse requests, and of each of at least MIN_REGIONS regions', were refused as not
  measured (no pulse_down, no recovery) and raises the alert proxy_login_refused
  (orchestrator/pulse_login.py);
- detect judges no pulse run whose answers were mostly refused (no 'down', no success_drop)
  and leaves it out of the later pulses' baselines (outages._pulse_history, detect_replay);
- a daily test whose answers were mostly refused is not imported (daily/ingest.py): its cell
  fails, by our side, and the country keeps its previous verdict, estimates and counts.

The low-availability rule (country_status.py) agrees: a login refusal is never a "no exit"
answer, so it never makes a country low availability.
"""

from __future__ import annotations

ERROR = "proxy_auth"
MOSTLY = 0.5  # refused from this share of the requests
MIN_REGIONS = 2  # a window is a login problem from this many refused regions
# A run's refused requests (r: its test_runs row, br: its batch_runs row), for SQL that reads
# batch runs; MEASURED_SQL keeps the runs not mostly refused (refused()), as baselines.
_REFUSED = f"coalesce((r.summary->'errors'->>'{ERROR}')::bigint,0)"
REFUSED_SQL = f"{_REFUSED} AS refused"
MEASURED_SQL = f"{_REFUSED}<{MOSTLY}*br.requests"


def refused(count: int | None, requests: int | None) -> bool:
    """Whether `count` refused requests are at least MOSTLY of `requests` (none: never)."""
    return bool(requests) and (count or 0) >= MOSTLY * requests


def summary_refused(summary: dict) -> bool:
    """Whether a run's summary (errors, completed) was mostly refused at login."""
    errors = summary.get("errors") or {}
    return refused(errors.get(ERROR), summary.get("completed"))
