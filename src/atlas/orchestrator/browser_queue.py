"""The browser queue order (K-BP5, `[schedule] plan = "queue"`): pure, no database.

Each [[host]] h of H (in settings order), with its n networks and P = t targets × c countries
positions per round, runs n rounds per period (queue_period_minutes), one run every gap =
period / (n × P), the host offset h × gap / H after the others. A round is the targets by
browser_seconds, highest first (ties keep the settings order), interleaved heavy and light
([1st, last, 2nd, second-to-last, …]), once per country. Position p of round r on period d runs
network (p + s × r + c) mod n, starting at period start + lead + offset + (r × P + p) × gap,
down to its whole minute: every network meets every (target, country) once a period, at the
same place in every period, so each pair is revisited exactly every period (BROWSER-CYCLE-22H-1).
A period that is not a UTC day shifts the hour of day at which they meet (1320: 2 hours earlier
each day). The step s is the first of 1, n−1, 2, n−2, … that is coprime to n and runs no network
twice in a row, across rounds and periods, also as the daily queue below; s = 1 when none does.

The periods count from schedule.queue_start, a UTC midnight, and c is the daily queue's c of the
day before it. Before queue_start the daily queue it replaces runs: period 1440 from the epoch,
c = d (each day shifts each pair one round, so it was revisited every 7/6 of a day with 6
networks). So every pair's first run from queue_start is in the round of its last daily run,
22 to 24 hours later with 1320, none twice and none skipped, and the file may be installed at any
time before queue_start. Without queue_start the daily queue runs (queue_period_minutes 1440).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import gcd
from typing import Any

MIN_GAP = timedelta(minutes=5)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class Entry:
    """One run of the queue: a network of a host, a target in a country, its start."""

    host: str
    network: str
    target: str
    country: str
    start: datetime


@dataclass(frozen=True, slots=True)
class HostQueue:
    host: str
    networks: tuple[str, ...]
    items: tuple[tuple[str, str], ...]  # (target, country) per position of a round
    gap: timedelta
    offset: timedelta
    step: int
    period: timedelta = DAY
    origin: datetime = EPOCH  # the start of period 0; no entry starts before it
    rotation: int | None = None  # c of every period; None: the period's index (the daily queue)
    until: datetime | None = None  # the daily queue's end: queue_start

    def day(self, moment: datetime) -> int:
        """The period holding `moment`."""
        return (moment - self.origin) // self.period

    def day_start(self, day: int) -> datetime:
        return self.origin + self.period * day

    def position(self, day_start: datetime, lead: timedelta, k: int) -> datetime:
        """The start of position k (r × P + p, may run past the period) from day_start, down to
        its whole minute: a queue run's slot is its start, and a bundle's slot is a whole
        minute (contracts.slot_stamp), also when gap or offset are not (54 runs a day)."""
        start = day_start + lead + self.offset + self.gap * k
        return start - timedelta(seconds=start.second, microseconds=start.microsecond)

    def network_at(self, day: int, k: int) -> str:
        r, p = divmod(k, len(self.items))
        shift = day if self.rotation is None else self.rotation
        return self.networks[(p + self.step * r + shift) % len(self.networks)]

    def entries(self, day: int, lead: timedelta) -> list[Entry]:
        """Every run of period `day` (periods since the origin), in start order, from the
        origin and before `until`."""
        start = self.day_start(day)
        found = [
            Entry(self.host, self.network_at(day, k), *item, self.position(start, lead, k))
            for k, item in enumerate(self.items * len(self.networks))
        ]
        return [
            entry
            for entry in found
            if self.origin <= entry.start and (self.until is None or entry.start < self.until)
        ]


def catch_up_at(
    queue: HostQueue, entry: Entry, taken: set[datetime], now: datetime, schedule: Any
) -> datetime | None:
    """The host's first position from now that no catch-up holds and whose own run is on
    another network, if a run starting there still ends before the entry's next start."""
    lead = timedelta(seconds=schedule.lead_seconds)
    deadline = timedelta(minutes=schedule.run_deadline_minutes)
    rounds = len(queue.items) * len(queue.networks)
    day = queue.day(now)
    day_start = queue.day_start(day)
    k = -((queue.position(day_start, lead, 0) - now) // queue.gap)  # the first at or after now
    while (start := queue.position(day_start, lead, k)) + deadline <= entry.start + queue.period:
        extra, position = divmod(k, rounds)
        if start not in taken and queue.network_at(day + extra, position) != entry.network:
            return start
        k += 1
    return None


def first_on_time(entries: list[Entry], batches: dict[tuple, datetime]) -> dict[str, datetime]:
    """Each host's first on-time batch (start_at = scheduled_for) among these entries. Before
    it the queue was not running for that host (just switched on, a new host): not missed."""
    began: dict[str, datetime] = {}
    for entry in entries:
        if batches.get((entry.target, entry.country, entry.start)) == entry.start:
            began.setdefault(entry.host, entry.start)
    return began


def round_order(costs: list[tuple[str, int]]) -> list[str]:
    """The targets by cost, highest first (ties keep their order), heavy and light alternating."""
    ranked = [target for target, _ in sorted(costs, key=lambda cost: -cost[1])]
    order = []
    while ranked:
        order.append(ranked.pop(0))
        if ranked:
            order.append(ranked.pop())
    return order


def step(networks: int, positions: int) -> int:
    """The round step: coprime to n, with no network twice in a row (see the module doc)."""
    n = networks
    candidates = [s for i in range(1, n) for s in (i, n - i)]
    for s in dict.fromkeys(candidates):
        if gcd(s, n) == 1 and (positions - 1 - s) % n and (positions - 2 - s) % n:
            return s
    return 1


def queues(settings: Any, daily: bool = False) -> list[HostQueue]:
    """The queue of every [[host]] of these settings (BrowserSettings), in host order: from
    schedule.queue_start (or, without it, the daily queue); `daily`: the daily queue before it."""
    schedule = settings.schedule
    start = schedule.queue_start
    if start is None or daily:
        span = (DAY, EPOCH, None, start if daily else None)
    else:
        period = timedelta(minutes=schedule.queue_period_minutes)
        span = (period, start, (start - EPOCH) // DAY - 1, None)
    order = round_order([(t.id, t.browser_seconds or 0) for t in settings.targets])
    items = tuple((target, country) for country in schedule.countries for target in order)
    hosts = settings.hosts
    result = []
    for h, host in enumerate(hosts):
        n = len(host.networks)
        gap = span[0] / (n * len(items))
        offset = gap * h / len(hosts)
        s = step(n, len(items))
        result.append(HostQueue(host.name, host.networks, items, gap, offset, s, *span))
    return result


def in_force(settings: Any, now: datetime) -> dict[str, HostQueue]:
    """Each host's queue at `now`: the daily one before schedule.queue_start."""
    start = settings.schedule.queue_start
    return {q.host: q for q in queues(settings, start is not None and now < start)}


def near(settings: Any, now: datetime) -> list[Entry]:
    """The entries of the periods before, at and after `now`, of the queues of these settings
    and, with schedule.queue_start, the daily one before it, in start order."""
    lead = timedelta(seconds=settings.schedule.lead_seconds)
    daily = queues(settings, True) if settings.schedule.queue_start else []
    found = [
        entry
        for queue in queues(settings) + daily
        for d in range(queue.day(now) - 1, queue.day(now) + 2)
        for entry in queue.entries(d, lead)
    ]
    return sorted(found, key=lambda entry: entry.start)


def load(settings: Any, host: str) -> float:
    """A host's estimated share of its browsers' time: Σ browser_seconds of its runs in a period
    / (browsers_per_host × period)."""
    networks = next(len(h.networks) for h in settings.hosts if h.name == host)
    seconds = sum(t.browser_seconds or 0 for t in settings.targets)
    work = seconds * networks * len(settings.schedule.countries)
    capacity = settings.browser.browsers_per_host * settings.schedule.queue_period_minutes * 60
    return work / capacity


def check(settings: Any) -> None:
    """Refuse queue settings it cannot plan (ValueError naming the field, never a value)."""
    period = settings.schedule.queue_period_minutes
    if period != DAY // timedelta(minutes=1) and settings.schedule.queue_start is None:
        raise ValueError("schedule.queue_start: is required when queue_period_minutes is not 1440")
    for index, target in enumerate(settings.targets):
        if target.browser_seconds is None:
            raise ValueError(f"target[{index}].browser_seconds: is required in queue mode")
        if target.every_minutes != period:
            raise ValueError(
                f"target[{index}].every_minutes: must equal schedule.queue_period_minutes"
            )
    for index, queue in enumerate(queues(settings)):
        if queue.gap < MIN_GAP:
            raise ValueError(f"host[{index}].networks: starts would be under 5 minutes apart")
        share = load(settings, queue.host)
        if share > 1:
            raise ValueError(f"host[{index}].networks: the estimated load {share:.0%} is over 100%")
    _check_hosts(queues(settings))
    if settings.schedule.queue_start is not None:
        _check_hosts(queues(settings, True))


def _check_hosts(hosts: list[HostQueue]) -> None:
    """Refuse two hosts starting a target in a country at the same moment of a period."""
    seen: dict[tuple[str, str, timedelta], str] = {}
    for index, queue in enumerate(hosts):
        for entry in queue.entries(0, timedelta(0)):
            key = (entry.target, entry.country, entry.start - queue.origin)
            if seen.setdefault(key, queue.host) != queue.host:
                raise ValueError(f"host[{index}]: starts a target when another host does")
