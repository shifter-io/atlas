"""LOWAVAIL-1 (#1181) step 2: a network's country status from its daily test (a pure rule).

The owner's rule (2026-11-15). A country where a network has almost no exits is "low
availability" (LOW, the code too_few_ips), or "no exits" (NO_EXITS) when its test found none:
never an error. It is decided from data, never from a status code alone. A daily test that found
fewer than verdicts.MIN_IPS unique IPs makes its country low availability only when its failures
are explained: the network's own "no exit" answers (its grammar's no_inventory_statuses,
availability.no_exit) are most of them (MOSTLY), or there are hardly any (under FEW of the
requests: the network answers, from a few exits). A broken network does neither; the data of
14-15 November is in docs/reports/lowavail-answers.md. In a country that is not normally big
(normally_big()), timeouts (TIMEOUTS) count as "no exit" answers too: some networks never answer
"no exit" there and let the request wait (the owner's answer to the report's Question 1).

Three safeguards keep a real problem visible:

1. Two days in a row. A country that had verdicts.BIG IPs or more (its recent daily tests or
   its October census peak) stays a country that counts (COUNTS) on its first test under
   MIN_IPS; only a second one in a row can make it low availability, and never by timeouts.
2. The network guard. When most of a network's normally big countries fail in the same hour
   (network_problem()), a country under MIN_IPS is NETWORK, never low availability.
3. A test under MIN_IPS whose failures are not explained (refused connections, login errors such
   as 407, or timeouts in a normally big country) is FAILING: visible, as today.

A country never tested keeps verdicts' no_daily_test.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from .availability import failures
from .verdicts import BIG, MIN_IPS

COUNTS = "counts"
LOW = "too_few_ips"  # shown as "low availability"
NO_EXITS = "no_exits"  # low availability with no IP at all
FAILING = "failing"  # under MIN_IPS, failures not explained: visible
NETWORK = "network_problem"  # under MIN_IPS while the network guard holds: visible
STATUSES = (COUNTS, LOW, NO_EXITS, FAILING, NETWORK)
EXPECTED = frozenset({LOW, NO_EXITS})  # never an error, an outage or an alert

MOSTLY = 0.5  # "no exit" answers explain a test from this share of its failures
FEW = 0.1  # failures under this share of a test's requests explain it too
DOWN = 0.5  # a big country's test fails (network guard) from this share of failed requests
GUARD_MIN = 3  # the network guard needs this many normally big countries in the hour
# No answer in time: the test's own timeout, or the gateway's (504, the exit did not answer)
TIMEOUTS = ("timeout", "504")


@dataclass(frozen=True)
class DailyTest:
    """One daily test of a network's country: its unique IPs, requests and failed answers
    (availability.failures())."""

    unique_ips: int
    requests: int
    failed: Counter = field(default_factory=Counter)

    @classmethod
    def of(
        cls,
        unique_ips: int,
        requests: int,
        statuses: dict,
        errors: dict,
        completed: int | None = None,
    ) -> DailyTest:
        """A test from its summary's counts: started requests, status_codes, errors and
        completed requests (default: every started one)."""
        done = requests if completed is None else completed
        return cls(unique_ips, requests, failures(statuses, errors, done))

    def failing(self) -> int:
        return sum(self.failed.values())


def explained(test: DailyTest, no_exit: Iterable[str], timeouts: bool = False) -> bool:
    """Whether the test's failures are those of a country with almost no exits: hardly any, or
    mostly the network's "no exit" answers (and with `timeouts`, TIMEOUTS)."""
    total = test.failing()
    if total < FEW * test.requests:
        return True
    codes = (*no_exit, *(TIMEOUTS if timeouts else ()))
    return sum(test.failed[code] for code in codes) >= MOSTLY * total


def normally_big(*sizes: int | None) -> bool:
    """Whether a country had BIG IPs or more in one of `sizes` (its previous daily test, its
    recent ones, its October census peak; None: unknown)."""
    return max((n for n in sizes if n is not None), default=0) >= BIG


def first_day(previous: int | None, census: int | None, recent: int | None = None) -> bool:
    """Whether a test under MIN_IPS is the first one of a normally big country: its previous
    test, if any, was not already under MIN_IPS."""
    if previous is not None and previous < MIN_IPS:
        return False
    return normally_big(previous, recent, census)


def status(
    test: DailyTest,
    no_exit: Iterable[str],
    previous: int | None = None,
    census: int | None = None,
    network_down: bool = False,
    recent: int | None = None,
) -> str:
    """The country's status after `test`: its network's "no exit" answers, the unique IPs of
    its previous daily test (None before its second), its October census peak (None when the
    census found no exit there), whether the network guard holds in the test's hour, and the
    most unique IPs of its daily tests of the last verdicts.RECENT_DAYS (None: only `previous`)."""
    if test.unique_ips >= MIN_IPS:
        return COUNTS
    if network_down:
        return NETWORK
    if first_day(previous, census, recent):
        return COUNTS  # the first test under MIN_IPS: it needs a second one in a row
    if not explained(test, no_exit, timeouts=not normally_big(previous, recent, census)):
        return FAILING
    return NO_EXITS if test.unique_ips == 0 else LOW


def network_problem(tests: Iterable[DailyTest]) -> bool:
    """The network guard: of at least GUARD_MIN normally big countries the network tested in an
    hour, more than half failed at least DOWN of their requests, whatever the answers (a network
    that answers its "no exit" code everywhere is down too). One or two big countries failing
    are those countries' own problem: they stay visible as failing."""
    found = [t.failing() >= DOWN * t.requests for t in tests if t.requests]
    return len(found) >= GUARD_MIN and sum(found) * 2 > len(found)
