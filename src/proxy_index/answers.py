"""Answer classes: which completed requests count toward a run's goal (census rule 4).

Every answer stays in the raw counters. On top of them a run with ``RunConfig.answer_classes``
counts three classes:

- *rate-limited*: a configured rate-limit status (429 on every network, plus the network's
  own, such as Geonode's 469), or a gateway connection refusal (error ``proxy_connect`` or
  ``connection``, counted as the ``proxy_connect`` signal) on a network that signals rate
  limits that way, while the run had a success in the last
  ``RECENT_SUCCESS_SECONDS``. A rate-limited answer never counts toward the goal: a count run
  replaces it, and the census sample excludes it.
- *no-IP*: a status the network sends when it has no exits in the requested country (Geonode's
  465). It counts in the census sample like a duplicate, never as a unique IP.
- *other-country*: a success whose exit the local database places in another country than the
  requested one. It counts in the sample like a duplicate and is neither a unique IP nor an
  observation. An address the database does not match counts as in the country.

A run whose rate limits continue with no success for ``RATE_LIMIT_STOP_SECONDS`` stops with
``stop_reason = "rate_limited"``. The opt-in ``retry_http_469`` policy (census rule 3) is the
special case "469 only, no safety stop, no new summary fields".
"""
from __future__ import annotations

from typing import Any, Protocol

from .models import Outcome, RunConfig
from .stats import Counters

# A gateway connection refusal counts as a rate limit only this soon after the run's latest
# success.
RECENT_SUCCESS_SECONDS = 120.0
# Rate limits and no success for this long stop the run ('rate_limited').
RATE_LIMIT_STOP_SECONDS = 120.0
CONNECT = "proxy_connect"
# The error codes of a gateway connection refusal, all counted under the CONNECT signal: the
# gateway refuses the connection or a tunnel (proxy_connect), or accepts it and drops it before
# answering (connection; a plain-HTTP request has no CONNECT, so this is how DataImpulse's
# gateway refuses under its account connection limit).
REFUSALS = frozenset({CONNECT, "connection"})


class Lookup(Protocol):
    def lookup(self, address: str) -> dict[str, Any]: ...


class Answers:
    """Classify one run's answers; all times are the runner's ``completed_at`` clock."""

    def __init__(self, config: RunConfig, enricher: Lookup | None = None) -> None:
        classes = config.answer_classes
        self.classes = classes
        self.census = config.stop_rule == "unique_ratio"
        signals = (classes.rate_limits if classes is not None
                   else ("469",) if config.retry_http_469 else ())
        self.statuses = frozenset(signal for signal in signals if signal != CONNECT)
        self.connect = CONNECT in signals
        self.no_inventory_statuses = frozenset(classes.no_inventory if classes else ())
        country = config.requested_country if classes is not None else None
        self.country = country.upper() if country and enricher is not None else None
        self.enricher = enricher if self.country else None
        self.rate_limited = dict.fromkeys(signals, 0)
        self.excluded = self.no_inventory = self.other_country = 0
        self.first: float | None = None
        self.last_success: float | None = None
        # A rate-limited answer arrived since the latest success (or the first completion).
        self.limited = False

    def add(self, result: Outcome, completed_at: float) -> str:
        """Count one completed answer; return its class (the first match wins)."""
        if self.first is None:
            self.first = completed_at
        status = str(result.status) if result.status is not None else None
        signal = status if status in self.statuses else None
        if signal is None and self.connect and result.error in REFUSALS and (
                self.last_success is not None
                and completed_at - self.last_success < RECENT_SUCCESS_SECONDS):
            signal = CONNECT
        if signal is not None:
            self.rate_limited[signal] += 1
            self.excluded += 1
            self.limited = True
            return "rate_limited"
        if result.error is None:
            self.last_success = max(self.last_success or completed_at, completed_at)
            self.limited = False
            if self.enricher is not None and result.ip:
                code = self.enricher.lookup(result.ip).get("country_code")
                if code and code.upper() != self.country:
                    self.other_country += 1
                    return "other_country"
        elif status in self.no_inventory_statuses:
            self.no_inventory += 1
            return "no_inventory"
        return "answer"

    def sample(self, totals: Counters) -> int:
        """The census sample: completed answers that were not rate-limited."""
        return totals.completed - self.excluded

    def started(self, count: int) -> int:
        """Starts (or completions) that count toward a request goal."""
        return count - self.excluded if self.classes is not None else count

    def halt(self, completed_at: float) -> str | None:
        """'rate_limited' once rate limits have had no success for RATE_LIMIT_STOP_SECONDS."""
        since = self.last_success if self.last_success is not None else self.first
        if self.classes is None or not self.limited or since is None:
            return None
        return "rate_limited" if completed_at - since >= RATE_LIMIT_STOP_SECONDS else None

    def summary(self, totals: Counters) -> dict[str, Any]:
        """The RunSummary fields of this policy (none without answer classes or rule 3)."""
        if self.classes is None:
            return {"census_requests": self.sample(totals),
                    "http_469_responses": totals.statuses["469"]} if self.statuses else {}
        return {
            "rate_limited": dict(self.rate_limited), "no_inventory": self.no_inventory,
            "other_country": self.other_country if self.enricher is not None else None,
            "census_requests": self.sample(totals) if self.census else None,
        }

    def progress(self, totals: Counters) -> dict[str, Any]:
        """The same counts as plain numbers for a progress callback."""
        found = self.summary(totals)
        if self.classes is not None:
            found["rate_limited"] = self.excluded
        return {key: value for key, value in found.items() if value is not None}
