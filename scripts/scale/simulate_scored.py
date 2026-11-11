"""SCORE-DATA-1: the pulses, load waves and outages a pulse fleet would have produced on the stored
days of a load, so every Index Score input is ready (test data; SIMULATE.md, "Score inputs").
A pure function of (Inputs, seed): the outage plan (outages()), each hour's runs (hour_runs()),
a load run's exit IPs among the real ones (draw()) and the counts of --plan-only (counts()).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta

from synthetic import LATENCY, WEIGHTS, apportion, key, unit

VERSION = "score-inputs-1"
MARK = {"synthetic": True, "score_inputs": VERSION}  # in every added run's summary
BASELINE = 8  # healthy pulses of a series before its first down one (detect needs 6)
GAP = 2  # healthy pulses between two outages of a country
LOAD_IPS = 100  # the most exit IPs one load run draws
JITTER = 0.03  # success rate and latency move within 1 +- JITTER from slot to slot
LOAD_FACTOR = (1.1, 1.8)  # latency under load / light load, per network
SPIKE = 2.5  # one latency spike per network and day: latency_spike and pulse_finished events
WINDOWS = (24, 168, 720)
# Outage minutes: short (all, each pulse day), major (3-4 networks), long or wide (1-2 networks).
MINUTES = {"short": (30, 90), "major": (60, 240), "long": (360, 600), "wide": (120, 240)}


@dataclass(frozen=True)
class Target:
    id: str
    minutes: int  # pulse interval
    requests: int  # pulse_requests
    concurrency: int  # pulse_concurrency
    census: bool
    load_requests: int  # census_concurrency: a load run's requests and concurrency


@dataclass(frozen=True)
class Profile:  # a network's daily tests: p50 and p90 latency (ms), success rate
    p50: float
    p90: float
    rate: float


@dataclass(frozen=True)
class Inputs:
    start: date
    days: int
    targets: tuple[Target, ...]
    countries: dict  # (network, day) -> its pulse countries; absent: no pulse that day
    regions: dict  # country -> runner region slug
    profiles: dict  # network -> Profile
    minimum: int  # completed requests per target, network and day (2 x minimum_requests)
    min_successes: int  # [slots] min_latency_successes
    load: bool = True  # load.enabled

    @property
    def networks(self) -> list[str]:
        return sorted({network for network, _ in self.countries})

    @property
    def step(self) -> int:  # the outage plan's slot: the shortest pulse interval (minutes)
        return min(target.minutes for target in self.targets)

    @property
    def slots(self) -> int:
        return 1440 // self.step

    @property
    def end(self) -> datetime:
        return self.day_start(self.days + 1)

    def day_start(self, day: int) -> datetime:
        return datetime.combine(self.start + timedelta(days=day - 1), time(), UTC)

    def day_of(self, moment: datetime) -> int:
        return (moment.date() - self.start).days + 1

    def pulse_days(self, network: str) -> list[int]:
        return [d for d in range(1, self.days + 1) if (network, d) in self.countries]


@dataclass(frozen=True)
class Outage:
    """Down pulses of a network in its countries from a slot of a day (slots None: open at the
    end of the days)."""

    network: str
    countries: tuple[str, ...]
    day: int
    slot: int
    slots: int | None
    tier: str = field(default="short", compare=False)

    def __contains__(self, pair: tuple) -> bool:  # (network, country)
        return self.network == pair[0] and pair[1] in self.countries

    def span(self, inputs: Inputs) -> tuple[datetime, datetime]:
        start = inputs.day_start(self.day) + timedelta(minutes=self.slot * inputs.step)
        if self.slots is None:
            return start, inputs.end
        return start, start + timedelta(minutes=self.slots * inputs.step)


@dataclass(frozen=True)
class Run:
    kind: str  # pulse or load
    network: str
    country: str
    target: str
    moment: datetime  # the slot (scheduled_for)
    requests: int
    successes: int
    bins: tuple  # (ms, successes) pairs
    run_id: str

    @property
    def batch(self) -> tuple:
        return (self.kind, self.country, self.target, self.moment)


def tiers(inputs: Inputs, seed) -> dict[str, str]:
    order = sorted(inputs.networks, key=lambda n: key(seed, "tier", n))
    longs, majors = 1 + key(seed, "longs") % 2, 3 + key(seed, "majors") % 2
    return {
        n: "long" if i < longs else "major" if i < longs + majors else "short"
        for i, n in enumerate(order)
    }


class Placer:
    """Places a network's outages of one day: each country's next free slot after BASELINE."""

    def __init__(self, inputs: Inputs, seed, network: str, day: int):
        self.inputs, self.seed, self.network, self.day = inputs, seed, network, day
        self.free = dict.fromkeys(inputs.countries[network, day], BASELINE)

    def place(self, countries, tier: str, kind: str, open_end=False) -> Outage | None:
        low, high = MINUTES[kind]
        minutes = low + key(self.seed, "minutes", kind, self.network, self.day) % (high - low + 1)
        # The outage runs from the last healthy pulse: one slot before the first down one.
        total, slots = self.inputs.slots, max(1, round(minutes / self.inputs.step) - 1)
        start = max(self.free[c] for c in countries)
        start = max(start, total - slots) if open_end else start
        slots = min(slots, total - start)
        if slots < 1:
            return None
        self.free |= dict.fromkeys(countries, start + slots + GAP)
        found = (self.network, tuple(countries), self.day, start, None if open_end else slots)
        return Outage(*found, tier=tier)


def network_outages(inputs: Inputs, seed, network: str, tier: str, ongoing: bool) -> list:
    """A short outage on each pulse day, then the tier's on one day (the last when ongoing)."""
    days, found = inputs.pulse_days(network), []
    big = days[-1] if ongoing else days[key(seed, "day", network) % len(days)]
    for day in days:
        placer, countries = Placer(inputs, seed, network, day), inputs.countries[network, day]
        k = key(seed, "country", network, day)
        first = countries[k % len(countries)]
        found.append(placer.place([first], "short", "short"))
        if day == big and tier != "short":
            others = [c for c in countries if c != first] or [first]
            wide = tier == "long" and len(others) >= 2 and k % 2
            chosen = others[: 2 + k % 2] if wide else [others[k % len(others)]]
            found.append(placer.place(chosen, tier, "wide" if wide else tier, ongoing))
    return [outage for outage in found if outage]


def outages(inputs: Inputs, seed) -> list[Outage]:
    """The outage plan: the tiers' spread, one outage still open at the end of the last day
    (the first long or major network that pulses then), every uptime distinct."""
    given = tiers(inputs, seed)
    order = sorted(inputs.networks, key=lambda n: (given[n] == "short", key(seed, "tier", n)))
    ongoing = next((n for n in order if (n, inputs.days) in inputs.countries), None)
    plan = [
        outage
        for n in inputs.networks
        for outage in network_outages(inputs, seed, n, given[n], n == ongoing)
    ]
    # A network whose uptime equals another's: its last closed outage grows (or shrinks) a slot.
    for _ in range(4 * len(inputs.networks) + 8):
        if (network := collision(inputs, plan)) is None:
            break
        index = max(i for i, o in enumerate(plan) if o.network == network and o.slots)
        o = plan[index]
        grown = o.slots + 1 if o.slot + o.slots < inputs.slots else max(1, o.slots - 1)
        plan[index] = replace(o, slots=grown)
    return plan


def uptime(inputs: Inputs, plan: list, network: str, hours: int, digits=2) -> float | None:
    """The outage-time uptime (%) the calculation will find at the end of the days
    (calculated/aggregate.py outage_uptime); None without a pulse in the window."""
    end = inputs.end
    since, early = end - timedelta(hours=hours), timedelta(minutes=inputs.step)
    days = [d for d in inputs.pulse_days(network) if inputs.day_start(d + 1) > since]
    countries = sorted({c for d in days for c in inputs.countries[network, d]})
    down = 0.0
    for country in countries:
        reach = since
        for a, b in sorted(o.span(inputs) for o in plan if (network, country) in o):
            a, b = max(a - early, reach), min(b, end)  # from the last healthy pulse
            if b > a:
                down, reach = down + (b - a).total_seconds(), b
    return round(100 - 100 * down / (hours * 3600 * len(countries)), digits) if countries else None


def collision(inputs: Inputs, plan: list[Outage]) -> str | None:
    """A network whose uptime equals another's: 24h to 2 decimals, 7d and 30d to 4."""
    for hours in WINDOWS:
        seen = set()
        for network in inputs.networks:
            value = uptime(inputs, plan, network, hours, 2 if hours == 24 else 4)
            if value is not None and value in seen:
                return network
            seen.add(value)
    return None


def down(inputs: Inputs, plan: list[Outage], network: str, country: str, moment) -> bool:
    spans = (o.span(inputs) for o in plan if (network, country) in o)
    return any(start <= moment < end for start, end in spans)


def run_id(seed, *parts) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"score_inputs:{seed}:{parts}"))


def latency(profile: Profile, successes: int, factor: float) -> tuple:
    """(ms, successes) bins: synthetic's latency shape stretched to the profile's p50 and p90."""
    spread = max(0.0, (profile.p90 / profile.p50 - 1) / (LATENCY[6] - 1))
    found = {}
    for f, n in zip(LATENCY, apportion(successes, WEIGHTS), strict=True):
        ms = math.ceil(profile.p50 * factor * (1 + (f - 1) * spread if f > 1 else f))
        found[ms] = found.get(ms, 0) + n
    return tuple((ms, n) for ms, n in sorted(found.items()) if n)


def jitter(seed, *parts) -> float:
    return 1 + JITTER * (2 * unit(seed, *parts) - 1)


def spiked(inputs: Inputs, seed, network: str, country: str, moment) -> bool:
    """The network's one latency spike of a day: a slot past BASELINE, in one country."""
    day = inputs.day_of(moment)
    k, countries = key(seed, "spike", network, day), inputs.countries[network, day]
    slot = BASELINE + k % max(1, inputs.slots - BASELINE)
    at = inputs.day_start(day) + timedelta(minutes=slot * inputs.step)
    return country == countries[(k >> 16) % len(countries)] and moment == at


def run(inputs: Inputs, seed, parts: tuple, total: int, good: int, factor: float) -> Run:
    kind, network, country, target, moment = parts
    bins = latency(inputs.profiles[network], good, factor)
    return Run(*parts, total, good, bins, run_id(seed, *parts[:4], moment.isoformat()))


def successes(inputs: Inputs, seed, parts: tuple, total: int) -> int:
    good = round(total * min(1.0, inputs.profiles[parts[1]].rate * jitter(seed, *parts)))
    return min(total, max(good, inputs.min_successes))


def pulse(inputs: Inputs, plan, seed, network: str, country: str, target: Target, moment) -> Run:
    """pulse_requests (more to complete inputs.minimum a day); no success while down."""
    pulses = (1440 // target.minutes) * len(inputs.countries[network, inputs.day_of(moment)])
    total = max(target.requests, math.ceil(inputs.minimum / pulses))
    parts = ("pulse", network, country, target.id, moment)
    good = 0 if down(inputs, plan, *parts[1:3], moment) else successes(inputs, seed, parts, total)
    spike = SPIKE if spiked(inputs, seed, *parts[1:3], moment) else 1
    factor = jitter(seed, "ms", *parts) * spike
    return run(inputs, seed, parts, total, good, factor)


def loads(inputs: Inputs, seed, references: dict, target: Target, moment) -> list[Run]:
    """The slot's load wave: a run per network pulsing in a region's reference country."""
    found, day = [], inputs.day_of(moment)
    for country in sorted(set(references.values())):
        for network in inputs.networks:
            if country in inputs.countries.get((network, day), ()):
                parts, total = ("load", network, country, target.id, moment), target.load_requests
                low, high = LOAD_FACTOR
                factor = low + (high - low) * unit(seed, "load", network)
                factor *= jitter(seed, "ms", *parts)
                good = successes(inputs, seed, parts, total)
                found.append(run(inputs, seed, parts, total, good, factor))
    return found


def hour_runs(inputs: Inputs, plan, seed, references: dict, day: int, hour: int) -> list[Run]:
    """The pulse and load runs of the slots that start in `hour` (0-23, UTC) of `day`."""
    start, found = inputs.day_start(day) + timedelta(hours=hour), []
    for target in inputs.targets:
        step = timedelta(minutes=target.minutes)
        first = math.ceil((start - inputs.day_start(day)) / step)
        moments = [inputs.day_start(day) + step * k for k in range(first, 1440 // target.minutes)]
        for moment in (m for m in moments if m < start + timedelta(hours=1)):
            for network in inputs.networks:
                for country in inputs.countries.get((network, day), ()):
                    found.append(pulse(inputs, plan, seed, network, country, target, moment))
            if target.census and inputs.load:
                found += loads(inputs, seed, references, target, moment)
    return found


def draw(ips: list[str], run: str, successes: int) -> list[tuple[str, int]]:
    """A load run's exit IPs and successes: up to LOAD_IPS of the real ones, from the run id."""
    n = min(len(ips), successes, LOAD_IPS)
    chosen = [ips[(key(run) + i) % len(ips)] for i in range(n)]
    return list(zip(chosen, apportion(successes, [1] * n), strict=True)) if n else []


def counts(inputs: Inputs, plan, seed, references: dict) -> dict:
    """Per day: rows to write; per network its runs and each target's requests."""
    found = {}
    for day in range(1, inputs.days + 1):
        given = (inputs, plan, seed, references.get(day, {}), day)
        runs, per = [r for hour in range(24) for r in hour_runs(*given, hour)], {}
        for r in runs:
            own = per.setdefault(r.network, {"pulse": 0, "load": 0, "requests": {}})
            own[r.kind] += 1
            if r.kind == "pulse":
                own["requests"][r.target] = own["requests"].get(r.target, 0) + r.requests
        rows = dict.fromkeys(("test_runs", "batch_runs", "run_buckets"), len(runs))
        rows["test_batches"] = len({r.batch for r in runs})
        rows["ip_observations"] = sum(min(r.successes, LOAD_IPS) for r in runs if r.kind == "load")
        found[day] = {"rows": rows, "networks": per}
    return found
