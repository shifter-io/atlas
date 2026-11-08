"""SIM-GEN-1: generated days at production's cadence and size (SIMULATE.md, "The generated days").

hour(profile, plan, seed, day, hour) returns the runs production runs in one UTC hour:
- the daily test of every network x country whose formula schedules it then (daily/planner.py
  plan_hour), in the planner's cells, each a run directory as cycle_steps.daily_directory
  builds one;
- the browser runs of the hosts' queues (orchestrator/browser_queue.py) that start in it
  (simulate_browser.py);
- no HTTP pulse, census or load wave, unless the plan has pulses (load_plan(pulses=True); see
  docs/api-web-map.md section 14).
Sizes and shapes come from the profile (SIM-PROFILE-1), else the formulas; addresses from
synthetic.py's lanes, one block per network x country. Everything is a pure function of
(profile, plan, seed): the model touches no database, and an hour comes out the same whether it
is generated alone or with others. load_plan reads the plan once from a database.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from functools import cached_property
from itertools import accumulate
from pathlib import Path
from typing import Any

import cycle_steps
import simulate_browser
import synthetic
import writer
from replay_plan import COUNTRIES, TARGETS
from replay_steps import ReplaySteps, ReplayWriter
from simulate_browser import Browser, dig
from simulate_places import Places
from synthetic import JITTER, REUSE, address, apportion, key, mix, unit

from proxy_index.browser.settings import load_settings
from proxy_index.daily.planner import plan_hour
from proxy_index.estimates import compute
from proxy_index.orchestrator.browser_queue import EPOCH, queues
from proxy_index.regions import load_regions
from proxy_index.storage import connect

ROOT = Path(__file__).resolve().parents[2]
FORMULAS = ROOT / "formulas"
BROWSER_SETTINGS = ROOT / "configs" / "browser-pulse.production.example.toml"
MARKER = "scale_sim"
DAYS = 7
HOUR = timedelta(hours=1)
ROOT2 = math.sqrt(2) - 1  # a Weyl step, as simulate_places.PHI


@dataclass(frozen=True)
class Plan:
    """What production plans from: the formulas by network, the browser queue settings and day
    1's date; and the pulse targets (id, minutes) with the enabled countries: none by default."""

    formulas: dict
    browser: Any
    start: date
    days: int = DAYS
    countries: tuple[str, ...] = ()
    pulses: tuple[tuple[str, int], ...] = ()
    regions: Any = None  # a RegionMap; None: configs/regions.yaml

    def day_start(self, day: int) -> datetime:
        if not 1 <= day <= self.days:
            raise ValueError(f"day must be 1..{self.days}")
        return datetime.combine(self.start + timedelta(days=day - 1), time(), UTC)


def load_plan(database, start: date, formulas=FORMULAS, browser=BROWSER_SETTINGS, pulses=False):
    """The plan of a database, read once: its enabled countries and, with pulses, its pulse
    targets (as replay_plan.py reads them), with the formulas and the browser queue settings."""
    settings = load_settings(browser)
    if settings.schedule.plan != "queue":
        raise ValueError(f"{Path(browser).name}: schedule.plan must be queue")
    with connect(database) as conn:
        countries = tuple(sorted(row[0] for row in conn.execute(COUNTRIES).fetchall()))
        targets = conn.execute(TARGETS).fetchall() if pulses else []
    names = sorted(path.stem for path in Path(formulas).glob("*.json"))
    found = [compute.load_formula(Path(formulas), name) for name in names]
    targets = tuple((row[0], row[1]) for row in targets)
    return Plan({f["network"]: f for f in found}, settings, start, DAYS, countries, targets)


@dataclass(frozen=True)
class Lane:
    """One network x country: test hour, unique IPs before the jitter, the share of a day's IPs
    seen the day before, its first IP index, and its formula with the country's entry."""

    network: str
    country: str
    position: int  # the lane's order
    hour: int
    ips: int
    reuse: float
    offset: int
    formula: dict = field(repr=False, compare=False)


class Model:
    """The lanes of every scheduled network x country and the shapes the profile gives them."""

    def __init__(self, profile: dict, plan: Plan, seed):
        self.profile, self.plan, self.seed, self.salt = profile, plan, seed, mix(key(seed))
        self.regions = plan.regions or load_regions()
        slugs = {code: region.slug for region in self.regions.regions for code in region.countries}
        self.summaries = Summaries(seed, slugs)
        self.lanes: dict[tuple[str, str], Lane] = {}
        self.shapes: dict[tuple[str, str], Places] = {}
        offset = 0
        for network in sorted(plan.formulas):
            offset = self._lanes(network, offset)

    def _lanes(self, network: str, offset: int) -> int:
        """Add a network's lanes from `offset`, each with a block for every day; the next free."""
        formula, shape = self.plan.formulas[network], dig(self.profile, "networks", network)
        fallback = synthetic._fallback(formula)
        hours = {entry["hour"]: entry for entry in shape.get("hours", [])}
        reuse = dig(shape, "recurrence").get("share")
        if reuse is None:
            reuse = REUSE[0] + (REUSE[1] - REUSE[0]) * unit(self.seed, "reuse", network)
        for country, hour in sorted(formula["schedule"].items()):
            _, ips, _, entry = synthetic.measured(formula, country, fallback, self.seed)
            ips = dig(hours, f"{hour:02d}", "countries", country).get("unique_ips") or ips
            own = formula | {"countries": {country: entry}}
            where = (network, country, len(self.lanes), hour)
            self.lanes[network, country] = Lane(*where, ips, reuse, offset, own)
            offset += math.ceil(ips * (1 + JITTER)) * self.plan.days
        return offset

    def places(self, network: str, country: str) -> Places:
        if (network, country) not in self.shapes:
            measured = dig(self.profile, "networks", network, "places", country)
            salt = unit(self.seed, "places", network, country)
            self.shapes[network, country] = Places(country, measured, salt)
        return self.shapes[network, country]

    def histogram(self, network: str) -> tuple[list[float], list[tuple[int, int]]]:
        """(cumulative shares, (low, high) request rows) of the profile's requests per IP."""
        seen = dig(self.profile, "networks", network, "seen_count_log2") or {"1": 1}
        found = sorted((int(k.split("-")[0]), int(k.split("-")[-1]), n) for k, n in seen.items())
        total = sum(n for *_, n in found)
        cdf = [value / total for value in accumulate(n for *_, n in found)]
        return cdf, [(low, high) for low, high, _ in found]

    def error_share(self, network: str, hour: int) -> float:
        """Errors over attempts in the network's profile hour; synthetic's share without one."""
        hours = dig(self.profile, "networks", network).get("hours", [])
        found = next((h for h in hours if h["hour"] == f"{hour:02d}"), {})
        total = (found.get("successes") or 0) + (found.get("errors") or 0)
        return (found.get("errors") or 0) / total if total else 1 / (synthetic.ERROR_SHARE + 1)

    def jitter(self, *parts, spread: float = JITTER) -> float:
        """A factor within 1 +- spread, fixed by the seed and the parts."""
        return 1 + spread * (2 * unit(self.seed, *parts) - 1)

    def window(self, lane: Lane, day: int) -> tuple[int, int, int]:
        """(first position, size, positions also used the day before) of a lane on a day."""
        start = end = size = recurring = 0
        for d in range(1, day + 1):
            before = size
            size = max(1, round(lane.ips * self.jitter("size", lane.network, lane.country, d)))
            recurring = min(round(lane.reuse * size), before)
            start, end = end - recurring, end - recurring + size
        return start, size, recurring

    def daily(self, lane: Lane, day: int) -> Daily:
        first, size, recurring = self.window(lane, day)
        k = key(self.seed, "daily", lane.network, lane.country, day)
        started = self.plan.day_start(day) + timedelta(hours=lane.hour, minutes=5 * (1 + k % 8))
        # 2 to 4 buckets, in turn over the lanes: a day's buckets are 3 a test on average.
        buckets = max(1, min(2 + (lane.position + day) % 3, size))
        return Daily(self, lane, day, first, size, recurring, started, buckets)

    def release(self, day: int) -> str:
        return hashlib.sha256(f"{MARKER}:{self.seed}:{day}".encode()).hexdigest()

    def bins(self, network: str, day: int) -> list[int]:
        """Latency bins around the network's median (as synthetic.Model.latency), per day."""
        median = 250 + 900 * unit(self.seed, "latency", network)
        median *= self.jitter("latency", network, day)
        return [math.ceil(median * factor) for factor in synthetic.LATENCY]

    def hour(self, day: int, hour: int) -> Hour:
        start = self.plan.day_start(day) + timedelta(hours=hour)
        schedules = {name: formula["schedule"] for name, formula in self.plan.formulas.items()}
        cells = plan_hour(schedules, hour, regions=self.regions).cells
        lanes = [self.lanes[cell.network, code] for cell in cells for code in cell.countries]
        tests = [self.daily(lane, day) for lane in lanes]
        runs = [simulate_browser.run(self, entry, day) for entry in self.queue(start)]
        return Hour(day, start, tuple(tests), tuple(runs), tuple(self.pulses(start)), cells)

    def queue(self, start: datetime) -> list:
        """The browser queue's entries (browser_queue.py) that start in the hour from `start`."""
        schedule = self.plan.browser.schedule
        period = timedelta(minutes=schedule.queue_period_minutes)
        lead, index = timedelta(seconds=schedule.lead_seconds), (start - EPOCH) // period
        found = [
            entry
            for queue in queues(self.plan.browser)
            for d in (index - 1, index)  # a period's last runs may start after it ends
            for entry in queue.entries(d, period, lead)
            if start <= entry.start < start + HOUR
        ]
        return sorted(found, key=lambda e: (e.start, e.host, e.network, e.target, e.country))

    def pulses(self, start: datetime) -> list[Pulse]:
        """The pulses of the hour at the pulse targets' cadence, with replay_steps' geo model."""
        found, networks = [], sorted(self.plan.formulas)
        for target, minutes in self.plan.pulses:
            step = timedelta(minutes=minutes)
            first = start + (EPOCH - start) % step
            moments = [first + step * k for k in range(math.ceil((start + HOUR - first) / step))]
            found += [
                self.pulse(network, country, target, moment)
                for moment in moments
                for network in networks
                for country in self.plan.countries
            ]
        return found

    def pulse(self, network: str, country: str, target: str, moment: datetime) -> Pulse:
        seed = key(self.seed, "pulse", network, country, target, moment)
        errors, (low, high) = seed % 4, synthetic.GEO_ROWS
        places = self.places(network, country).entries[: low + (seed >> 8) % (high - low + 1)]
        shares = apportion(synthetic.PULSE_REQUESTS - errors, [1] * len(places))
        bins = self.bins(network, 1)
        geo = [
            (place, n, n, bins[(seed + i) % len(bins)])
            for i, (place, n) in enumerate(zip(places, shares, strict=True))
        ]
        geo += [({"matched": False}, errors, 0, None)] if errors else []
        product = self.plan.formulas[network].get("product", "residential")
        return Pulse(network, product, country, target, moment, errors, tuple(geo))


class Summaries(ReplaySteps):
    """ReplaySteps' run summaries (lane, daily_summary) without its database."""

    def __init__(self, seed, slugs: dict[str, str]):
        self.writer, self.replay, self.slugs = ReplayWriter(seed), seed, slugs


@dataclass(frozen=True)
class Daily:
    """One network x country daily test of one day."""

    model: Model = field(repr=False, compare=False)
    lane: Lane
    day: int
    first: int  # first window position of the lane
    size: int  # unique IPs
    recurring: int  # IPs the lane's test of the day before also had
    started_at: datetime
    buckets: int

    @property
    def run_id(self) -> str:
        lane = self.lane
        name = f"{MARKER}:{self.model.seed}:daily:{self.day}:{lane.network}:{lane.country}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, name))

    @property
    def finished_at(self) -> datetime:
        return self.started_at + timedelta(seconds=self.buckets * 300 - 1)

    def ips(self):
        """(IP index, window position, request rows, place) of each IP of the test."""
        lane, model = self.lane, self.model
        places = model.places(lane.network, lane.country)
        cdf, ranges = model.histogram(lane.network)
        for p in range(self.first, self.first + self.size):
            n = lane.offset + p
            u = (p + 7919 * self.day) * ROOT2 % 1
            low, high = ranges[min(bisect.bisect_right(cdf, u), len(ranges) - 1)]
            yield n, p, low + mix(n ^ model.salt ^ self.day) % (high - low + 1), places.pick(p, n)

    def rows(self):
        """(IP index, place, bucket, request rows) per (5-minute bucket, IP), as ingested: an IP
        is in one to all buckets, never in more than its request rows."""
        for n, p, count, place in self.ips():
            width = min(count, 1 + mix(n ^ self.model.salt ^ (self.day << 32)) % self.buckets)
            for j, rows in enumerate(apportion(count, [1] * width)):
                yield n, place, (p + j) % self.buckets, rows

    @cached_property
    def successes(self) -> tuple[list[int], int]:
        """(request rows per bucket, ip_observations rows)."""
        found, observations = [0] * self.buckets, 0
        for _, _, b, rows in self.rows():
            found[b], observations = found[b] + rows, observations + 1
        return found, observations

    @property
    def errors(self) -> int:
        """Timeouts at the profile hour's error share (cycle_steps' buckets count no other)."""
        lane, model = self.lane, self.model
        share = model.error_share(lane.network, lane.hour)
        jitter = model.jitter("errors", lane.network, lane.country, self.day)
        return round(sum(self.successes[0]) * share / (1 - share) * jitter) if share < 1 else 0

    def tables(self) -> dict[str, int]:
        """Rows per table the ingest and the estimate store for the test (SIMULATE.md)."""
        lane = self.lane
        try:
            found = compute.hours(lane.formula, lane.country, self.started_at, self.size, 1)
        except ValueError:  # no factor at the test hour: the test is not estimated
            found = []
        shared = ("ip_enrichment", "ip_addresses", "product_ips", "pool_ips")
        return dict.fromkeys(shared, self.size) | {
            "test_runs": 1,
            "run_buckets": self.buckets,
            "run_geo_buckets": 0,
            "ip_observations": self.successes[1],
            "run_enrichments": 1,
            "estimated_hours": sum(row["estimated"] for row in found),
            "batch_runs": 0,
            "test_events": 0,
        }

    def write(self, directory: Path) -> None:
        """The test's run directory: summary.json, ips.jsonl.gz and the DB-IP files."""
        model, lane = self.model, self.lane
        places, observed = model.places(lane.network, lane.country), []
        for n, place, b, rows in self.rows():
            moment = self.started_at + timedelta(minutes=5 * b)
            row = ReplaySteps.ips_row(self.run_id, address(n), rows, moment)
            observed.append((row, places.enrichment(place)))
        release, meta = model.release(self.day), {"synthetic": True}
        ips = cycle_steps.daily_files(directory, self.run_id, observed, release, meta)
        errors = apportion(self.errors, [1] * self.buckets)
        bins = model.bins(lane.network, self.day)
        buckets = cycle_steps.daily_buckets(self.started_at, self.successes[0], errors, bins)
        product = lane.formula.get("product", "residential")
        target = model.summaries.lane(writer.TARGET, lane.network, product, lane.country)
        summary = model.summaries.daily_summary(
            self.run_id, target, self.started_at, buckets, ips, self.size
        )
        (directory / "summary.json").write_text(json.dumps(summary))


@dataclass(frozen=True)
class Pulse:
    """One pulse run (a plan with pulses): its timeouts and (place, completed, successes, ms)
    rows."""

    network: str
    product: str
    country: str
    target: str
    start: datetime
    errors: int
    geo: tuple = field(repr=False)


@dataclass(frozen=True)
class Hour:
    day: int
    start: datetime
    daily: tuple[Daily, ...]
    browser: tuple[Browser, ...]
    pulses: tuple[Pulse, ...]
    cells: tuple  # daily/planner.py's cells: one per network and region with countries due

    def reports(self) -> list[dict]:
        """Each cell's figures as daily/ingest.py reports them for emit_cell."""
        found = []
        for cell in self.cells:
            mine = {(cell.network, country) for country in cell.countries}
            tests = [t for t in self.daily if (t.lane.network, t.lane.country) in mine]
            report = {"network": cell.network, "region": cell.region, "status": "done"}
            report |= {"countries": list(cell.countries), "ingested": list(cell.countries)}
            report["requests"] = sum(sum(t.successes[0]) + t.errors for t in tests)
            report["unique_ips"] = sum(t.size for t in tests)
            report["started_at"] = min(t.started_at for t in tests).isoformat()
            found.append(report | {"finished_at": max(t.finished_at for t in tests).isoformat()})
        return found


def hour(profile: dict, plan: Plan, seed, day: int, hour: int) -> Hour:
    """The runs of `hour` (0-23, UTC) of `day` (1..plan.days); see the module doc."""
    return Model(profile, plan, seed).hour(day, hour)
