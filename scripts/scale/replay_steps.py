"""REPLAY-1: the ingest steps of a simulated hour (REPLAY.md). Each returns (part, done) and keeps
its progress in the step dict it is given."""

from __future__ import annotations

import gzip
import json
import math
import tempfile
import time
from collections import defaultdict, namedtuple
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import browser_model
import psycopg
import synthetic
import writer
from cycle_steps import Failed, Steps, daily_buckets, daily_files
from psycopg.rows import dict_row
from synthetic import apportion
from target import REPLAY_MARKER

from atlas import storage
from atlas.browser.contracts import BROWSER_REGION_ID
from atlas.estimates import compute
from atlas.orchestrator.nodes import issue_token
from atlas.stats import LOCATION_FIELDS, NETWORK_FIELDS
from atlas.targets import registry

NODE_HOST = "scale-replay-"  # every HTTP node the replay registers: scale-replay-<region>
ERRORS = (Failed, OSError, ValueError, KeyError, TypeError, psycopg.Error, aiohttp.ClientError)
REGIONS = (
    "SELECT DISTINCT r.region_id,r.slug FROM countries c JOIN runner_regions r USING(region_id) "
    "WHERE c.enabled ORDER BY 1"
)
SLUGS = "SELECT trim(c.country_code),r.slug FROM countries c JOIN runner_regions r USING(region_id)"
RUNS = (
    "SELECT br.batch_id,br.product_id,br.node_id,br.status,trim(b.country_code) AS country,"
    "b.kind,b.target_id,b.start_at,p.provider,p.name AS product FROM batch_runs br "
    "JOIN test_batches b USING(batch_id) JOIN products p USING(product_id) "
    "WHERE b.kind IN ('pulse','load') AND b.scheduled_for>=%s AND b.scheduled_for<%s "
    "AND br.node_id=ANY(%s) ORDER BY b.start_at,br.batch_id,br.product_id"
)
NODE = "SELECT node_id,host FROM nodes WHERE region_id=%s AND provider=%s AND product=%s"


Lane = namedtuple("Lane", "target config")  # a replay run's summary target and config


class ReplayWriter(writer.Writer):
    """Only Writer.summary (the counters of a run summary), for Steps.summary."""

    def __init__(self, seed):
        self.settings = SimpleNamespace(seed=seed)


def slice_tests(directory: Path, hour: int) -> list[dict]:
    return json.loads((directory / f"slice-{hour:02d}.json").read_text())["tests"]


def networks(directory: Path) -> list[str]:
    """Every network of the 24 slices."""
    return sorted({test["network"] for hour in range(24) for test in slice_tests(directory, hour)})


def slice_rows(directory: Path, hour: int) -> dict[str, list[dict]]:
    """slice-HH's rows by country."""
    rows = defaultdict(list)
    with gzip.open(directory / f"slice-{hour:02d}.jsonl.gz", "rt") as lines:
        for line in lines:
            row = json.loads(line)
            rows[row["country"]].append(row)
    return rows


def places(rows: list[dict]) -> list[tuple[dict, dict]]:
    """The distinct DB-IP (location, network) attributes of the matched rows."""
    found = {}
    for row in rows:
        if row["enrichment"].get("matched"):
            location = {key: row["enrichment"].get(key) for key in LOCATION_FIELDS}
            network = {key: row["enrichment"].get(key) for key in NETWORK_FIELDS}
            found.setdefault(json.dumps([location, network], sort_keys=True), None)
    return [tuple(json.loads(key)) for key in found]


class ReplaySteps(Steps):
    """The replay's ingest steps on one database; state is replay.py's state."""

    def __init__(self, conn, args, state: dict):
        self.conn, self.args, self.state, self.rate = conn, args, state, None
        self.replay, names = state["replay_id"], state["networks"]
        self.formulas = {name: compute.load_formula(Path(args.formulas), name) for name in names}
        self.model = self.of(names)
        self.writer = ReplayWriter(self.replay)
        self.regions = dict(conn.execute(REGIONS).fetchall())
        self.slugs = dict(conn.execute(SLUGS).fetchall())
        self.rows: tuple[int, dict] = (-1, {})

    def of(self, names) -> SimpleNamespace:  # the model the browser step reads
        settings = SimpleNamespace(networks=tuple(names), seed=self.replay)
        return SimpleNamespace(settings=settings, formulas=[self.formulas[n] for n in names])

    def late(self) -> bool:
        return False

    def country_rows(self, hour: int, country: str) -> list[dict]:
        if self.rows[0] != hour:
            self.rows = (hour, slice_rows(Path(self.args.slices), hour))
        return self.rows[1].get(country, [])

    def product(self, network: str) -> str:
        return self.formulas[network].get("product", "residential")

    def node(self, region_id: int, network: str) -> tuple | None:
        return self.conn.execute(NODE, (region_id, network, self.product(network))).fetchone()

    def ours(self, region_id: int, network: str) -> bool:
        """No node there yet, or the replay's own (by id, or by its host name)."""
        found, hosts = self.node(region_id, network), (NODE_HOST, browser_model.HOST)
        return not found or found[0] in self.state["nodes"] or (found[1] or "").startswith(hosts)

    def identity(self, lane: Lane) -> dict:
        return {"target": lane.target, "config": lane.config, REPLAY_MARKER: self.replay}

    def lane(self, target_id: str, provider: str, product: str, country: str) -> Lane:
        known = registry.get(target_id)
        target = {"id": target_id, "version": known.version, "url": known.build_request().url}
        config = {"target": target_id, "provider": provider, "product": product}
        config |= {"requested_country": country, "runner_location": self.slugs[country]}
        return Lane(target | {"engine": known.engine}, config | {"proxy_protocol": "http"})

    # Step 1: the hour's daily tests, from its slice.

    def daily(self, day: int, start: datetime, step: dict) -> tuple[dict, bool]:
        done, failures = step.setdefault("ingested", []), []
        unestimated = step.setdefault("not_estimated", [])
        timings = step.setdefault("test_seconds", [])  # each stored test's ingest and estimate
        tests = slice_tests(Path(self.args.slices), start.hour)
        for test in tests:
            network, country = test["network"], test["country"]
            run_id = self.run_id("daily", day, start.hour, network, country)
            if run_id in done:
                continue
            began = time.monotonic()
            try:  # both idempotent: a retry ingests again (a no-op), then estimates
                with tempfile.TemporaryDirectory(prefix="scale-replay-") as temporary:
                    self.directory(test, run_id, start, Path(temporary))
                    storage.ingest(temporary, self.args.database)  # one transaction
                try:
                    formula = self.formulas[network]
                    compute.estimate_run(self.args.database, run_id, country, formula)
                except ValueError as exc:  # no factor for the country there: noted, not retried
                    unestimated.append(f"{network} {country}: {exc}"[:200])
            except ERRORS as exc:
                failures.append(f"{network} {country}: {exc}"[:300])
                continue
            done.append(run_id)
            timings.append(round(time.monotonic() - began, 3))
        part = {"tests": len(tests), "stored": len(done), "failed": len(failures)}
        return part | {"not_estimated": len(unestimated), "failures": failures[:20]}, not failures

    def directory(self, test: dict, run_id: str, start: datetime, out: Path) -> None:
        """A test's run directory, as cycle_steps.daily_directory builds one."""
        network, country = test["network"], test["country"]
        rows = [row for row in self.country_rows(start.hour, country) if row["network"] == network]
        if not rows:
            raise Failed("the slice holds no IP of the test")
        key = synthetic.key(self.replay, network, country, start.isoformat())
        started, count = start + timedelta(minutes=5 * (1 + key % 8)), 2 + key % 3
        observed, successes = [], [0] * count
        for i, row in enumerate(rows):
            moment = started + timedelta(minutes=5 * (i % count))
            ips = self.ips_row(run_id, row["ip"], row["count"], moment)
            observed.append((ips, row["enrichment"]))
            successes[i % count] += row["count"]
        meta = {REPLAY_MARKER: self.replay}
        artifact = daily_files(out, run_id, observed, rows[0]["dbip_release"], meta)
        errors = apportion(int(test.get("slot_errors") or 0), [1] * count)
        buckets = daily_buckets(started, successes, errors, self.bins(network))
        lane = self.lane(writer.TARGET, network, self.product(network), country)
        unique = len({row["ip"] for row in rows})
        summary = self.daily_summary(run_id, lane, started, buckets, artifact, unique)
        (out / "summary.json").write_text(json.dumps(summary))

    @staticmethod
    def ips_row(run_id: str, ip: str, count: int, moment: datetime) -> dict:
        times = {"bucket_start": moment.isoformat(), "first_seen": moment.isoformat()}
        times["last_seen"] = (moment + timedelta(minutes=4)).isoformat()
        return {"schema_version": "1", "run_id": run_id, "ip": ip, "count": count} | times

    def bins(self, network: str) -> list[int]:
        """The network's latency bins, as synthetic.Model.latency draws them."""
        median = 250 + 900 * synthetic.unit(self.replay, "latency", network)
        return [math.ceil(median * factor) for factor in synthetic.LATENCY]

    # Step 2: the hour's pulses (cycle_steps.Steps.pulses with these hooks).

    def pulse_nodes(self) -> dict[int, str]:
        """A scale-replay node of every network in every region of an enabled country, except
        where production has its own node (its token is never rotated): id -> token."""
        tokens, mine = {}, self.state["nodes"]
        for name in self.model.settings.networks:
            for region_id, slug in self.regions.items():
                if not self.ours(region_id, name):
                    continue
                who = {"provider": name, "product": self.product(name), "region": slug}
                node = issue_token(self.args.database, **who, host=NODE_HOST + slug)
                mine.extend([node["node_id"]] if node["node_id"] not in mine else [])
                tokens[node["node_id"]] = node["token"]
        return tokens

    def pulse_runs(self, start: datetime) -> list[dict]:
        hour = (start, start + timedelta(hours=1), self.state["nodes"])
        return self.conn.cursor(row_factory=dict_row).execute(RUNS, hour).fetchall()

    def plan_times(self, start: datetime) -> list[datetime]:
        return [start + timedelta(minutes=5 * k) for k in range(12)]

    def pulse_ips(self, slot, run: dict, run_id: str, moment: datetime, successes: int) -> list:
        """A load wave's IP rows (its ingest needs them): the slice's IPs of the country."""
        if run["kind"] != "load" or not successes:
            return []
        found = sorted({row["ip"] for row in self.country_rows(slot[1].hour, run["country"])})
        found = (found or [synthetic.address(run["batch_id"])])[:successes]
        counts = apportion(successes, [1] * len(found))
        return [self.ips_row(run_id, ip, n, moment) for ip, n in zip(found, counts, strict=True)]

    def pulse_geo(self, slot, run: dict) -> tuple:
        lane = self.lane(run["target_id"], run["provider"], run["product"], run["country"])
        seed = synthetic.key(self.replay, "pulse", run["batch_id"], run["product_id"])
        errors, (low, high) = seed % 4, synthetic.GEO_ROWS
        found = places(self.country_rows(slot[1].hour, run["country"]))
        found = found[: low + (seed >> 8) % (high - low + 1)]
        bins = self.bins(run["provider"])
        if not found:  # the empty key once: the timeouts too, or the ingest refuses a duplicate
            ms, requests = bins[seed % len(bins)], synthetic.PULSE_REQUESTS
            return lane, errors, [(None, None, requests, requests - errors, ms)]
        shares = apportion(synthetic.PULSE_REQUESTS - errors, [1] * len(found))
        rows = [
            (loc, net, n, n, bins[(seed + i) % len(bins)])
            for i, ((loc, net), n) in enumerate(zip(found, shares, strict=True))
        ]
        return lane, errors, rows + ([(None, None, errors, 0, None)] if errors else [])

    # Step 3: the browser hour (BROWSER-MODEL-1), where production has no browser node.

    def browser(self, day: int, start: datetime, step: dict) -> tuple[dict, bool]:
        mine, names = self.state["nodes"], self.model.settings.networks
        free = [n for n in names if self.ours(BROWSER_REGION_ID, n)]
        if not free:
            return {"skipped": "production has a browser node of every network"}, True
        steps = SimpleNamespace(args=self.args, model=self.of(free))
        try:
            part, done = browser_model.browser_step(steps, day, start, step)
        finally:  # its nodes are registered first: kept even when the hour fails
            found = {self.node(BROWSER_REGION_ID, n) for n in free} - {None}
            mine.extend(sorted({node[0] for node in found} - set(mine)))
        return part | {"networks": len(free)}, done
