"""SCORE-DATA-1: what `simulate.py --add-scored-inputs` reads: the stored load's daily tests
(networks, countries, real IPs, profile), the pulse targets, minimums and load_references."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, timedelta

import simulate_scored as model
from replay import Refused
from replay_plan import COUNTRIES
from replay_steps import SLUGS
from simulate_scored import Inputs, Profile, Target
from target import REPLAY_MARKER

from proxy_index import daily
from proxy_index.calculated import policy
from proxy_index.orchestrator.control import read_setting
from proxy_index.orchestrator.db import Database
from proxy_index.orchestrator.references import load_references

SAMPLE = 2000  # real IPs read per network, country and day; a load run draws from them
RUNS = (
    "FROM test_runs t JOIN scope_stats s ON s.scope_id=t.scope_id "
    "JOIN products p ON p.product_id=s.product_id "
)
DAILY = (
    f"t.summary->>'{daily.MARKER}'='{daily.DAILY}' AND t.summary->>'{REPLAY_MARKER}'=%s "
    "AND t.started_at>=%s AND t.started_at<%s"
)
COUNTRY = "upper(trim(o.requested_country))"
SUCCESSES = "(t.summary->>'successes')::float"
COUNTS = (  # a lane's successes, completed requests, and p50 and p90 times its successes
    f",sum({SUCCESSES}),sum((t.summary->>'completed')::float),"
    f"sum((t.summary->'latency_ms'->>'p50')::float*{SUCCESSES}),"
    f"sum((t.summary->'latency_ms'->>'p90')::float*{SUCCESSES})"
)
LANES = (  # the load's daily tests: network, product, country, UTC day, unique IPs, COUNTS
    f"SELECT p.provider,p.name,{COUNTRY},(t.started_at AT TIME ZONE 'UTC')::date,"
    f"max((t.summary->>'unique_ips')::bigint){COUNTS} {RUNS}"
    f"JOIN pool_stats o ON o.pool_id=s.pool_id WHERE {DAILY} GROUP BY 1,2,3,4"
)
REAL_IPS = (
    f"SELECT ip FROM (SELECT DISTINCT host(a.ip) AS ip {RUNS}"
    "JOIN pool_stats o ON o.pool_id=s.pool_id JOIN ip_observations i ON i.run_key=t.run_key "
    f"JOIN ip_addresses a ON a.ip_id=i.ip_id WHERE {DAILY} AND p.provider=%s AND {COUNTRY}=%s "
    "AND i.bucket_start>=%s) x ORDER BY md5(ip) LIMIT %s"
)
PRODUCTS = "SELECT provider,name,product_id FROM products"
TARGETS = (
    "SELECT target_id,pulse_interval_minutes,pulse_requests,pulse_concurrency,census,"
    "census_concurrency FROM targets WHERE pulse_enabled AND engine<>'browser' "
    "ORDER BY sort_order,target_id"
)


def pulse_days(value: str) -> tuple[str, tuple[int, ...]]:
    """--pulse-days byteful=1,2 (argparse type)."""
    network, _, days = value.partition("=")
    found = tuple(sorted({int(day) for day in days.split(",")}))
    if not network or min(found) < 1:
        raise ValueError(f"{value!r} is not network=day,day")
    return network, found


def add_arguments(parser) -> None:
    add = parser.add_argument
    add("--add-scored-inputs", action="store_true", help="add pulses to a load (SIMULATE.md)")
    add("--pulse-days", type=pulse_days, action="append", default=[], help="as byteful=1,2")


def checked(parser, args) -> None:
    if args.add_scored_inputs and (args.rerun or not args.out):
        parser.error("--add-scored-inputs needs --out (the load's) and no --rerun")
    if args.pulse_days and not args.add_scored_inputs:
        parser.error("--pulse-days goes with --add-scored-inputs")


class Reader:
    """The Inputs of a stored load (its state), read on an open connection."""

    def __init__(self, conn, args, state: dict):
        self.conn, self.args, self.state = conn, args, state
        self.first = date.fromisoformat(state["first_day"])
        self.inputs = Inputs(self.first, args.days, (), {}, {}, {}, 0, 0)
        start = self.inputs.day_start(1)
        self.bounds = (state["replay_id"], start, start + timedelta(days=args.days))
        self.lanes = conn.execute(LANES, self.bounds).fetchall()
        self.products = {(row[0], row[1]): row[2] for row in conn.execute(PRODUCTS)}
        self.product = {row[0]: row[1] for row in self.lanes}
        self.slugs = dict(conn.execute(SLUGS).fetchall())
        self.region_ids = dict(conn.execute(COUNTRIES).fetchall())
        self.source, self.unique = {}, {}

    def read(self) -> Inputs:
        conn, args = self.conn, self.args
        config, _ = policy.load(args.methodology)
        targets = tuple(Target(*row) for row in conn.execute(TARGETS).fetchall())
        countries = self.countries(dict(args.pulse_days))
        regions = {c: self.slugs[c] for c in self.region_ids}
        minimum = 2 * config["minimum_requests"]
        least = policy.slot_settings(config)["min_latency_successes"]
        load = bool(read_setting(conn, "load.enabled"))
        given = (self.first, args.days, targets, countries, regions, self.profiles())
        self.inputs = Inputs(*given, minimum, least, load)
        return self.inputs

    def countries(self, overrides: dict) -> dict:
        """(network, day) -> pulse countries: the enabled ones with real IPs that day, or on
        the nearest real day for a day --pulse-days adds (self.source keeps which)."""
        real = defaultdict(set)
        for network, _, country, day, unique, *_ in self.lanes:
            if country in self.region_ids and unique:
                real[network, (day - self.first).days + 1].add(country)
                self.unique[network, (day - self.first).days + 1, country] = unique
        unknown = set(overrides) - {n for n, _ in real}
        if unknown:
            raise Refused(f"--pulse-days: no daily test of {sorted(unknown)} in the load")
        found = {}
        for network in sorted({n for n, _ in real}):
            days = sorted(d for n, d in real if n == network)
            for day in overrides.get(network, days):
                if day > self.args.days:
                    raise Refused(f"--pulse-days {network}: day {day} is past the load's days")
                self.source[network, day] = min(days, key=lambda d: (abs(d - day), d))
                found[network, day] = tuple(sorted(real[network, self.source[network, day]]))
        return found

    def profiles(self) -> dict:
        """network -> Profile of its daily tests: p50 and p90 weighed by successes."""
        sums = defaultdict(lambda: [0.0] * 4)
        for network, *_, successes, completed, p50, p90 in self.lanes:
            for i, value in enumerate((successes, completed, p50, p90)):
                sums[network][i] += value or 0
        found = {}
        for network, (successes, completed, p50, p90) in sums.items():
            p50, p90 = (p50 / successes, p90 / successes) if successes else (600, 1500)
            rate = successes / completed if completed else 0.9
            found[network] = Profile(p50, max(p90, p50), rate)
        return found

    def ips(self, network: str, country: str, day: int) -> list[str]:
        """A sample of the network's real IPs of the day (its source day) and country."""
        start = self.inputs.day_start(self.source[network, day])
        given = (self.state["replay_id"], start, start + timedelta(days=1), network, country)
        return [row[0] for row in self.conn.execute(REAL_IPS, (*given, start, SAMPLE))]

    def unique_ips(self, run) -> int:
        """A run's distinct exit IPs: never more than the network's real ones there."""
        source = self.source[run.network, self.inputs.day_of(run.moment)]
        real = self.unique.get((run.network, source, run.country), 0)
        return min(run.successes, real, model.LOAD_IPS)


def references(database, inputs: Inputs, day: int) -> dict:
    """slug -> country of the day's load_references, chosen and stored as the orchestrator does
    on its first pulse slot of the day (orchestrator/references.py)."""

    async def chosen():
        db = Database(str(database), size=1)
        try:
            async with db.transaction() as conn:
                return await load_references(conn, inputs.day_start(day))
        finally:
            await db.close()

    found = [row["country_code"].strip() for row in asyncio.run(chosen())]
    return {inputs.regions[country]: country for country in found}


def planned_references(conn, inputs: Inputs) -> dict:
    """day -> slug -> country (--plan-only): the orchestrator's choice without earlier pulses,
    a region's override, else its default, when the region has the country enabled."""
    regions = set(inputs.regions.items())
    given = [read_setting(conn, f"load.reference_{name}") for name in ("overrides", "defaults")]
    found = {
        slug: country.upper()
        for slug in set(inputs.regions.values())
        for country in [given[0].get(slug) or given[1].get(slug) or ""]
        if (country.upper(), slug) in regions
    }
    return dict.fromkeys(range(1, inputs.days + 1), found)
