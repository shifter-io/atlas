"""The browser queue order (K-BP5, `[schedule] plan = "queue"`): pure, no database.

Each [[host]] h of H (in settings order), with its n networks and P = t targets × c countries
positions per round, runs n rounds per period (queue_period_minutes, 1440: a UTC day), one run
every gap = period / (n × P), the host offset h × gap / H after the others. A round is the
targets by browser_seconds, highest first (ties keep the settings order), interleaved heavy and
light ([1st, last, 2nd, second-to-last, …]), once per country. Position p of round r on period
d runs network (p + s × r + d) mod n, starting at period start + lead + offset + (r × P + p) ×
gap: every network meets every (target, country) once a period, and each period shifts the hour
at which they meet. The step s is the first of 1, n−1, 2, n−2, … that is coprime to n and runs
no network twice in a row, across rounds and across periods; s = 1 when none does (n ≤ 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import gcd
from typing import Any

MIN_GAP = timedelta(minutes=5)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


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

    def position(self, day_start: datetime, lead: timedelta, k: int) -> datetime:
        """The start of position k (r × P + p, may run past the period) from day_start."""
        return day_start + lead + self.offset + self.gap * k

    def network_at(self, day: int, k: int) -> str:
        r, p = divmod(k, len(self.items))
        return self.networks[(p + self.step * r + day) % len(self.networks)]

    def entries(self, day: int, period: timedelta, lead: timedelta) -> list[Entry]:
        """Every run of period `day` (periods since the epoch), in start order."""
        start = EPOCH + period * day
        return [
            Entry(self.host, self.network_at(day, k), *item, self.position(start, lead, k))
            for k, item in enumerate(self.items * len(self.networks))
        ]


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


def queues(settings: Any) -> list[HostQueue]:
    """The queue of every [[host]] of these settings (BrowserSettings), in host order."""
    schedule = settings.schedule
    period = timedelta(minutes=schedule.queue_period_minutes)
    order = round_order([(t.id, t.browser_seconds or 0) for t in settings.targets])
    items = tuple((target, country) for country in schedule.countries for target in order)
    hosts = settings.hosts
    result = []
    for h, host in enumerate(hosts):
        n = len(host.networks)
        gap = period / (n * len(items))
        offset = gap * h / len(hosts)
        result.append(HostQueue(host.name, host.networks, items, gap, offset, step(n, len(items))))
    return result


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
    for index, target in enumerate(settings.targets):
        if target.browser_seconds is None:
            raise ValueError(f"target[{index}].browser_seconds: is required in queue mode")
        if target.every_minutes != period:
            raise ValueError(
                f"target[{index}].every_minutes: must equal schedule.queue_period_minutes"
            )
    seen: dict[tuple[str, str, timedelta], str] = {}
    for index, queue in enumerate(queues(settings)):
        if queue.gap < MIN_GAP:
            raise ValueError(f"host[{index}].networks: starts would be under 5 minutes apart")
        share = load(settings, queue.host)
        if share > 1:
            raise ValueError(f"host[{index}].networks: the estimated load {share:.0%} is over 100%")
        for entry in queue.entries(0, timedelta(minutes=period), timedelta(0)):
            key = (entry.target, entry.country, entry.start - EPOCH)
            if seen.setdefault(key, queue.host) != queue.host:
                raise ValueError(f"host[{index}]: starts a target when another host does")
